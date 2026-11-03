#!/usr/bin/env python3
"""
Batch Generator for 10-Second English Talking-Animal Comedy Skits (1920x1080 Full HD)
-------------------------------------------------------------------------------------
Submits 10s single-scene comedy skits to the local `muse2api` server (`POST /v1/videos`),
downloads the 1080p MP4s, and automatically exports 640px animated GIF previews + posters.
"""

import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

API_BASE = "http://127.0.0.1:18610"
HEADERS = {
    "Authorization": "Bearer sk-muse-local",
    "Content-Type": "application/json",
}

ENGLISH_SKIT_PRESETS = [
    {
        "slug": "skit_1_detective_cat_dog_10s",
        "title": "The Birthday Cake Interrogation (Detective Cat & Guilty Golden Retriever)",
        "prompt": (
            "3D Pixar and Zootopia style animated comedy skit, 16:9 widescreen, 8K cinema clarity. "
            "In a bright modern kitchen beside a half-eaten strawberry birthday cake, a chubby British Shorthair "
            "detective cat wearing a tiny plaid deerstalker hat and round glasses stands on the counter pointing "
            "a magnifying glass at a guilty Golden Retriever sitting below with pink frosting smeared all over his nose. "
            "ONLY the detective cat opens and moves his mouth, speaking in a dramatic, witty British male detective voice: "
            "\"Look me in the eyes, Barnaby! You expect me to believe the birthday cake jumped off the table and attacked your nose?!\" "
            "while the Golden Retriever keeps his mouth completely closed, wagging his tail nervously with wide guilty puppy eyes."
        ),
    },
    {
        "slug": "skit_2_gym_hamster_bear_10s",
        "title": "Personal Trainer Hamster & The Grizzly Bear",
        "prompt": (
            "3D Pixar and Zootopia style animated comedy skit, 16:9 widescreen, 8K cinema clarity. "
            "Inside a sunny modern fitness gym, a tiny chubby golden hamster wearing a miniature red sweatband and "
            "sport tank top stands on a bench pointing enthusiastically at a giant exhausted Grizzly Bear who is "
            "sweating heavily while struggling to curl tiny two-pound pink dumbbells. "
            "ONLY the tiny hamster opens and moves his mouth, yelling in a hilarious high-energy American gym coach voice: "
            "\"Come on Big Bruno, don't quit on me now! Those two-pound pink dumbbells aren't gonna lift themselves, lightweight baby!\" "
            "while the giant Grizzly Bear keeps his mouth closed, huffing and rolling his eyes in comic exhaustion."
        ),
    },
    {
        "slug": "skit_3_podcast_penguin_frog_10s",
        "title": "Late-Night Animal Gossip Podcast (Emperor Penguin & Sassy Tree Frog)",
        "prompt": (
            "3D Pixar and Sing style animated comedy skit, 16:9 widescreen, 8K cinema clarity. "
            "Inside a trendy neon-lit podcast studio with professional broadcast microphones on boom arms, "
            "on the left sits a dapper Emperor Penguin wearing a red bowtie and headphones who keeps his beak "
            "completely closed and nods in shocked amazement; on the right sits a sassy bright-green Tree Frog "
            "wearing studio headphones who gestures dramatically with her webbed hands while speaking into the mic "
            "in a sassy, expressive American female comedy voice: "
            "\"And I told her, honey, if that toad is still hopping into other lily pads at midnight, he is definitely not your prince!\""
        ),
    },
]


def generate_skit(preset: dict, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = preset["slug"]
    print(f"\n=== Submitting {slug}: {preset['title']} ===", flush=True)
    payload = json.dumps(
        {
            "model": "muse-video",
            "prompt": preset["prompt"],
            "size": "16:9",
            "duration": 10,
        }
    ).encode("utf-8")

    req = urllib.request.Request(f"{API_BASE}/v1/videos", data=payload, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req) as resp:
        task = json.loads(resp.read().decode("utf-8"))
    tid = task["id"]
    print(f"Task ID: {tid}", flush=True)

    url = f"{API_BASE}/v1/videos/{tid}"
    req_g = urllib.request.Request(url, headers={"Authorization": "Bearer sk-muse-local"})
    last_prog = -1
    for _ in range(240):
        with urllib.request.urlopen(req_g) as resp:
            st_data = json.loads(resp.read().decode("utf-8"))
        st = st_data.get("status")
        prog = st_data.get("progress", 0)
        if prog != last_prog or st in ("succeeded", "failed"):
            print(f"[{slug}] status={st} progress={prog}%", flush=True)
            last_prog = prog
        if st == "succeeded":
            media_url = st_data["result"]["url"]
            mp4_out = out_dir / f"{slug}.mp4"
            gif_out = out_dir / f"{slug}.gif"
            jpg_out = out_dir / f"{slug}.jpg"
            urllib.request.urlretrieve(media_url, str(mp4_out))
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(mp4_out),
                    "-vf", "fps=10,scale=640:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer",
                    str(gif_out),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", "2.5", "-i", str(mp4_out),
                    "-vf", "scale=960:-1", "-frames:v", "1", "-q:v", "2", str(jpg_out),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"[{slug}] Saved to {mp4_out}", flush=True)
            return st_data
        if st == "failed":
            raise RuntimeError(f"Failed {slug}: {st_data}")
        time.sleep(3)
    raise TimeoutError(f"Timed out waiting for {slug}")


if __name__ == "__main__":
    target_dir = Path(__file__).resolve().parent.parent / "docs" / "demos"
    for p in ENGLISH_SKIT_PRESETS:
        generate_skit(p, target_dir)
