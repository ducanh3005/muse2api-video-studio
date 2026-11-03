# Muse UA Marketing Video Studio (`/ua-studio`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete Mobile UA Marketing Video Generation Web Studio (`/ua-studio`) integrated into `muse2api` with 5-format Multi-Account Pool & Rotation management, App Store / Google Play + Screenshot App DNA analysis, Hook-First multi-script generation (`muse-chat`), Hook Keyframe generation (`muse-image`), 10s 1920x1080 Hook Video generation (`muse-video`), and automated Pillow + FFmpeg Dynamic App Endcard composition.

**Architecture:** Extends the existing FastAPI server (`src/muse2api/`) with a dedicated UA Marketing core helper module (`src/muse2api/core/ua_endcard.py` for multi-format account parsing, Store metadata scraping, JSON extraction, and Pillow/FFmpeg 1080p Endcard composition), a REST router (`src/muse2api/api/routes/ua.py` mounted under `/v1/ua`), and a single-file interactive Web Studio (`src/muse2api/web/ua_studio.html` served at `GET /ua-studio`).

**Tech Stack:** Python 3.12+, FastAPI, httpx, Pillow (`PIL`), FFmpeg (`subprocess`), HTML5/CSS3/Vanilla JS (`fetch` + SSE/polling), pytest.

## Global Constraints

- Server base URL defaults to same-origin (`http://127.0.0.1:18610`) with default local API key `sk-muse-local` pre-filled in `/ua-studio`.
- All video outputs and composed UA Endcards must be `1920x1080` (`16:9` widescreen), `24fps`, `H.264` (`yuv420p`) with `44.1kHz` stereo AAC audio so concatenation never fails or desyncs.
- All generated `video_prompt` strings must enforce the `muse2api` Single Speaker Turn-Taking and Acoustic Voice Profile rules (`ONLY the <Character> opens and moves its mouth, speaking in <Voice Profile>: "<Dialogue>" while other characters keep their mouths completely closed`).
- Multi-account batch import must auto-detect 5 formats: (1) Bearer/session token (`Bearer <token>` or `hatch_sess=<token>`), (2) Cookie header string, (3) cURL command, (4) JSON cookie array/object, and (5) Email + TinyHost/Generator.email OTP string (`email@domain | https://tinyhost.shop/email@domain`).

---

### Task 1: Multi-Format Account Parser, Store Scraper, JSON Extractor & Endcard Composer (`src/muse2api/core/ua_endcard.py`)

**Files:**
- Create: `src/muse2api/core/ua_endcard.py`
- Test: `tests/test_ua_core.py`

**Interfaces:**
- Consumes: `muse2api.core.media.MediaStore`, `muse2api.core.media.load_image_ref`
- Produces:
  - `parse_multi_account_input(raw_text: str) -> list[dict[str, Any]]`
  - `extract_json_payload(text: str) -> Any`
  - `scrape_store_metadata(store_url: str, timeout: float = 15.0) -> dict[str, Any]`
  - `render_endcard_image(app_name: str, headline: str, cta_text: str, rating: str = "4.9", icon_bytes: bytes | None = None, screenshot_bytes_list: list[bytes] | None = None, width: int = 1920, height: int = 1080) -> bytes`
  - `compose_ua_video_with_endcard(hook_video_path: Path, endcard_png_bytes: bytes, output_path: Path, endcard_duration: float = 3.5) -> Path`

- [ ] **Step 1: Write the failing test (`tests/test_ua_core.py`)**

