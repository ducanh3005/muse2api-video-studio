"""Facts about the muse.ai web app shared by all drivers.

Everything here was observed from the public web client and may change without
notice. Keep upstream-specific constants in this module so that a site update
is a one-file fix.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid

import httpx

from ..drivers.base import SessionInfo
from ..errors import UpstreamAuthError, UpstreamError

SITE = "https://muse.ai"
NEW_THREAD_URL = f"{SITE}/thread/new"
SESSION_API = f"{SITE}/api/session"
VM_WAKE_API = f"{SITE}/api/hatch/vm/wake"

# Cookies that together make up a logged-in session.
SESSION_COOKIES = ("hatch_sess",)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def missing_session_cookies(cookies: dict[str, str]) -> list[str]:
    return [name for name in SESSION_COOKIES if not cookies.get(name)]


async def renew_session(
    cookies: dict[str, str], *, wake_vm: bool = True, timeout: float = 15.0
) -> SessionInfo:
    """Hit the session endpoint, which rotates short-lived cookies via Set-Cookie,
    and optionally wake the account's cloud workspace so the first request is fast."""
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": SITE,
        "Referer": NEW_THREAD_URL,
        "Accept": "application/json",
    }
    jar = dict(cookies)
    expires: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        resp = await client.get(SESSION_API, headers={"Cookie": cookie_header(jar)})
        if resp.status_code in (401, 403):
            raise UpstreamAuthError(f"session rejected by upstream (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise UpstreamError(f"session endpoint returned HTTP {resp.status_code}")

        for c in resp.cookies.jar:
            if c.value:
                jar[c.name] = c.value
            if c.expires:
                expires[c.name] = int(c.expires)

        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        meta = {"session_status": body.get("status"), "vm_id": body.get("vm_id"),
                "vm_state": body.get("vm_state")}

        if wake_vm and meta["vm_id"] and meta["vm_state"] != "DISABLED":
            try:
                w = await client.post(
                    VM_WAKE_API,
                    headers={"Cookie": cookie_header(jar)},
                    json={"vm_id": meta["vm_id"], "retry_count": 0,
                          "connect_attempt_id": str(uuid.uuid4())},
                )
                meta["vm_wake_ok"] = w.status_code == 200
            except httpx.HTTPError:
                meta["vm_wake_ok"] = False

    return SessionInfo(ok=meta["session_status"] == "assigned", cookies=jar,
                       cookie_expires=expires, meta=meta)


def parse_account_string(raw: str, email: str = "", mail_url: str = "") -> tuple[str, str]:
    """Parse 'user@domain | https://tinyhost.shop/user@domain' or plain email."""
    if raw and "|" in raw:
        parts = [p.strip() for p in raw.split("|", 1)]
        email = email or parts[0]
        mail_url = mail_url or parts[1]
    elif raw and "@" in raw and not email:
        email = raw.strip()
    email = email.strip()
    if not mail_url and "@" in email:
        mail_url = f"https://tinyhost.shop/{email}"
    return email, mail_url


async def get_latest_tinyhost_id(email: str, timeout: float = 10.0) -> int:
    """Return the newest message ID currently in the TinyHost inbox for email."""
    if "@" not in email:
        return 0
    user, domain = email.strip().split("@", 1)
    url = f"https://tinyhost.shop/api/email/{domain}/{user}/?page=1&limit=5"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return 0
            emails = resp.json().get("emails") or []
            return max((int(e.get("id") or 0) for e in emails), default=0)
    except Exception:
        return 0


async def fetch_tinyhost_otp(email: str, since_id: int = 0, timeout: float = 65.0) -> str:
    """Poll TinyHost JSON API until a new 6-digit Meta login OTP code arrives."""
    if "@" not in email:
        raise UpstreamAuthError(f"invalid email address: {email}")
    user, domain = email.strip().split("@", 1)
    url = f"https://tinyhost.shop/api/email/{domain}/{user}/?page=1&limit=5"
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=10.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    emails = resp.json().get("emails") or []
                    for item in emails:
                        msg_id = int(item.get("id") or 0)
                        if msg_id <= since_id:
                            continue
                        subj = str(item.get("subject") or "")
                        body = str(item.get("body") or "") + " " + str(item.get("html_body") or "")
                        m = re.search(r"\b(\d{6})\b", subj) or re.search(
                            r"(?:login|confirmation|security)\s+code[^0-9]*(\d{6})\b", body, re.I
                        )
                        if m:
                            return m.group(1)
            except Exception:
                pass
            await asyncio.sleep(2.0)
    raise UpstreamAuthError(f"timed out waiting for OTP email at {url}")

