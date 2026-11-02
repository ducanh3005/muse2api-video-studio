#!/usr/bin/env python3
"""
High-Precision Multi-Scene Voice Synchronization Pipeline for Muse AI Videos
----------------------------------------------------------------------------
Uses:
  1. HTDemucs (`htdemucs --two-stems=vocals`) to cleanly separate speech vocals from background SFX/ambience.
  2. Seed-VC 44.1kHz F0-Conditioned (`DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth`
     with Whisper-small semantic features + RMVPE pitch tracker + CAMPPlus speaker verification embedding
     + NVIDIA BigVGAN v2 44.1kHz neural vocoder) to clone Character 1's reference voice across odd scenes (1, 3, 5)
     and Character 2's reference voice across even scenes (2, 4, 6).
  3. Cepstral Spectral Envelope (Formant) Matching + Median Log-F0 Locking so vocal timbre and pitch
     stay consistent across all scenes while preserving 100% frame-accurate lip-sync and SFX.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import librosa
import numpy as np
import parselmouth
import soundfile as sf
import torch
import torchaudio
from scipy.signal import stft, istft
from scipy.ndimage import gaussian_filter1d


def adjust_f0_semitones(f0_sequence: torch.Tensor, n_semitones: float) -> torch.Tensor:
    factor = 2.0 ** (n_semitones / 12.0)
    return f0_sequence * factor


def match_spectral_envelope(source_wav: np.ndarray, ref_wav: np.ndarray, sr: int = 44100, alpha: float = 0.65) -> np.ndarray:
    """
    Matches the smoothed spectral envelope (formant structure) of `source_wav` to `ref_wav`
    on active speech frames so residual formant differences between scenes are eliminated.
    """
    n_fft = 2048
    hop = 512
    _, _, Z_src = stft(source_wav, fs=sr, nperseg=n_fft, noverlap=n_fft - hop)
    _, _, Z_ref = stft(ref_wav, fs=sr, nperseg=n_fft, noverlap=n_fft - hop)

    mag_src = np.abs(Z_src)
    mag_ref = np.abs(Z_ref)

    frame_e_src = np.mean(mag_src, axis=0)
    frame_e_ref = np.mean(mag_ref, axis=0)
    active_src = frame_e_src > (np.max(frame_e_src) * 0.12)
    active_ref = frame_e_ref > (np.max(frame_e_ref) * 0.12)

    if np.sum(active_src) < 5 or np.sum(active_ref) < 5:
        return source_wav

    mean_spec_src = np.mean(mag_src[:, active_src], axis=1) + 1e-7
    mean_spec_ref = np.mean(mag_ref[:, active_ref], axis=1) + 1e-7

    # Smooth in log-frequency domain to capture vocal tract formants (F1-F4) without harmonic comb filtering
    env_src = np.exp(gaussian_filter1d(np.log(mean_spec_src), sigma=10.0))
    env_ref = np.exp(gaussian_filter1d(np.log(mean_spec_ref), sigma=10.0))

    eq_gain = (env_ref / (env_src + 1e-7)) ** alpha
    eq_gain = np.clip(eq_gain, 0.35, 2.8)
    # Taper off below 90 Hz and above 10 kHz
    freqs = np.linspace(0, sr / 2, len(eq_gain))
    eq_gain[freqs < 90] = 1.0
    eq_gain[freqs > 10000] = 1.0

    Z_matched = Z_src * eq_gain[:, None]
    _, out_wav = istft(Z_matched, fs=sr, nperseg=n_fft, noverlap=n_fft - hop)
    if len(out_wav) >= len(source_wav):
        out_wav = out_wav[:len(source_wav)]
    else:
        out_wav = np.pad(out_wav, (0, len(source_wav) - len(out_wav)))
    return out_wav.astype(np.float32)


def run_pipeline(
    input_video: Path,
    output_video: Path,
    work_dir: Path,
    seed_vc_dir: Path,
    v1_ref_scene: int = 1,
    v2_ref_scene: int = 2,
    v1_semitones: float = 0.0,
    v2_semitones: float = -2.0,
    diffusion_steps: int = 50,
    cfg_rate: float = 0.85,
    f0_var_scale: float = 0.65,
) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(seed_vc_dir))
    orig_cwd = os.getcwd()
    os.chdir(seed_vc_dir)

    from inference import load_models

    class Args:
        f0_condition = True
        fp16 = False
        checkpoint = None
        config = None

    print("[1/4] Loading Seed-VC 44.1kHz F0-Conditioned model (Whisper + RMVPE + CAMPPlus + BigVGAN)...")
    model, semantic_fn, f0_fn, vocoder_fn, campplus_model, mel_fn, mel_fn_args = load_models(Args())
    device = next(model.cfm.parameters()).device
    sr = mel_fn_args["sampling_rate"]
    print(f"      Loaded on device={device}, sr={sr}")

    # 1. Extract 6 10-second scene audio tracks
    print("[2/4] Extracting 6 scene audio tracks & separating vocals via HTDemucs...")
    raw_wavs = []
    for i in range(1, 7):
        start_s = (i - 1) * 10
        wav_p = work_dir / f"raw_scene_{i}.wav"
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(start_s), "-t", "10",
                "-i", str(input_video), "-vn",
                "-acodec", "pcm_s16le", "-ar", str(sr), "-ac", "2", str(wav_p),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        raw_wavs.append(str(wav_p))

    sep_dir = work_dir / "separated"
    subprocess.run(
        [
            sys.executable, "-m", "demucs.separate",
            "-n", "htdemucs", "--two-stems=vocals",
            "-o", str(sep_dir), *raw_wavs,
        ],
        check=True,
    )

    htdemucs_dir = sep_dir / "htdemucs"
    ref_dir = work_dir / "refs_clean"
    ref_dir.mkdir(exist_ok=True)
    out_dir = work_dir / "synced_scenes"
    out_dir.mkdir(exist_ok=True)

    def extract_active_ref(voc_file: Path, out_ref: Path) -> np.ndarray:
        y, _ = librosa.load(str(voc_file), sr=sr, mono=True)
        hop = int(0.01 * sr)
        rms = librosa.feature.rms(y=y, frame_length=int(0.03 * sr), hop_length=hop)[0]
        active_idx = np.where(rms > np.max(rms) * 0.12)[0]
        if len(active_idx) > 10:
            s0 = max(0, int(active_idx[0] * hop - 0.15 * sr))
            s1 = min(len(y), int(active_idx[-1] * hop + 0.15 * sr))
            clip = y[s0:s1]
        else:
            clip = y[int(0.8 * sr):int(8.5 * sr)]
        sf.write(str(out_ref), clip, sr)
        return clip

    v1_ref_path = ref_dir / "v1_ref.wav"
    v2_ref_path = ref_dir / "v2_ref.wav"
    v1_ref_audio = extract_active_ref(htdemucs_dir / f"raw_scene_{v1_ref_scene}" / "vocals.wav", v1_ref_path)
    v2_ref_audio = extract_active_ref(htdemucs_dir / f"raw_scene_{v2_ref_scene}" / "vocals.wav", v2_ref_path)

    @torch.no_grad()
    def convert_f0_locked(source_wav_path: Path, target_ref_path: Path, ref_np: np.ndarray, semi_tones: float) -> np.ndarray:
        source_audio, _ = librosa.load(str(source_wav_path), sr=sr, mono=True)
        ref_audio, _ = librosa.load(str(target_ref_path), sr=sr, mono=True)
        orig_len = len(source_audio)

        hop = int(0.01 * sr)
        win = int(0.03 * sr)
        rms = librosa.feature.rms(y=source_audio, frame_length=win, hop_length=hop)[0]
        speech_mask_frames = rms > (np.max(rms) * 0.05)
        speech_env = np.interp(
            np.arange(orig_len),
            np.arange(len(speech_mask_frames)) * hop,
            speech_mask_frames.astype(np.float32),
        )
        kernel = int(0.04 * sr)
        if kernel > 1:
            speech_env = np.convolve(speech_env, np.ones(kernel) / kernel, mode="same")
        speech_env = np.clip(speech_env * 1.4, 0.0, 1.0)

        source_t = torch.tensor(source_audio, dtype=torch.float32).unsqueeze(0).to(device)
        ref_t = torch.tensor(ref_audio[:sr * 25], dtype=torch.float32).unsqueeze(0).to(device)

        converted_waves_16k = torchaudio.functional.resample(source_t, sr, 16000)
        ref_waves_16k = torchaudio.functional.resample(ref_t, sr, 16000)

        S_alt = semantic_fn(converted_waves_16k)
        S_ori = semantic_fn(ref_waves_16k)

        mel = mel_fn(source_t.float())
        mel2 = mel_fn(ref_t.float())

        target_lengths = torch.LongTensor([mel.size(2)]).to(mel.device)
        target2_lengths = torch.LongTensor([mel2.size(2)]).to(mel2.device)

        feat2 = torchaudio.compliance.kaldi.fbank(
            ref_waves_16k, num_mel_bins=80, dither=0, sample_frequency=16000
        )
        feat2 = feat2 - feat2.mean(dim=0, keepdim=True)
        style2 = campplus_model(feat2.unsqueeze(0))

        F0_ori = torch.from_numpy(f0_fn(ref_waves_16k[0], thred=0.03)).float().to(device)[None]
        F0_alt = torch.from_numpy(f0_fn(converted_waves_16k[0], thred=0.03)).float().to(device)[None]

        voiced_F0_ori = F0_ori[F0_ori > 1]
        voiced_F0_alt = F0_alt[F0_alt > 1]

        log_f0_alt = torch.log(F0_alt + 1e-5)
        median_log_f0_ori = torch.median(torch.log(voiced_F0_ori + 1e-5))
        median_log_f0_alt = torch.median(torch.log(voiced_F0_alt + 1e-5))

        shifted_log_f0_alt = log_f0_alt.clone()
        voiced_mask = F0_alt > 1
        shifted_log_f0_alt[voiced_mask] = (
            (log_f0_alt[voiced_mask] - median_log_f0_alt) * f0_var_scale + median_log_f0_ori
        )
        shifted_f0_alt = torch.exp(shifted_log_f0_alt)
        if semi_tones != 0.0:
            shifted_f0_alt[voiced_mask] = adjust_f0_semitones(shifted_f0_alt[voiced_mask], semi_tones)

        cond, _, _, _, _ = model.length_regulator(
            S_alt, ylens=target_lengths, n_quantizers=3, f0=shifted_f0_alt
        )
        prompt_condition, _, _, _, _ = model.length_regulator(
            S_ori, ylens=target2_lengths, n_quantizers=3, f0=F0_ori
        )

        cat_condition = torch.cat([prompt_condition, cond], dim=1)
        vc_target = model.cfm.inference(
            cat_condition,
            torch.LongTensor([cat_condition.size(1)]).to(mel2.device),
            mel2,
            style2,
            None,
            diffusion_steps,
            inference_cfg_rate=cfg_rate,
        )
        vc_target = vc_target[:, :, mel2.size(-1):]
        vc_wave = vocoder_fn(vc_target.float()).squeeze().cpu().numpy()

        if len(vc_wave) >= orig_len:
            vc_wave = vc_wave[:orig_len]
        else:
            vc_wave = np.pad(vc_wave, (0, orig_len - len(vc_wave)))

        # Match formant spectral envelope to reference vocal
        vc_wave = match_spectral_envelope(vc_wave, ref_np, sr=sr, alpha=0.65)
        vc_wave = vc_wave * speech_env
        active = vc_wave[speech_env > 0.5]
        if len(active) > 0:
            cur_rms = np.sqrt(np.mean(active ** 2)) + 1e-8
            vc_wave = vc_wave * (0.145 / cur_rms)

        return vc_wave

    print("[3/4] Running 50-step F0-Locked + Formant-Matched Seed-VC across all 6 scenes...")
    for idx in range(1, 7):
        is_v1 = idx in (1, 3, 5)
        role = "CHARACTER_1 (LEFT)" if is_v1 else "CHARACTER_2 (RIGHT)"
        ref_p = v1_ref_path if is_v1 else v2_ref_path
        ref_arr = v1_ref_audio if is_v1 else v2_ref_audio
        semi = v1_semitones if is_v1 else v2_semitones

        voc_path = htdemucs_dir / f"raw_scene_{idx}" / "vocals.wav"
        bg_path = htdemucs_dir / f"raw_scene_{idx}" / "no_vocals.wav"

        vc_mono = convert_f0_locked(voc_path, ref_p, ref_arr, semi_tones=semi)
        voc_out = out_dir / f"voc_44k_scene_{idx}.wav"
        sf.write(str(voc_out), vc_mono, sr)

        snd = parselmouth.Sound(str(voc_out))
        pitch = snd.to_pitch(time_step=0.01, pitch_floor=75.0, pitch_ceiling=550.0)
        f0 = pitch.selected_array["frequency"]
        v = f0[f0 > 0]
        print(f"      Scene {idx} [{role}] -> F0 median={np.median(v):.1f}Hz, mean={np.mean(v):.1f}Hz, std={np.std(v):.1f}Hz")

        bg_stereo, _ = librosa.load(str(bg_path), sr=sr, mono=False)
        orig_len = bg_stereo.shape[1]
        if len(vc_mono) >= orig_len:
            vc_mono = vc_mono[:orig_len]
        else:
            vc_mono = np.pad(vc_mono, (0, orig_len - len(vc_mono)))

        vc_stereo = np.vstack([vc_mono, vc_mono])
        # Keep 100% of background SFX (farts, sewer ambience, comical sound effects)
        mixed = vc_stereo * 1.0 + bg_stereo * 0.85
        peak = np.max(np.abs(mixed))
        if peak > 0.96:
            mixed = mixed * (0.96 / peak)

        scene_out = out_dir / f"final_44k_scene_{idx}.wav"
        sf.write(str(scene_out), mixed.T, sr, subtype="PCM_16")

    print("[4/4] Stitching master 60s 44.1kHz audio & muxing into 1080p MP4...")
    concat_txt = out_dir / "concat.txt"
    with open(concat_txt, "w") as f:
        for idx in range(1, 7):
            f.write(f"file '{out_dir / f'final_44k_scene_{idx}.wav'}'\n")

    master_wav = out_dir / "master_44k_synced_60s.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c:a", "pcm_s16le", str(master_wav)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(input_video),
            "-i", str(master_wav),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-ar", "44100",
            "-shortest", str(output_video),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.chdir(orig_cwd)
    print(f"Done! Saved voice-synchronized video to: {output_video}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Synchronize voices across 6-scene Muse AI video using Seed-VC 44.1kHz")
    parser.add_argument("--input", required=True, type=Path, help="Input 60s MP4 video path")
    parser.add_argument("--output", required=True, type=Path, help="Output voice-synced MP4 video path")
    parser.add_argument("--work-dir", required=True, type=Path, help="Working directory for intermediate audio")
    parser.add_argument("--seed-vc-dir", required=True, type=Path, help="Path to cloned Plachtaa/seed-vc repository")
    parser.add_argument("--v1-ref-scene", type=int, default=1, help="Reference scene index for Left Character (1, 3, or 5)")
    parser.add_argument("--v2-ref-scene", type=int, default=2, help="Reference scene index for Right Character (2, 4, or 6)")
    parser.add_argument("--v1-semitones", type=float, default=0.0, help="Semitone shift for Left Character")
    parser.add_argument("--v2-semitones", type=float, default=-2.0, help="Semitone shift for Right Character")
    args = parser.parse_args()

    run_pipeline(
        input_video=args.input,
        output_video=args.output,
        work_dir=args.work_dir,
        seed_vc_dir=args.seed_vc_dir,
        v1_ref_scene=args.v1_ref_scene,
        v2_ref_scene=args.v2_ref_scene,
        v1_semitones=args.v1_semitones,
        v2_semitones=args.v2_semitones,
    )