```python
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from muse2api.core.ua_endcard import (
    compose_ua_video_with_endcard,
    extract_json_payload,
    parse_multi_account_input,
    render_endcard_image,
)


def test_parse_multi_account_input_all_formats():
    raw = """
    # 1. Email + TinyHost
    user1@ccpro.click | https://tinyhost.shop/user1@ccpro.click
    # 2. Bearer token with label
    vip_acc | Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.token123
    # 3. Cookie header string
    cookie_acc | hatch_sess=sess_abc_999; cf_clearance=cf_123
    # 4. cURL command
    curl 'https://muse.ai/api/session' -H 'Cookie: hatch_sess=curl_sess_777; foo=bar'
    """
    parsed = parse_multi_account_input(raw)
    assert len(parsed) == 4
    assert parsed[0]["mode"] == "email_otp"
    assert parsed[0]["email"] == "user1@ccpro.click"
    assert "tinyhost.shop" in parsed[0]["mail_url"]

    assert parsed[1]["mode"] == "cookies"
    assert parsed[1]["label"] == "vip_acc"
    assert parsed[1]["cookies"]["hatch_sess"].startswith("eyJhbGci")

    assert parsed[2]["mode"] == "cookies"
    assert parsed[2]["label"] == "cookie_acc"
    assert parsed[2]["cookies"]["hatch_sess"] == "sess_abc_999"

    assert parsed[3]["mode"] == "cookies"
    assert parsed[3]["cookies"]["hatch_sess"] == "curl_sess_777"


def test_parse_multi_account_json_cookie_array():
    raw = json.dumps([
        {"name": "hatch_sess", "value": "json_cookie_val_1", "expirationDate": 1890000000},
        {"name": "other", "value": "xyz"},
    ])
    parsed = parse_multi_account_input(raw)
    assert len(parsed) == 1
    assert parsed[0]["mode"] == "cookies"
    assert parsed[0]["cookies"]["hatch_sess"] == "json_cookie_val_1"


def test_extract_json_payload_handles_markdown_fences():
    sample = 'Here is your response:\n```json\n{"scripts": [{"id": "s1", "thumbstop_score": 9.6}]}\n```\nDone!'
    data = extract_json_payload(sample)
    assert isinstance(data, dict)
    assert data["scripts"][0]["id"] == "s1"


def test_render_endcard_image_and_compose_video(tmp_path: Path):
    icon_img = Image.new("RGBA", (256, 256), (255, 80, 120, 255))
    icon_buf = io.BytesIO()
    icon_img.save(icon_buf, format="PNG")

    shot_img = Image.new("RGB", (540, 960), (40, 90, 200))
    shot_buf = io.BytesIO()
    shot_img.save(shot_buf, format="PNG")

    png_bytes = render_endcard_image(
        app_name="Drama Puzzle: Secret Mansion",
        headline="Can You Uncover The Hidden Truth?",
        cta_text="Download Free Now",
        rating="4.9",
        icon_bytes=icon_buf.getvalue(),
        screenshot_bytes_list=[shot_buf.getvalue()],
    )
    rendered = Image.open(io.BytesIO(png_bytes))
    assert rendered.size == (1920, 1080)

    hook_mp4 = tmp_path / "hook_1s.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=1920x1080:r=24:d=1.0",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=1.0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(hook_mp4),
        ],
        check=True,
        capture_output=True,
    )
    out_mp4 = tmp_path / "full_ua.mp4"
    compose_ua_video_with_endcard(hook_mp4, png_bytes, out_mp4, endcard_duration=1.5)
    assert out_mp4.is_file()
    assert out_mp4.stat().st_size > 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ua_core.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'muse2api.core.ua_endcard'`

- [ ] **Step 3: Write implementation (`src/muse2api/core/ua_endcard.py`)**

Implement `parse_multi_account_input`, `extract_json_payload`, `scrape_store_metadata`, `render_endcard_image`, and `compose_ua_video_with_endcard` in `src/muse2api/core/ua_endcard.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_ua_core.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/muse2api/core/ua_endcard.py tests/test_ua_core.py
git commit -m "feat(ua-core): add multi-format account parser, store scraper, and 1080p FFmpeg endcard composer"
```

---

### Task 2: UA Marketing Backend API Routes (`src/muse2api/api/routes/ua.py`)

**Files:**
- Create: `src/muse2api/api/routes/ua.py`
- Modify: `src/muse2api/api/routes/__init__.py`
- Test: `tests/test_ua_api.py`

**Interfaces:**
- Consumes: `src/muse2api/core/ua_endcard.py`, `Services`, `AccountPool`, `Gateway`
- Produces REST endpoints under `/v1/ua`:
  - `GET /v1/ua/accounts/pool`
  - `POST /v1/ua/accounts/pool-config`
  - `POST /v1/ua/accounts/batch-import`
  - `POST /v1/ua/accounts/{account_id}/action`
  - `POST /v1/ua/analyze-app`
  - `POST /v1/ua/generate-scripts`
  - `POST /v1/ua/refine-script`
  - `POST /v1/ua/render-endcard`

- [ ] **Step 1: Write the failing test (`tests/test_ua_api.py`)**

```python
from __future__ import annotations

import base64
import io
from PIL import Image


def _sample_png_data_url() -> str:
    img = Image.new("RGB", (320, 180), (20, 120, 220))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_ua_accounts_pool_and_batch_import(client, auth):
    r0 = client.get("/v1/ua/accounts/pool", headers=auth)
    assert r0.status_code == 200
    assert "strategy" in r0.json()

    r1 = client.post(
        "/v1/ua/accounts/batch-import",
        headers=auth,
        json={
            "raw_text": "acc_token_1 | Bearer sess_tok_111\nacc_cookie_2 | hatch_sess=sess_tok_222",
            "verify_session": False,
        },
    )
    assert r1.status_code == 200
    body = r1.json()
    assert body["imported_count"] == 2

    r2 = client.post(
        "/v1/ua/accounts/pool-config",
        headers=auth,
        json={"strategy": "round_robin"},
    )
    assert r2.status_code == 200
    assert r2.json()["strategy"] == "round_robin"


def test_ua_analyze_app_and_generate_scripts(client, auth):
    r = client.post(
        "/v1/ua/analyze-app",
        headers=auth,
        json={
            "app_name": "Merge Drama: Makeover Story",
            "description": "Help heartbroken heroes renovate their mansion and uncover betrayal secrets!",
            "screenshots": [_sample_png_data_url()],
        },
    )
    assert r.status_code == 200
    dna = r.json()["app_dna"]
    assert "app_name" in dna
    assert "core_mechanic" in dna

    r_scripts = client.post(
        "/v1/ua/generate-scripts",
        headers=auth,
        json={
            "app_dna": dna,
            "archetypes": ["drama", "iq_fail"],
            "count": 2,
            "dialogue_language": "en",
        },
    )
    assert r_scripts.status_code == 200
    scripts = r_scripts.json()["scripts"]
    assert len(scripts) >= 2
    assert "hook_0_3s" in scripts[0]
    assert "image_prompt" in scripts[0]
    assert "video_prompt" in scripts[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ua_api.py -v`
