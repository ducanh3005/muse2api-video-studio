# 🛠️ Developer Guide & Software Integration Manual (`muse2api-video-studio`)

This guide explains how to integrate **`muse2api-video-studio`** into your own desktop application, SaaS backend, or automated content studio pipeline.

---

## 1. End-to-End Architecture

`muse2api-video-studio` exposes a standard OpenAI-compatible HTTP API on `http://127.0.0.1:18610/v1` while managing a pool of authenticated Muse AI cloud sessions under the hood:

1. **Zero-Cookie Account Pool (`src/muse2api/accounts/pool.py` & `src/muse2api/upstream/muse.py`):**
   - Accepts accounts in `"email@domain | https://tinyhost.shop/email@domain"` format via `POST /admin/accounts/login`.
   - Automatically polls the TinyHost JSON API (`https://tinyhost.shop/api/email/{domain}/{user}/`) to extract the 6-digit OTP verification code and completes headless Chromium login.
   - Runs a background keep-alive loop (`src/muse2api/accounts/keepalive.py`) that refreshes session cookies automatically.

2. **Headless Browser CDP Engine (`src/muse2api/drivers/browser/driver.py`):**
   - **Single-Scene 10s Video (`duration <= 10`):** Sends a structured prompt to `media.generate_video`, waits for the virtualized DOM attachment to finish rendering, downloads the native MP4 with audio, and upscales it to `1920x1080` (`24fps`, `H.264 CRF 17`) using FFmpeg `lanczos + cas=0.7 + unsharp`.
   - **Multi-Scene 60s Storyboard (`duration > 10`):**
     1. Generates `master_keyframe.png` (`media.generate_image`) from `[CHARACTER_REF]` so both characters and the background environment are locked visually.
     2. Generates Scene 1 (`0s–10s`) from `master_keyframe.png` + `[SCENE_1_ACTION]` + `[SCENE_1_V1/V2]`.
     3. Generates Scenes 2–6 (`10s–60s`) on the same thread using `resume_from_snapshot_id` so each scene continues seamlessly from the exact end state of the previous scene.
     4. Saves individual scene clips (`data/media/last_scenes/scene_1..6.mp4`) and concatenates + upscales the full 60s `1920x1080` master video.

3. **Neural Multi-Scene Voice Synchronization (`scripts/sync_voices_seed_vc.py`):**
   - **Stage 1 (`HTDemucs`):** Separates speech (`vocals.wav`) from background sound effects and environmental audio (`no_vocals.wav`).
   - **Stage 2 (`Seed-VC 44.1kHz F0-Conditioned`):** Uses `DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth` (`Whisper-small` semantic extractor + `RMVPE` neural pitch tracker + `CAMPPlus` speaker verification embedding + `NVIDIA BigVGAN v2 44.1kHz`) to clone Character 1's reference voice across odd scenes (`1, 3, 5`) and Character 2's reference voice across even scenes (`2, 4, 6`).
   - **Stage 3 (`Cepstral Formant Envelope Matching`):** Aligns the smoothed STFT spectral envelope (`F1–F4` vocal tract formants) of each converted vocal to the reference vocal and mixes back `no_vocals.wav` so all sound effects remain intact.

---

## 2. Prompt Engineering Best Practices for Talking Animals & Skits

### Rule 1: Single Speaker Turn-Taking (Prevent Simultaneous Mouth Movement)
When two characters appear in the same frame, always explicitly specify which character moves its mouth and which character keeps its mouth closed:

```text
ONLY the <Left/Right Character> opens and moves its mouth, speaking in <Voice Description>: "<Dialogue>" while the <Other Character> keeps its mouth completely closed, reacting only with facial expressions.
```

### Rule 2: Acoustic Voice Profile Anchoring
Include pitch and timbre descriptors in every prompt (`e.g., witty British male detective voice ~160Hz` or `high-energy American gym coach voice ~240Hz`) so the native audio generator starts close to the target vocal register.

### Rule 3: Cartoon Comedy Visual Phrasing
To pass upstream safety filters on slapstick comedy (e.g., *Larva*-style sewer gags), describe effects in family-friendly animation terms:
- Instead of crude terms, use: *"playfully wiggles its tail and releases a comical cartoon puff of glowing green aroma cloud with a funny brassy toot sound effect"* or *"exhales a comical cartoon wave of visible green garlic-breath mist"*.

---

## 3. REST API Reference

### `POST /admin/accounts/login`
Registers and authenticates a Muse AI account using email + TinyHost OTP.

**Request:**
```json
{
  "account": "lucas8709c@ccpro.click | https://tinyhost.shop/lucas8709c@ccpro.click",
  "label": "worker-01"
}
```

### `POST /v1/videos`
Submits an asynchronous video generation task (`10s` single-scene or `20s–60s` multi-scene).

**Request:**
```json
{
  "model": "muse-video",
  "prompt": "3D Pixar style animated comedy skit...",
  "size": "16:9",
  "duration": 10
}
```

**Response:**
```json
{
  "id": "task_9c029b765d29423c",
  "object": "video",
  "status": "queued",
  "progress": 0,
  "created_at": 1793672974,
  "model": "muse-video"
}
```

### `GET /v1/videos/{task_id}`
Polls task progress (`0%` -> `100%`) and returns the `1920x1080` MP4 download URL when `status == "succeeded"`:

```json
{
  "id": "task_9c029b765d29423c",
  "object": "video",
  "status": "succeeded",
  "progress": 100,
  "result": {
    "url": "http://127.0.0.1:18610/v1/media/vid_007331a473ae4b25.mp4",
    "mime": "video/mp4",
    "width": 1920,
    "height": 1080
  }
}
```
