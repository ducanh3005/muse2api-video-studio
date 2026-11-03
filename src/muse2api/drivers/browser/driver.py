"""Browser driver: operates the muse.ai web client in headless Chromium over CDP.

Design notes
------------
* One Chromium process; each account gets its own *browser context* (isolated
  cookie jar), so switching accounts never requires clearing cookies.
* One tab per account, reused across requests. Per-account serialisation is
  guaranteed by the account pool (``MUSE2API_ACCOUNT_MAX_CONCURRENCY=1``).
* A follow-up from the same user stays on that tab when the new ``messages``
  continue the conversation already on the page. Anything else opens a fresh
  thread, so two users never share a page.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException

from ...accounts.model import Account
from ...config import Settings
from ...core.prompt import followup_text
from ...errors import (
    UpstreamAuthError,
    UpstreamError,
    UpstreamGlitch,
    UpstreamQuotaError,
    UpstreamRefused,
    UpstreamTimeout,
)
from ...upstream import muse
from ..base import (
    ChatRequest,
    DriverCapabilities,
    ImageRequest,
    InputImage,
    MediaResult,
    MuseDriver,
    SessionInfo,
    VideoRequest,
)
from . import dom
from .cdp import CDPError, CDPSession
from .chromium import ChromiumProcess, find_chromium

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.15
STABLE_POLLS_DONE = 4
STABLE_POLLS_FORCE = 12
# Past this many assistant bubbles the page gets slow and a new thread is cheaper.
HOT_BUBBLE_LIMIT = 16


def is_thread_url(url: str) -> bool:
    """A real muse conversation, not the empty "new thread" page."""
    path = urlsplit(url).path.rstrip("/")
    return path.startswith("/thread/") and path != "/thread/new"


@dataclass
class _Tab:
    account_id: str
    context_id: str
    target_id: str
    session: CDPSession
    hint: str | None = None
    turns: list[tuple[str, str]] = field(default_factory=list)
    image_count: int = 0
    thread_url: str = ""
    quota_note: str = ""
    """Last ignored quota-hint snippet, so a page polled every 0.6s logs it once."""
    rpc_session_id: str = ""
    rpc_uploads: list[str] = field(default_factory=list)
    busy: bool = False
    stale: bool = False
    """Cookies were renewed; close this tab once it is idle."""
    closed: bool = False

    @property
    def has_state(self) -> bool:
        return bool(self.turns or self.thread_url)


class BrowserDriver(MuseDriver):
    name = "browser"
    capabilities = DriverCapabilities(
        chat=True, chat_images=True, image=True, image_edit=True, video=True, renew_session=True
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._chromium: ChromiumProcess | None = None
        self._browser: CDPSession | None = None
        # One browser context (cookie jar) per account, holding up to
        # account_max_concurrency tabs; each request checks one tab out.
        self._contexts: dict[str, str] = {}
        self._tabs: dict[str, list[_Tab]] = {}
        self._opening: dict[str, int] = {}
        self._tab_cond = asyncio.Condition()
        self._ctx_lock = asyncio.Lock()
        self._browser_lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        exe = find_chromium(self.settings.chromium_path)
        self._chromium = ChromiumProcess(
            exe, self.settings.cdp_port, self.settings.profile_dir, self.settings.headless,
            proxy=self.settings.browser_proxy,
        )
        ws_url = await self._chromium.start()
        self._browser = await CDPSession.connect(ws_url)
        log.info("browser driver ready")

    async def shutdown(self) -> None:
        for tabs in list(self._tabs.values()):
            for tab in list(tabs):
                await self._close_tab(tab)
        if self._browser:
            await self._browser.close()
        if self._chromium:
            await self._chromium.stop()

    async def _browser_session(self) -> CDPSession:
        """The browser-level CDP session, reconnecting if its websocket dropped.

        Without this one dead connection fails every request that needs a new tab
        until restart. Chromium usually keeps running, so ``start()`` reattaches and
        its tabs survive; if Chromium itself died, it launches a new one.
        """
        async with self._browser_lock:
            if self._browser and not self._browser.closed:
                return self._browser
            assert self._chromium
            if self._browser:
                log.warning("browser CDP connection lost (%s); reconnecting",
                            self._browser.close_reason)
                await self._browser.close()
            try:
                self._browser = await CDPSession.connect(await self._chromium.start())
                live = await self._browser.send("Target.getBrowserContexts")
            except Exception as exc:  # noqa: BLE001
                raise UpstreamError(f"browser unavailable: {exc}") from exc
            # A relaunched Chromium has none of the old contexts; recreate them on demand.
            ids = set(live.get("browserContextIds", []))
            for account_id, context_id in list(self._contexts.items()):
                if context_id not in ids:
                    del self._contexts[account_id]
            log.info("browser CDP connection restored")
            return self._browser

    async def health(self) -> dict[str, Any]:
        ok = self._browser is not None and not self._browser.closed
        tabs = [t for ts in self._tabs.values() for t in ts]
        return {"driver": self.name, "ok": ok, "tabs": len(tabs),
                "busy_tabs": sum(t.busy for t in tabs)}

    # ------------------------------------------------------------ tabs
    async def _checkout(self, account: Account,
                        prefer: Callable[[_Tab], bool] | None = None) -> _Tab:
        """Reserve an idle tab of ``account``, opening a new one while under the
        per-account limit. An idle tab matching ``prefer`` wins; without one, a new
        tab beats reusing (and so wiping) a tab that holds another conversation.
        Pair every call with ``_checkin``."""
        limit = max(1, self.settings.account_max_concurrency)
        dead: list[_Tab] = []
        async with self._tab_cond:
            while True:
                tabs = self._tabs.setdefault(account.id, [])
                for t in [t for t in tabs
                          if t.closed or (not t.busy and (t.stale or t.session.closed))]:
                    tabs.remove(t)
                    dead.append(t)
                idle = [t for t in tabs if not t.busy]
                can_open = len(tabs) + self._opening.get(account.id, 0) < limit
                match = next((t for t in idle if prefer is None or prefer(t)), None)
                if match or (idle and not can_open):
                    tab = match or next((t for t in idle if not t.has_state), idle[0])
                    tab.busy = True
                    break
                if can_open:
                    self._opening[account.id] = self._opening.get(account.id, 0) + 1
                    tab = None
                    break
                # The pool caps requests per account, so this only waits out a tab
                # that is still being closed.
                await self._tab_cond.wait()
        for t in dead:
            await self._close_tab(t)
        if tab:
            return tab
        try:
            tab = await self._open_tab(account)
        finally:
            async with self._tab_cond:
                self._opening[account.id] -= 1
                if tab:
                    tab.busy = True
                    self._tabs.setdefault(account.id, []).append(tab)
                self._tab_cond.notify_all()
        log.info("opened tab %d for account %s", len(self._tabs[account.id]), account.id)
        return tab

    async def _checkin(self, tab: _Tab) -> None:
        # Free the tab before taking the lock: a request cancelled while waiting for
        # the lock (client gone) must not leave its tab marked busy for good.
        tab.busy = False
        await asyncio.shield(self._notify_tabs())
        if tab.stale:
            await self._close_tab(tab)

    async def _notify_tabs(self) -> None:
        async with self._tab_cond:
            self._tab_cond.notify_all()

    async def _context(self, account: Account) -> str:
        async with self._ctx_lock:
            if ctx := self._contexts.get(account.id):
                return ctx
            browser = await self._browser_session()
            res = await browser.send("Target.createBrowserContext", {"disposeOnDetach": False})
            self._contexts[account.id] = res["browserContextId"]
            return res["browserContextId"]

    async def _open_tab(self, account: Account) -> _Tab:
        if missing := muse.missing_session_cookies(account.cookies):
            raise UpstreamAuthError(f"account is missing session cookies: {', '.join(missing)}")
        assert self._chromium
        try:
            context_id = await self._context(account)
            # A window per tab: Chromium throttles background tabs of a shared window,
            # which made parallel requests on one account run one after another.
            target = await (await self._browser_session()).send(
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": context_id, "newWindow": True},
            )
            target_id = target["targetId"]
            session = await CDPSession.connect(
                f"ws://127.0.0.1:{self._chromium.port}/devtools/page/{target_id}"
            )
            try:
                for domain in ("Page", "Runtime", "Network"):
                    await session.send(f"{domain}.enable")
                await session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
                await session.send("Page.setWebLifecycleState", {"state": "active"})
                await self._set_cookies(session, account)
            except BaseException:
                await session.close()
                raise
        except (CDPError, OSError, asyncio.TimeoutError, WebSocketException) as exc:
            raise UpstreamError(f"browser error: {exc}") from exc
        return _Tab(account.id, context_id, target_id, session)

    async def _close_tab(self, tab: _Tab) -> None:
        """Idempotent; marks the tab closed first so a cancelled close never leaves it
        counted as open or busy."""
        if tab.closed:
            return
        tab.closed, tab.busy = True, False
        await asyncio.shield(self._dispose_tab(tab))

    async def _dispose_tab(self, tab: _Tab) -> None:
        async with self._tab_cond:
            tabs = self._tabs.get(tab.account_id, [])
            if tab in tabs:
                tabs.remove(tab)
            last = not tabs and not self._opening.get(tab.account_id)
            context_id = self._contexts.pop(tab.account_id, None) if last else None
            self._tab_cond.notify_all()
        await tab.session.close()
        if self._browser and not self._browser.closed:
            with contextlib.suppress(Exception):
                await self._browser.send("Target.closeTarget", {"targetId": tab.target_id})
            if context_id:
                with contextlib.suppress(Exception):
                    await self._browser.send(
                        "Target.disposeBrowserContext", {"browserContextId": context_id}
                    )

    @staticmethod
    async def _set_cookies(session: CDPSession, account: Account) -> None:
        floor = time.time() + 3600
        for name, value in account.cookies.items():
            if not value:
                continue
            exp = account.cookie_expires.get(name) or 0
            params: dict[str, Any] = {
                "name": name,
                "value": value,
                "path": "/",
                "secure": True,
                # Never hand Chromium a past expiry; it would drop the cookie silently.
                "expires": exp if exp > floor else time.time() + 7 * 86400,
            }
            if name.startswith("__Host-"):
                params["url"] = "https://muse.ai/"
            else:
                params["domain"] = ".muse.ai"
            with contextlib.suppress(CDPError):
                await session.send("Network.setCookie", params)

    @staticmethod
    def _forget(tab: _Tab) -> None:
        tab.hint = None
        tab.turns = []
        tab.image_count = 0
        tab.thread_url = ""
        tab.rpc_session_id = ""
        tab.rpc_uploads = []

    def _load_hot(self, tab: _Tab, account: Account) -> None:
        """Restore the last conversation after a restart or a new browser tab."""
        if tab.turns or tab.thread_url:
            return
        hot = account.meta.get("hot_page")
        if not isinstance(hot, dict):
            return
        turns = []
        for item in hot.get("turns") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                turns.append((str(item[0]), str(item[1])))
        tab.turns = turns
        tab.hint = hot.get("hint") or None
        tab.thread_url = str(hot.get("url") or "")
        try:
            tab.image_count = int(hot.get("image_count") or 0)
        except (TypeError, ValueError):
            tab.image_count = 0

    def _save_hot(self, tab: _Tab, account: Account, req: ChatRequest, url: str) -> None:
        if url:
            tab.thread_url = url
        tab.hint = req.conversation_hint
        tab.turns = list(req.turns)
        tab.image_count = len(req.images)
        account.meta["hot_page"] = {
            "hint": req.conversation_hint or "",
            "turns": [list(turn) for turn in req.turns],
            "url": tab.thread_url,
            "image_count": tab.image_count,
        }

    async def _href(self, tab: _Tab) -> str:
        try:
            return (await tab.session.evaluate("location.href")) or ""
        except CDPError:
            return ""

    async def _on_thread(self, tab: _Tab) -> bool:
        """The open page is the saved conversation and can take another message."""
        if tab.rpc_session_id:
            try:
                await self._ensure_rpc_ready(tab)
                state = await self._state(tab)
                if state.get("generating"):
                    return False
                return (state.get("agentCount") or 0) < HOT_BUBBLE_LIMIT
            except Exception:
                return False
        href = await self._href(tab)
        if not is_thread_url(href):
            return False
        if tab.thread_url and urlsplit(href).path.rstrip("/") != urlsplit(tab.thread_url).path.rstrip("/"):
            return False
        try:
            page = await tab.session.evaluate(dom.PAGE_STATE) or {}
            if not page.get("hasInput"):
                return False
            if "Connecting..." in (page.get("head") or ""):
                return False
            state = await self._state(tab)
        except CDPError:
            return False
        if state.get("generating"):
            return False
        if any(hint in state.get("tail", "") for hint in dom.STALL_HINTS):
            return False
        return (state.get("agentCount") or 0) < HOT_BUBBLE_LIMIT

    async def _ensure_rpc_ready(self, tab: _Tab) -> None:
        """Ensure the Noise Gateway RPC transport (__hatchEarlyGatewayRuntimeState) is connected."""
        check_js = """Boolean(
            window.__hatchEarlyGatewayRuntimeState &&
            window.__hatchEarlyGatewayRuntimeState.activeRuntime &&
            window.__hatchEarlyGatewayRuntimeState.activeRuntime.getSnapshot &&
            window.__hatchEarlyGatewayRuntimeState.activeRuntime.getSnapshot().isConnected &&
            typeof window.__hatchEarlyGatewayRuntimeState.activeRuntime.getSnapshot().sendRequest === "function"
        )"""
        with contextlib.suppress(CDPError, TimeoutError):
            if await tab.session.evaluate(check_js, timeout=3.0):
                return
        href = await self._href(tab)
        if "muse.ai" not in href:
            await tab.session.send("Page.navigate", {"url": muse.SITE})
        deadline = time.monotonic() + self.settings.page_ready_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            with contextlib.suppress(CDPError, TimeoutError):
                if await tab.session.evaluate(check_js, timeout=4.0):
                    return
                body = (await tab.session.evaluate("document.body ? document.body.innerText : ''", timeout=3.0) or "").lower()
                if any(h in body for h in dom.LOGIN_HINTS):
                    raise UpstreamAuthError("muse.ai redirected to login; cookies are no longer valid")
        raise UpstreamTimeout("Noise Gateway RPC transport did not connect in time")

    async def _new_thread(self, tab: _Tab) -> None:
        import uuid
        self._forget(tab)
        await self._ensure_rpc_ready(tab)
        tab.rpc_session_id = str(uuid.uuid4())
        tab.thread_url = f"{muse.SITE}/thread/{tab.rpc_session_id}"

    # ------------------------------------------------------------ input
    async def _attach(self, tab: _Tab, images: list[InputImage]) -> None:
        if not images:
            return
        await self._ensure_rpc_ready(tab)
        import uuid
        for idx, img in enumerate(images):
            ext = img.mime.split("/")[-1].replace("jpeg", "jpg")
            remote_path = f"workspace/imagine_media/input_{uuid.uuid4().hex[:8]}_{idx}.{ext}"
            b64 = base64.b64encode(img.data).decode()
            js = f"""(async () => {{
                const snap = window.__hatchEarlyGatewayRuntimeState.activeRuntime.getSnapshot();
                await snap.sendRequest("fs.write", {{
                    path: {dom.q(remote_path)},
                    data_base64: {dom.q(b64)},
                    overwrite: true,
                    append: false,
                    create_parent: true
                }});
                return {{ok: true, path: {dom.q(remote_path)}}};
            }})()"""
            res = await tab.session.evaluate(js, await_promise=True, timeout=60.0)
            if not (res or {}).get("ok"):
                raise UpstreamError(f"failed to upload image via Noise RPC fs.write: {res}")
            tab.rpc_uploads.append(remote_path)

    async def _send(self, tab: _Tab, prompt: str) -> None:
        import uuid
        await self._ensure_rpc_ready(tab)
        if not tab.rpc_session_id:
            tab.rpc_session_id = str(uuid.uuid4())
            tab.thread_url = f"{muse.SITE}/thread/{tab.rpc_session_id}"
        full_prompt = prompt
        if tab.rpc_uploads:
            refs = ", ".join(tab.rpc_uploads)
            full_prompt = f"[Reference image file(s) already saved in workspace at: {refs}]\n{prompt}"
            tab.rpc_uploads = []
        js = f"""(async () => {{
            const snap = window.__hatchEarlyGatewayRuntimeState.activeRuntime.getSnapshot();
            const devId = localStorage.getItem("hatch:debug:device-id") || crypto.randomUUID();
            const r = await snap.sendRequest("chat.stream", {{
                message: {dom.q(full_prompt)},
                node_id: devId,
                capabilities: [],
                session_id: {dom.q(tab.rpc_session_id)},
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Saigon"
            }});
            return {{ok: true, session_id: r.session_id || {dom.q(tab.rpc_session_id)}}};
        }})()"""
        res = await tab.session.evaluate(js, await_promise=True, timeout=30.0)
        if not (res or {}).get("ok"):
            raise UpstreamError(f"Noise RPC chat.stream failed: {res}")

    async def _state(self, tab: _Tab) -> dict:
        if not tab.rpc_session_id:
            return {"agentCount": 0, "lastText": "", "generating": False, "attachments": [], "tail": "", "approval": False}
        await self._ensure_rpc_ready(tab)
        js = f"""(async () => {{
            const snap = window.__hatchEarlyGatewayRuntimeState.activeRuntime.getSnapshot();
            let h;
            try {{
                h = await snap.sendRequest("chat.history", {{session_id: {dom.q(tab.rpc_session_id)}}});
            }} catch (e) {{
                return {{agentCount: 0, lastText: "", generating: false, attachments: [], tail: "", approval: false}};
            }}
            const evs = h.chat_events || [];
            const users = evs.filter(e => e.role === "user");
            const assts = evs.filter(e => e.role === "assistant");
            const lastAsst = assts.length > 0 ? assts[assts.length - 1] : null;
            const lastText = lastAsst ? (lastAsst.display_text || (lastAsst.payload && lastAsst.payload.content) || "") : "";
            const lastStatus = lastAsst && lastAsst.payload ? lastAsst.payload.status : "";
            const generating = (users.length > assts.length) || Boolean(lastAsst && lastStatus !== "completed");
            const attachments = [];
            for (const a of assts) {{
                for (const r of (a.resources || [])) {{
                    let meta = {{}};
                    try {{ meta = typeof r.metadata === "string" ? JSON.parse(r.metadata) : (r.metadata || {{}}); }} catch (e) {{}}
                    const p = meta.path || (r.uri ? r.uri.replace("sandbox://", "") : "");
                    if (p) {{
                        attachments.push({{
                            kind: r.kind || (p.endsWith(".mp4") ? "video" : "image"),
                            src: "rpc://" + p,
                            tid: r.label || p,
                            w: meta.width || 0,
                            h: meta.height || 0,
                            mime: r.mime_type || meta.mime_type || ""
                        }});
                    }}
                }}
            }}
            return {{
                agentCount: assts.length,
                lastText,
                generating,
                attachments,
                tail: lastText,
                approval: false
            }};
        }})()"""
        state = await tab.session.evaluate(js, await_promise=True, timeout=20.0) or {}
        tail = state.get("tail", "")
        low = tail.lower()
        if hit := next((h for h in dom.QUOTA_HINTS if h in low), None):
            i = low.find(hit)
            snippet = " ".join(tail[max(0, i - 120):i + 120].split())
            reply = state.get("lastText", "")
            if not state.get("generating") and hit in reply.lower():
                log.warning("quota hint %r in reply on %s: %s", hit,
                            tab.thread_url or "new thread", snippet)
                raise UpstreamQuotaError(f"muse.ai reports the account is out of quota: …{snippet}…")
            if snippet != tab.quota_note:
                tab.quota_note = snippet
                log.info("ignoring quota hint %r outside the reply on %s: %s", hit,
                         tab.thread_url or "new thread", snippet)
        return state

    async def _prepare(self, tab: _Tab, prompt: str, images: list[InputImage]) -> dict:
        await self._new_thread(tab)
        base = await self._state(tab)
        await self._attach(tab, images)
        await self._send(tab, prompt)
        return base

    async def _deny_approvals(self, tab: _Tab) -> None:
        denied = await tab.session.evaluate(dom.DENY_APPROVALS) or []
        for what in denied:
            log.info("denied muse.ai %s", what.lower())

    # ------------------------------------------------------------ chat
    async def _live_continuation(self, tab: _Tab) -> bool:
        """This tab already shows the stored conversation, even if its URL is still /thread/new."""
        last_user = next((text for role, text in reversed(tab.turns) if role == "user"), "")
        if not last_user:
            return False
        try:
            page = await tab.session.evaluate(dom.PAGE_STATE) or {}
            if not page.get("hasInput"):
                return False
            state = await self._state(tab)
            present = await tab.session.evaluate(
                f"!!(document.body && document.body.innerText.includes({dom.q(last_user[:80])}))"
            )
        except CDPError:
            return False
        if state.get("generating"):
            return False
        if any(hint in state.get("tail", "") for hint in dom.STALL_HINTS):
            return False
        if (state.get("agentCount") or 0) >= HOT_BUBBLE_LIMIT:
            return False
        return bool(present)

    async def _open_saved(self, tab: _Tab) -> None:
        if tab.thread_url and "/thread/" in tab.thread_url:
            sid = tab.thread_url.rsplit("/thread/", 1)[-1].split("?")[0].strip("/")
            if sid and sid != "new":
                await self._ensure_rpc_ready(tab)
                tab.rpc_session_id = sid
                return
        await tab.session.send("Page.navigate", {"url": tab.thread_url})
        deadline = time.monotonic() + self.settings.page_ready_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            if await self._on_thread(tab):
                return
        raise UpstreamTimeout("saved conversation did not become ready")

    async def _begin_chat(self, account: Account, req: ChatRequest):
        """Send ``req`` on the saved conversation when it continues that page."""
        hint = req.conversation_hint or ""
        tab = await self._checkout(account, prefer=lambda t: t.has_state and (t.hint or "") == hint)
        prompt, images = req.prompt, req.images
        try:
            # The saved conversation (restored after a restart) may only go into a
            # tab while no other tab of the account already holds a conversation.
            if not any(t.has_state for t in self._tabs.get(account.id, []) if t is not tab):
                self._load_hot(tab, account)
            same_owner = (tab.hint or "") == (req.conversation_hint or "")
            follow = followup_text(tab.turns, req.turns) if tab.turns and same_owner else None
            if follow and len(req.images) >= tab.image_count and (
                tab.thread_url or await self._live_continuation(tab)
            ):
                if await self._on_thread(tab) or await self._live_continuation(tab):
                    log.info("reusing hot page %s", tab.thread_url or "open page")
                elif tab.thread_url:
                    log.info("reopening %s", tab.thread_url)
                    await self._open_saved(tab)
                prompt = follow
                images = req.images[tab.image_count:]
            else:
                log.info("opening a new thread")
                saved = account.meta.get("hot_page")
                if isinstance(saved, dict) and (saved.get("hint") or "") == hint:
                    account.meta.pop("hot_page", None)
                await self._new_thread(tab)
            base = await self._state(tab)
            await self._attach(tab, images)
            await self._send(tab, prompt)
            return tab, base
        except CDPError as exc:
            await self._close_tab(tab)
            await self._checkin(tab)
            raise UpstreamError(f"browser error: {exc}") from exc
        except BaseException:
            self._forget(tab)
            await self._checkin(tab)
            raise

    async def chat_stream(self, account: Account, req: ChatRequest) -> AsyncIterator[str]:
        tab, base = await self._begin_chat(account, req)
        base_count, base_text = base.get("agentCount", 0), base.get("lastText", "")
        started = time.monotonic()
        emitted, last, stable, got_first = "", None, 0, False
        finished = False
        try:
            while True:
                if req.cancel and req.cancel.is_set():
                    return
                elapsed = time.monotonic() - started
                if elapsed > req.timeout:
                    raise UpstreamTimeout("assistant reply timed out")
                if not got_first and elapsed > req.first_token_timeout:
                    raise UpstreamTimeout("no first token from assistant")
                await asyncio.sleep(POLL_INTERVAL)
                st = await self._state(tab)
                if st.get("approval"):
                    await self._deny_approvals(tab)
                    continue
                text = st.get("lastText", "")
                is_new = st.get("agentCount", 0) > base_count or (text and text != base_text)
                if not is_new or not text:
                    if elapsed > 15 and any(h in st.get("tail", "") for h in dom.STALL_HINTS):
                        raise UpstreamTimeout("upstream workspace is stuck connecting")
                    continue
                got_first = True
                if text != last:
                    delta = text[len(emitted):] if text.startswith(emitted) else text
                    if delta:
                        emitted = text
                        yield delta
                    last, stable = text, 0
                else:
                    stable += 1
                    if (not st.get("generating") and stable >= STABLE_POLLS_DONE) or stable >= STABLE_POLLS_FORCE:
                        # Raised even though the text already went out, so the reply
                        # ends as an error (and is not billed) rather than as an answer.
                        if dom.is_glitch_reply(text):
                            raise UpstreamGlitch(f"muse.ai failed while responding: {text[:200]}")
                        finished = True
                        return
        except CDPError as exc:
            await self._close_tab(tab)
            raise UpstreamError(f"browser error: {exc}") from exc
        finally:
            if finished:
                url = tab.thread_url if tab.rpc_session_id else ""
                if not url:
                    for _ in range(10):
                        url = await self._href(tab)
                        if is_thread_url(url):
                            break
                        await asyncio.sleep(0.2)
                self._save_hot(tab, account, req, url if is_thread_url(url) else "")
                if tab.thread_url:
                    log.info("saved hot page %s", tab.thread_url)
                else:
                    log.info("reply finished but the thread url was not ready yet")
            else:
                self._forget(tab)
            await self._checkin(tab)

    # ------------------------------------------------------------ media
    @staticmethod
    def _media_prompt(prompt: str, kind: str, size: str | None, duration: int | None = None,
                      has_ref: bool = False) -> str:
        ratio = size or "16:9"
        dur = duration or 10
        if kind == "video" and dur > 10:
            num_scenes = min(6, max(2, (dur + 9) // 10))
            return (
                f"You must produce a single seamless {dur}-second ({num_scenes} scenes x 10s) {ratio} video "
                f"with 100% character and environment consistency across all scenes.\n"
                f"Follow these exact steps without skipping:\n"
                f"1. MASTER KEYFRAME: Call media.generate_image (output_format='png') ONCE to generate a "
                f"razor-sharp {ratio} master reference PNG image establishing the exact characters and setting.\n"
                f"2. {num_scenes} CONSISTENT 10-SECOND SCENES: Call media.generate_video {num_scenes} times "
                f"(Scene 1 to Scene {num_scenes}, 10 seconds each). In Scene 1, pass the master PNG (kind='image') "
                f"and Scene 1 text. In Scenes 2..{num_scenes}, ALWAYS pass resume_from_snapshot_id from the "
                f"previous media.generate_video call AND pass the master PNG (kind='image') along with that "
                f"scene's action and spoken dialogue so the characters stay 100% identical.\n"
                f"3. VIETNAMESE DIALOGUE & FFMPEG STITCH: Using muse.exec, if dialogue is requested, ensure "
                f"clear Vietnamese dialogue audio (using /opt/hatch/bin/tts --language vi with two distinct "
                f"voices mixed with the video audio if needed) and run ffmpeg to concatenate all {num_scenes} "
                f"scenes in order into one continuous {dur}-second MP4 file (final_{dur}s.mp4).\n"
                f"4. ATTACH ONLY FINAL VIDEO: Attach ONLY the final stitched {dur}-second MP4 file "
                f"(final_{dur}s.mp4) in your chat reply (do not attach intermediate clips or the PNG).\n\n"
                f"Story & Scene Specification:\n{prompt}"
            )
        if kind == "video" and not has_ref:
            return (
                f"First call media.generate_image (output_format='png') to create a razor-sharp, "
                f"photorealistic {ratio} master keyframe image for this scene (clean focal subject, "
                f"8K cinema lens clarity, tack-sharp textures, no tiny distorted background crowds). "
                f"Then immediately call media.generate_video passing that generated PNG image as input "
                f"to animate it smoothly into a {dur}-second {ratio} video with zero motion blur, "
                f"and attach the final video in chat. Scene description: {prompt}"
            )
        hints = []
        if size:
            hints.append(f"aspect ratio {size}")
        if duration:
            hints.append(f"{duration} seconds long")
        verb = "Generate an image" if kind == "image" else "Generate an ultra-sharp video"
        suffix = f" ({', '.join(hints)})" if hints else ""
        return f"{verb}{suffix}: {prompt}"

    # muse.ai posts a completion sentence ("Here's your video…") a few seconds
    # before the media attachment's blob src is ready, so a reply that is "text
    # only" right now may still be finalising. Keep waiting this long for the
    # attachment to appear/load before treating the reply as a refusal.
    _MEDIA_GRACE = {"video": 90.0, "image": 8.0}
    # The timeout is soft while muse.ai is visibly still working (stop button
    # shown or attachment loading): giving up then threw away images that landed
    # a minute later, and the failover started the whole generation over.
    _OVERRUN_FACTOR = 2.5
    # ...but only while the reply is still moving. A stuck agent turn (tools
    # loaded, generation never called) keeps the stop button up indefinitely,
    # and waiting out the full overrun on it cost clients 10-25 minutes.
    _STALL_AFTER = {"video": 480.0, "image": 120.0}

    async def _wait_media(self, tab: _Tab, base: dict, kind: str, timeout: float,
                          on_progress, cancel: asyncio.Event | None,
                          seen_srcs: set[str] | None = None) -> dict:
        base_atts = len(base.get("attachments", []))
        base_count = base.get("agentCount", 0)
        base_text = base.get("lastText", "")
        grace = 15.0 if seen_srcs is not None else self._MEDIA_GRACE.get(kind, 8.0)
        stall_after = self._STALL_AFTER.get(kind, 120.0)
        started = changed_at = time.monotonic()
        text_done_at: float | None = None
        denied = asked_to_attach = busy = loading = False
        last_sig: tuple | None = None
        while True:
            now = time.monotonic()
            elapsed, quiet = now - started, now - changed_at
            if elapsed >= timeout and not (busy and elapsed < timeout * self._OVERRUN_FACTOR
                                           and (loading or quiet < stall_after)):
                break
            if cancel and cancel.is_set():
                raise asyncio.CancelledError
            await asyncio.sleep(0.6)
            st = await self._state(tab)
            if st.get("approval"):
                await self._deny_approvals(tab)
                denied, text_done_at = True, None
                continue
            all_atts = st.get("attachments", [])
            new_atts = all_atts[base_atts:] if seen_srcs is None else all_atts
            loading = any(kind in (a.get("tid") or "") and not a.get("src") for a in new_atts)
            fresh = [
                a for a in new_atts
                if a.get("src") and a.get("kind") == kind and (seen_srcs is None or a.get("src") not in seen_srcs)
            ]
            if fresh and not st.get("generating") and not loading:
                if (waited := time.monotonic() - started) > timeout:
                    log.info("%s arrived %.0fs past the %.0fs timeout", kind, waited - timeout, timeout)
                return fresh[-1]
            sig = (st.get("agentCount", 0), st.get("lastText", ""),
                   tuple(a.get("tid") or "" for a in new_atts))
            if sig != last_sig:
                last_sig, changed_at = sig, time.monotonic()
            busy = bool(st.get("generating")) or loading
            elapsed = time.monotonic() - started
            if on_progress:
                on_progress(min(95, int(elapsed / timeout * 100)))
            # An attachment node for this kind is on the page but its (blob) src
            # has not loaded yet -> media is still finalising, keep waiting.
            if loading:
                text_done_at = None
                continue
            # No attachment yet. The reply may be genuinely text-only (a refusal),
            # or the attachment may simply lag the completion sentence.
            text = st.get("lastText", "")
            has_new_reply = (st.get("agentCount", 0) > base_count) or (bool(text) and text != base_text)
            if has_new_reply and text and not st.get("generating"):
                if text_done_at is None:
                    text_done_at = time.monotonic()
                elif time.monotonic() - text_done_at >= grace:
                    if any(h in text.lower() for h in dom.GLITCH_HINTS):
                        raise UpstreamGlitch(f"muse.ai failed while responding: {text[:200]}")
                    if not asked_to_attach:
                        log.info("%s not attached (%s); asking for it in chat: %.120s", kind,
                                 "denied approval" if denied else "text-only reply", text)
                        if "want to try" in text.lower() or "calmer version" in text.lower() or "safer version" in text.lower():
                            await self._send(
                                tab,
                                "Yes, generate that calm version video right now using media.generate_video "
                                "with resume_from_snapshot_id and master_keyframe.png, and attach the video in chat."
                            )
                        else:
                            await self._send(tab, f"Please attach the final {kind} file here in "
                                                  "the chat" + (" instead of uploading it." if denied
                                                                else ", as a single attachment."))
                        asked_to_attach, text_done_at = True, None
                        base_count = st.get("agentCount", 0)
                        base_text = text
                        continue
                    # Last resort: media delivered as a link in the text bubble.
                    probe = await tab.session.evaluate(dom.LAST_BUBBLE_MEDIA) or {}
                    url = self._pick_media_url(probe.get("links", []), kind)
                    if url:
                        log.info("%s delivered as link in text bubble: %s", kind, url)
                        return {"src": url, "kind": kind}
                    log.info("%s: no media after %.0fs grace; links=%s html=%.300s",
                             kind, grace, probe.get("links", []), probe.get("html", ""))
                    raise UpstreamRefused(f"upstream replied with text only: {text[:200]}")
            else:
                text_done_at = None
        if busy:
            log.warning("%s still generating after %.0fs (reply unchanged for %.0fs); giving up: %.160s",
                        kind, elapsed, quiet, (last_sig or (0, ""))[1].replace("\n", " "))
        raise UpstreamTimeout(f"{kind} generation timed out")

    _VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v")
    _IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")
    _MEDIA_HOSTS = ("videodelivery.net", "cloudflarestream.com", "cloudflarestorage.com",
                    "amazonaws.com", "storage.googleapis.com", "blob.core.windows.net",
                    "musecdn", "cdn.muse", "muse-cdn")
    _MEDIA_PATHS = ("/media/", "/download", "/dl/", "/video", "/attachment", "/files/")

    @classmethod
    def _pick_media_url(cls, links: list[str], kind: str) -> str | None:
        """Best media URL from a reply bubble, or None if nothing looks like one."""
        exts = cls._VIDEO_EXT if kind == "video" else cls._IMAGE_EXT

        def score(u: str) -> int:
            low = u.lower()
            path = low.split("?", 1)[0].split("#", 1)[0]
            if u.startswith("blob:") or path.endswith(exts):
                return 3
            if any(h in low for h in cls._MEDIA_HOSTS):
                return 2
            if any(seg in low for seg in cls._MEDIA_PATHS):
                return 1
            return 0

        cands = [u for u in links if u and not u.startswith(("mailto:", "javascript:"))]
        cands.sort(key=score, reverse=True)
        return cands[0] if cands and score(cands[0]) > 0 else None

    async def _download(self, tab: _Tab, att: dict, kind: str) -> MediaResult:
        src = att.get("src") or ""
        if src.startswith("rpc://"):
            rpc_path = src[len("rpc://"):]
            js = f"""(async () => {{
                try {{
                    const snap = window.__hatchEarlyGatewayRuntimeState.activeRuntime.getSnapshot();
                    const blob = await snap.sendRequest("fs.raw", {{path: {dom.q(rpc_path)}}});
                    const b64 = await new Promise((res, rej) => {{
                        const r = new FileReader();
                        r.onload = () => res(r.result.split(",")[1]);
                        r.onerror = () => rej(r.error);
                        r.readAsDataURL(blob);
                    }});
                    return {{ok: true, b64, mime: blob.type || ""}};
                }} catch (e) {{
                    return {{ok: false, err: String(e)}};
                }}
            }})()"""
            res = await tab.session.evaluate(js, await_promise=True, timeout=300)
        else:
            res = await tab.session.evaluate(dom.fetch_as_base64(src), await_promise=True,
                                             timeout=300)
        if not (res or {}).get("ok"):
            raise UpstreamError(f"failed to download generated {kind}: {(res or {}).get('err')}")
        mime = res.get("mime") or att.get("mime") or ("video/mp4" if kind == "video" else "image/png")
        return MediaResult(data=base64.b64decode(res["b64"]), mime=mime, kind=kind,
                           width=att.get("w") or None, height=att.get("h") or None)

    async def _generate(self, account: Account, prompt: str, images: list[InputImage],
                        kind: str, timeout: float, on_progress, cancel) -> MediaResult:
        # Media always starts a fresh thread, so keep tabs holding a chat for chats.
        tab = await self._checkout(account, prefer=lambda t: not t.has_state)
        try:
            base = await self._prepare(tab, prompt, images)
            att = await self._wait_media(tab, base, kind, timeout, on_progress, cancel)
            return await self._download(tab, att, kind)
        except CDPError as exc:
            await self._close_tab(tab)
            raise UpstreamError(f"browser error: {exc}") from exc
        finally:
            await self._checkin(tab)

    async def generate_image(self, account: Account, req: ImageRequest) -> list[MediaResult]:
        prompt = self._media_prompt(req.prompt, "image", req.size)
        results = []
        for _ in range(max(1, req.n)):
            r = await self._generate(account, prompt, req.reference_images, "image",
                                     req.timeout, req.on_progress, req.cancel)
            r.revised_prompt = req.prompt
            results.append(r)
        return results

    async def generate_video(self, account: Account, req: VideoRequest) -> MediaResult:
        images = [req.first_frame] if req.first_frame else []
        if req.duration and req.duration > 10:
            num_scenes = min(6, max(2, (req.duration + 9) // 10))
            return await self._generate_multi_scene_video(account, req, images, num_scenes)
        prompt = self._media_prompt(req.prompt, "video", req.size, req.duration, has_ref=bool(images))
        res = await self._generate(account, prompt, images, "video", req.timeout,
                                   req.on_progress, req.cancel)
        return await self._upscale_video_1080p(res)

    async def _generate_multi_scene_video(self, account: Account, req: VideoRequest,
                                          images: list[InputImage], num_scenes: int) -> MediaResult:
        import re
        ratio = req.size or "16:9"
        char_m = re.search(r"\[CHARACTER_REF\]\s*:\s*([^\n]+)", req.prompt)
        char_ref = char_m.group(1).strip() if char_m else req.prompt
        v1_m = re.search(r"\[V1_VOICE\]\s*:\s*([^\n]+)", req.prompt)
        v2_m = re.search(r"\[V2_VOICE\]\s*:\s*([^\n]+)", req.prompt)
        v1_voice = v1_m.group(1).strip() if v1_m else (
            "animated comical high-pitched goofy Northern Hanoi Vietnamese male cartoon voice "
            "(giọng hoạt hình nam hài hước miền Bắc Hà Nội, cao lém lỉnh ~250Hz, phát âm chuẩn rõ dấu tiếng Việt)"
        )
        v2_voice = v2_m.group(1).strip() if v2_m else (
            "animated feisty raspy lower-pitched Northern Hanoi Vietnamese male cartoon voice "
            "(giọng hoạt hình nam cáu kỉnh hài hước miền Bắc Hà Nội, trầm khàn ~170Hz, phát âm chuẩn rõ dấu tiếng Việt)"
        )
        scene_actions: dict[int, str] = {}
        for m in re.finditer(r"\[SCENE_(\d+)_ACTION\]\s*:\s*([^\n]+)", req.prompt):
            scene_actions[int(m.group(1))] = m.group(2).strip()
        for m in re.finditer(r"\[SCENE_(\d+)_(V1|V2)\]\s*:\s*([^\n]+)", req.prompt):
            s_i = int(m.group(1))
            if m.group(2) == "V1":
                spk_rule = (
                    "ONLY the LEFT character opens and moves its mouth while speaking "
                    "in Vietnamese using VOICE_PROFILE_V1 (the RIGHT character MUST keep its mouth completely "
                    "closed and silent, reacting only with facial expressions)"
                )
            else:
                spk_rule = (
                    "ONLY the RIGHT character opens and moves its mouth while speaking "
                    "in Vietnamese using VOICE_PROFILE_V2 (the LEFT character MUST keep its mouth completely "
                    "closed and silent, reacting only with facial expressions)"
                )
            line = m.group(3).strip()
            scene_actions[s_i] = scene_actions.get(s_i, "") + f' {spk_rule}: "{line}"'

        verbatim_context = (
            f"Context (include this exact Context block verbatim inside the prompt text passed to media.generate_video; "
            f"keep all UTF-8 Vietnamese tone marks intact and never strip diacritics):\n"
            f"- Characters & Setting: {char_ref}\n"
            f"- Left Character Voice (VOICE_PROFILE_V1): {v1_voice}, exact same voice across all scenes.\n"
            f"- Right Character Voice (VOICE_PROFILE_V2): {v2_voice}, exact same voice across all scenes.\n"
            f"- Turn Rule: Only one character speaks per scene; the non-speaking character keeps its mouth completely closed."
        )

        tab = await self._checkout(account, prefer=lambda t: not t.has_state)
        seen_srcs: set[str] = set()
        clips: list[MediaResult] = []
        try:
            for idx in range(1, num_scenes + 1):
                if req.cancel and req.cancel.is_set():
                    raise asyncio.CancelledError
                t_start, t_end = (idx - 1) * 10, idx * 10
                s_act = scene_actions.get(idx) or f"Part {idx} ({t_start}s-{t_end}s) of: {req.prompt}"
                if idx == 1:
                    if images:
                        scene_prompt = (
                            f"Call media.generate_video ONCE passing the attached reference image (kind='image') "
                            f"and the following text verbatim in prompt (kind='text') to animate Scene 1 "
                            f"(10 seconds, {ratio}, zero motion blur), then attach the video in chat:\n"
                            f"Scene 1 Action: {s_act}\n"
                            f"{verbatim_context}"
                        )
                    else:
                        scene_prompt = (
                            f"Execute these 2 steps in order:\n"
                            f"1. Call media.generate_image (output_format='png', name='master_keyframe') ONCE to "
                            f"create a razor-sharp, photorealistic {ratio} master reference PNG of: {char_ref}\n"
                            f"2. Immediately call media.generate_video ONCE passing that master_keyframe.png "
                            f"(kind='image') AND the following text verbatim (kind='text') to animate Scene 1 "
                            f"(10 seconds, {ratio}, zero motion blur), then attach the video in chat:\n"
                            f"Scene 1 Action: {s_act}\n"
                            f"{verbatim_context}"
                        )
                    base = await self._prepare(tab, scene_prompt, images)
                else:
                    # Wait for any lingering generation status on the same tab to clear
                    for _ in range(20):
                        st_now = await self._state(tab)
                        if not st_now.get("generating"):
                            break
                        await asyncio.sleep(0.5)
                    await asyncio.sleep(1.0)
                    base = await self._state(tab)
                    scene_prompt = (
                        f"Now generate Scene {idx} of {num_scenes} (10 seconds, {ratio}) on this exact thread.\n"
                        f"IMPORTANT TOOL CALL RULES FOR media.generate_video:\n"
                        f"1. Set resume_from_snapshot_id to the snapshot_id returned by Scene {idx - 1}.\n"
                        f"2. Do NOT pass any kind='image' entry in prompt (re-passing the still image overrides the "
                        f"snapshot continuity); pass ONLY a single kind='text' entry.\n"
                        f"3. In that kind='text' entry, copy BOTH the Scene {idx} Action AND the full Context block "
                        f"below verbatim (do not shorten or paraphrase) so the stateless video subagent keeps the "
                        f"exact same characters and voices:\n\n"
                        f"Scene {idx} Action: {s_act}\n"
                        f"{verbatim_context}\n\n"
                        f"Attach Scene {idx}'s video in chat."
                    )
                    await self._send(tab, scene_prompt)

                def _scene_prog(p: int, scene_i: int = idx) -> None:
                    if req.on_progress:
                        overall = int(((scene_i - 1) + (p / 100.0)) / num_scenes * 92)
                        req.on_progress(min(92, max(1, overall)))

                log.info("waiting for multi-scene video %d/%d on account %s", idx, num_scenes, account.id)
                att = await self._wait_media(
                    tab, base, "video", req.timeout, _scene_prog, req.cancel, seen_srcs=seen_srcs
                )
                seen_srcs.add(att["src"])
                clip = await self._download(tab, att, "video")
                clips.append(clip)
                log.info("downloaded scene %d/%d (%d bytes)", idx, num_scenes, len(clip.data))
                if req.on_progress:
                    req.on_progress(int(idx / num_scenes * 92))
        except CDPError as exc:
            await self._close_tab(tab)
            raise UpstreamError(f"browser error: {exc}") from exc
        finally:
            self._forget(tab)
            await self._checkin(tab)

        return await self._concat_and_upscale_1080p(clips, req.prompt, req.on_progress)

    @staticmethod
    async def _concat_and_upscale_1080p(clips: list[MediaResult], prompt: str = "",
                                        on_progress=None) -> MediaResult:
        import os
        import shutil
        import tempfile
        if not clips:
            raise UpstreamError("no video clips generated")
        if len(clips) == 1:
            return await BrowserDriver._upscale_video_1080p(clips[0])
        if not shutil.which("ffmpeg"):
            return clips[-1]

        first = clips[0]
        portrait = bool(first.width and first.height and first.height > first.width)
        target_w, target_h = (1080, 1920) if portrait else (1920, 1080)

        with tempfile.TemporaryDirectory(prefix="m2a_multi_") as tmpdir:
            norm_paths: list[str] = []
            vf = (
                f"scale={target_w}:{target_h}:flags=lanczos+accurate_rnd+full_chroma_int,"
                f"cas=0.7,unsharp=5:5:0.8:3:3:0.4,fps=24,format=yuv420p,setsar=1"
            )

            os.makedirs("/Users/ducanh/Downloads/muse2api/data/media/last_scenes", exist_ok=True)
            for i, clip in enumerate(clips, start=1):
                raw_path = os.path.join(tmpdir, f"raw_{i}.mp4")
                norm_path = os.path.join(tmpdir, f"norm_{i}.mp4")
                with open(raw_path, "wb") as f:
                    f.write(clip.data)
                with open(f"/Users/ducanh/Downloads/muse2api/data/media/last_scenes/scene_{i}.mp4", "wb") as f_bak:
                    f_bak.write(clip.data)

                # Keep 100% pure un-warped Muse AI native audio stream
                pr_a = await asyncio.create_subprocess_exec(
                    "ffprobe", "-v", "error", "-select_streams", "a",
                    "-show_entries", "stream=index", "-of", "csv=p=0", raw_path,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                )
                has_a_out, _ = await pr_a.communicate()
                has_orig_audio = bool(has_a_out.decode().strip())

                if has_orig_audio:
                    fc = f"[0:v]{vf}[v];[0:a]aformat=sample_rates=48000:channel_layouts=stereo[a]"
                    cmd = [
                        "ffmpeg", "-y", "-i", raw_path,
                        "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "17",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", norm_path
                    ]
                else:
                    cmd = [
                        "ffmpeg", "-y", "-i", raw_path, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                        "-vf", vf, "-map", "0:v", "-map", "1:a", "-shortest",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "17",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", norm_path
                    ]

                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await proc.wait()
                if proc.returncode == 0 and os.path.exists(norm_path):
                    norm_paths.append(norm_path)
                else:
                    norm_paths.append(raw_path)

            concat_list = os.path.join(tmpdir, "concat.txt")
            with open(concat_list, "w", encoding="utf-8") as f:
                for p in norm_paths:
                    f.write(f"file '{p}'\n")

            final_path = os.path.join(tmpdir, "final_stitched.mp4")
            proc_cat = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
                "-c", "copy", "-movflags", "+faststart", final_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc_cat.wait()
            if on_progress:
                on_progress(99)
            if proc_cat.returncode == 0 and os.path.exists(final_path):
                with open(final_path, "rb") as f:
                    data = f.read()
                return MediaResult(data=data, mime="video/mp4", kind="video",
                                   width=target_w, height=target_h)
        return clips[-1]

    @staticmethod
    async def _upscale_video_1080p(res: MediaResult) -> MediaResult:
        import os
        import shutil
        import tempfile
        if not shutil.which("ffmpeg"):
            return res
        portrait = bool(res.width and res.height and res.height > res.width)
        target_w, target_h = (1080, 1920) if portrait else (1920, 1080)
        with tempfile.TemporaryDirectory(prefix="m2a_up_") as tmpdir:
            in_path = os.path.join(tmpdir, "in.mp4")
            out_path = os.path.join(tmpdir, "out.mp4")
            with open(in_path, "wb") as f:
                f.write(res.data)
            vf = (
                f"scale={target_w}:{target_h}:flags=lanczos+accurate_rnd+full_chroma_int,"
                f"cas=0.7,unsharp=5:5:0.8:3:3:0.4,format=yuv420p,setsar=1"
            )
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", in_path,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "17",
                "-maxrate", "14M", "-bufsize", "28M",
                "-c:a", "copy", out_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0 and os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    res.data = f.read()
                res.width, res.height = target_w, target_h
        return res

    # ------------------------------------------------------------ session
    async def renew_session(self, account: Account) -> SessionInfo:
        info = await muse.renew_session(account.cookies)
        # Retire the account's tabs so new ones load the rotated cookies; a tab
        # in the middle of a request is closed when it is checked back in.
        async with self._tab_cond:
            tabs = list(self._tabs.get(account.id, []))
            for tab in tabs:
                tab.stale = True
            idle = [t for t in tabs if not t.busy]
        for tab in idle:
            await self._close_tab(tab)
        return info

    async def login_with_email(self, email: str, mail_url: str = "") -> SessionInfo:
        """Automated headless login using email + TinyHost OTP (zero manual cookies)."""
        email, mail_url = muse.parse_account_string("", email=email, mail_url=mail_url)
        if not email or "@" not in email:
            raise UpstreamAuthError("valid email address is required for auto-login")
        since_id = await muse.get_latest_tinyhost_id(email) if "tinyhost.shop" in mail_url else 0
        assert self._chromium
        browser = await self._browser_session()
        ctx_res = await browser.send("Target.createBrowserContext", {"disposeOnDetach": True})
        context_id = ctx_res["browserContextId"]
        target_id = ""
        mail_target_id = ""
        session: CDPSession | None = None
        mail_session: CDPSession | None = None
        try:
            t_res = await browser.send(
                "Target.createTarget",
                {"url": muse.SITE, "browserContextId": context_id, "newWindow": True},
            )
            target_id = t_res["targetId"]
            session = await CDPSession.connect(
                f"ws://127.0.0.1:{self._chromium.port}/devtools/page/{target_id}"
            )
            for domain in ("Page", "Runtime", "Network"):
                await session.send(f"{domain}.enable")
            await session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            await session.send("Page.setWebLifecycleState", {"state": "active"})

            existing_codes: set[str] = set()
            if mail_url and "tinyhost.shop" not in mail_url:
                mt_res = await browser.send(
                    "Target.createTarget",
                    {"url": mail_url, "browserContextId": context_id, "newWindow": False},
                )
                mail_target_id = mt_res["targetId"]
                mail_session = await CDPSession.connect(
                    f"ws://127.0.0.1:{self._chromium.port}/devtools/page/{mail_target_id}"
                )
                for domain in ("Page", "Runtime"):
                    await mail_session.send(f"{domain}.enable")
                await asyncio.sleep(2.0)
                pre_codes = await mail_session.evaluate(
                    """(() => {
                      const txt = document.body ? document.body.innerText : '';
                      return (txt.match(/\\b\\d{6}\\b/g) || []);
                    })()"""
                )
                if isinstance(pre_codes, list):
                    existing_codes.update(str(c) for c in pre_codes)

            # 1. Wait for email input and submit email
            deadline = time.monotonic() + 30.0
            email_submitted = False
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                step = await session.evaluate(
                    f"""(() => {{
                      const body = document.body ? document.body.innerText : '';
                      if (/enter your code|6-digit/i.test(body)) return 'otp_screen';
                      let inp = document.querySelector('form input:not([type="checkbox"]):not([type="hidden"])')
                                || document.querySelector('input[type="email"], input[type="text"]');
                      if (!inp) {{
                        const btn = [...document.querySelectorAll('button,[role=button]')]
                          .find(b => /continue with email|log in|sign in/i.test(b.innerText || ''));
                        if (btn) {{ btn.click(); return 'clicked_login_btn'; }}
                        return 'waiting';
                      }}
                      inp.focus();
                      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                      setter.call(inp, {dom.q(email)});
                      inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                      inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                      const submitBtn = [...document.querySelectorAll('form button, button[type="submit"]')]
                        .find(b => !b.disabled && /continue|next|log in|submit/i.test((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')));
                      if (submitBtn) {{ submitBtn.click(); return 'submitted_btn'; }}
                      return 'filled_only';
                    }})()"""
                )
                if step == "otp_screen":
                    email_submitted = True
                    break
                if step in ("submitted_btn", "filled_only"):
                    for kind in ("keyDown", "keyUp"):
                        await session.send(
                            "Input.dispatchKeyEvent",
                            {"type": kind, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                             **({"text": "\r"} if kind == "keyDown" else {})},
                        )
                    email_submitted = True
                    await asyncio.sleep(1.5)
                    body_check = await session.evaluate("document.body ? document.body.innerText : ''") or ""
                    if "enter your code" in body_check.lower() or "6-digit" in body_check.lower():
                        break

            if not email_submitted:
                raise UpstreamAuthError("could not find email login form on muse.ai")

            # 2. Wait for OTP screen
            otp_deadline = time.monotonic() + 20.0
            while time.monotonic() < otp_deadline:
                body_text = (await session.evaluate("document.body ? document.body.innerText : ''") or "").lower()
                if "enter your code" in body_text or "6-digit" in body_text or "sent to" in body_text:
                    break
                await asyncio.sleep(0.5)

            # 3. Fetch 6-digit OTP from TinyHost API or open webmail tab (e.g. generator.email)
            otp = ""
            if mail_session is None:
                log.info("waiting for OTP code for %s from TinyHost (since_id=%d)...", email, since_id)
                otp = await muse.fetch_tinyhost_otp(email, since_id=since_id, timeout=65.0)
                log.info("received OTP code for %s: %s", email, otp)
            else:
                log.info("waiting for OTP code for %s from webmail tab (%s)...", email, mail_url)
                webmail_deadline = time.monotonic() + 180.0
                last_reload = time.monotonic()
                while time.monotonic() < webmail_deadline:
                    await asyncio.sleep(1.5)
                    # Check if user already entered OTP or logged in manually on the muse.ai tab
                    already_past_otp = await session.evaluate(
                        """(() => {
                          const body = (document.body ? document.body.innerText : '').toLowerCase();
                          if (body.includes('confirm you')) return true;
                          if (!body.includes('enter your code') && !body.includes('6-digit') &&
                              document.querySelector('textarea, [contenteditable="true"]')) return true;
                          return false;
                        })()"""
                    )
                    if already_past_otp:
                        log.info("user manually completed OTP step on muse.ai tab for %s", email)
                        break

                    # Poll webmail tab for a 6-digit verification code
                    found_codes = await mail_session.evaluate(
                        """(() => {
                          const txt = document.body ? document.body.innerText : '';
                          const prioritized = [];
                          const re = /(?:code|meta|muse|confirmation|security|login)[^0-9]{0,40}(\\d{6})\\b|\\b(\\d{6})[^0-9]{0,40}(?:is your|verification|code|meta)/gi;
                          let m;
                          while ((m = re.exec(txt)) !== null) {
                            prioritized.push(m[1] || m[2]);
                          }
                          const all = txt.match(/\\b\\d{6}\\b/g) || [];
                          return [...prioritized, ...all];
                        })()"""
                    )
                    if isinstance(found_codes, list):
                        for c in found_codes:
                            cs = str(c)
                            if cs not in existing_codes:
                                otp = cs
                                break
                    if otp:
                        log.info("received OTP code for %s from webmail tab: %s", email, otp)
                        break

                    if time.monotonic() - last_reload > 8.0:
                        mail_body = (await mail_session.evaluate("document.body ? document.body.innerText : ''") or "").lower()
                        if "remove the limit" not in mail_body and "access temporarily limited" not in mail_body:
                            with contextlib.suppress(Exception):
                                await mail_session.send("Page.reload", {"ignoreCache": False})
                        last_reload = time.monotonic()

            # 4. Enter OTP and submit (if we extracted otp)
            if otp:
                await session.send("Page.bringToFront")
                await session.evaluate(
                    """(() => {
                      const inp = document.querySelector('form input:not([type="checkbox"]):not([type="hidden"])')
                                  || document.querySelector('input');
                      if (inp) {
                        inp.focus();
                        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        setter.call(inp, '');
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                      }
                    })()"""
                )
                await session.send("Input.insertText", {"text": otp})
                await asyncio.sleep(0.4)
                await session.evaluate(
                    f"""(() => {{
                      const inp = document.querySelector('form input:not([type="checkbox"]):not([type="hidden"])')
                                  || document.querySelector('input');
                      if (inp && inp.value !== {dom.q(otp)}) {{
                        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        setter.call(inp, {dom.q(otp)});
                        inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                      }}
                      const nextBtn = [...document.querySelectorAll('form button, button[type="submit"]')]
                        .find(b => !b.disabled && /next|continue|verify|log in|submit/i.test(b.innerText || ''));
                      if (nextBtn) nextBtn.click();
                    }})()"""
                )
                for kind in ("keyDown", "keyUp"):
                    await session.send(
                        "Input.dispatchKeyEvent",
                        {"type": kind, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                         **({"text": "\r"} if kind == "keyDown" else {})},
                    )

            # 5. Wait for session cookies to be issued (up to 300s if human verification checkpoint appears)
            cookie_deadline = time.monotonic() + 300.0
            cookies: dict[str, str] = {}
            expires: dict[str, int] = {}
            while time.monotonic() < cookie_deadline:
                await asyncio.sleep(1.0)
                c_res = await session.send("Network.getCookies", {"urls": ["https://muse.ai", "https://www.muse.ai"]})
                for c in c_res.get("cookies", []):
                    name, val = c.get("name"), c.get("value")
                    if name and val:
                        cookies[name] = val
                        if c.get("expires") and c["expires"] > 0:
                            expires[name] = int(c["expires"])
                logged_in_dom = await session.evaluate(
                    """(() => {
                      const host = location.hostname || '';
                      if (!host.endsWith('muse.ai')) return false;
                      const body = (document.body ? document.body.innerText : '').toLowerCase();
                      if (body.includes('enter your code') || body.includes('6-digit') || body.includes('confirm you')) return false;
                      return Boolean(document.querySelector('textarea, [contenteditable="true"]'));
                    })()"""
                )
                if logged_in_dom and cookies.get("hatch_sess"):
                    await asyncio.sleep(1.5)
                    c_res2 = await session.send("Network.getCookies", {"urls": ["https://muse.ai", "https://www.muse.ai"]})
                    for c in c_res2.get("cookies", []):
                        name, val = c.get("name"), c.get("value")
                        if name and val:
                            cookies[name] = val
                            if c.get("expires") and c["expires"] > 0:
                                expires[name] = int(c["expires"])
                    break

            if missing := muse.missing_session_cookies(cookies):
                raise UpstreamAuthError(f"OTP submitted but session cookies still missing: {', '.join(missing)}")

            try:
                info = await muse.renew_session(cookies, wake_vm=True)
                for k, v in cookies.items():
                    info.cookies.setdefault(k, v)
                for k, v in expires.items():
                    info.cookie_expires.setdefault(k, v)
                return info
            except Exception:
                return SessionInfo(ok=True, cookies=cookies, cookie_expires=expires, meta={"email": email})
        finally:
            if mail_session:
                with contextlib.suppress(Exception):
                    await mail_session.close()
            if mail_target_id:
                with contextlib.suppress(Exception):
                    await browser.send("Target.closeTarget", {"targetId": mail_target_id})
            if session:
                with contextlib.suppress(Exception):
                    await session.close()
            if target_id:
                with contextlib.suppress(Exception):
                    await browser.send("Target.closeTarget", {"targetId": target_id})
            with contextlib.suppress(Exception):
                await browser.send("Target.disposeBrowserContext", {"browserContextId": context_id})