Expected: FAIL with `404 Not Found` on `/v1/ua/accounts/pool`

- [ ] **Step 3: Write implementation (`src/muse2api/api/routes/ua.py` and register in `src/muse2api/api/routes/__init__.py`)**

Implement all `/v1/ua/*` endpoints with intelligent fallback script synthesis when upstream returns free-form text, full prompt engineering for `muse-chat`, `muse-image`, and `muse-video`, and register `ua` in `build_router()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_ua_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/muse2api/api/routes/ua.py src/muse2api/api/routes/__init__.py tests/test_ua_api.py
git commit -m "feat(ua-api): add /v1/ua endpoints for multi-account rotation, app DNA analysis, hook scripts, and endcard rendering"
```

---

### Task 3: UA Marketing Video Studio Web UI (`src/muse2api/web/ua_studio.html` & `GET /ua-studio`)

**Files:**
- Create: `src/muse2api/web/ua_studio.html`
- Modify: `src/muse2api/api/routes/pages.py`
- Test: `tests/test_pages.py`

**Interfaces:**
- Consumes: `/v1/ua/*`, `/v1/images/generations`, `/v1/videos`, `/v1/videos/{task_id}`, `/v1/chat/completions`
- Produces: `GET /ua-studio` serving the interactive UA Marketing Video Studio web app.

- [ ] **Step 1: Add test for `/ua-studio` in `tests/test_pages.py`**

Verify `GET /ua-studio` returns HTTP 200 and includes the Multi-Account Pool manager, App Link / Screenshot analyzer, Hook-First Script Matrix, and Endcard controls.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_pages.py -v`
Expected: FAIL with `404 Not Found` on `/ua-studio`

- [ ] **Step 3: Implement `src/muse2api/web/ua_studio.html` and route in `src/muse2api/api/routes/pages.py`**

Build the complete studio interface with:
1. Top Control Bar: API Key input (default `sk-muse-local`), Live Account Pool & Strategy selector (`Round-Robin`, `LRU`, `Affinity`), Multi-Account Import Modal (supports 5 login formats with 1-click import, renew, reset, delete), and Batch Generate buttons (`🎨 Tạo tất cả Ảnh Hook`, `🎬 Render tất cả Video`).
2. Left Sidebar: Store URL input (Google Play / App Store) + 1-click preset demo apps + Drag & Drop App Icon/Screenshots uploader + Editable App DNA card + UA Hook Archetype selector (`🔥 Drama`, `🤯 IQ 99% Fail`, `😱 Giải cứu khẩn cấp`, `✨ Satisfying / Biến hình`, `🎙️ POV / Bóc phốt`, `🐾 Pixar Thú cưng`) + Dialogue language (`English` / `Tiếng Việt`) & Script count.
3. Main Workspace: Grid of Hook-First Script Cards with:
   - Highlighted **0–3s Thumbstop Hook Box** (Visual Shock + Spoken Hook Line + On-Screen Text Banner + Thumbstop Score badge).
   - 1-click AI Hook Refiners (`⚡ Đổi Hook sốc hơn`, `🎭 Đổi sang Drama bắt quả tang`, `😂 Đổi sang hài hước`).
   - Editable `image_prompt` and `video_prompt`.
   - Hook Keyframe (`muse-image`) generator & preview + custom image upload override.
   - 10s Hook Video (`muse-video`) generator with live progress bar + automatic or 1-click **Dynamic App Endcard (`Pillow + FFmpeg`)** composer producing the full `13.5s` `1920x1080` UA Ad MP4 alongside the raw `10s` Hook MP4.
4. Slide-over / Bottom Drawer: **UA Strategy Chat Copilot (`muse-chat`)** aware of the active App DNA and scripts, with a button to convert any chat idea into a new Script Card.

- [ ] **Step 4: Run full test suite to verify everything passes**

Run: `.venv/bin/pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/muse2api/web/ua_studio.html src/muse2api/api/routes/pages.py tests/test_pages.py
git commit -m "feat(ua-studio): add /ua-studio web application for UA Marketing video generation"
```
