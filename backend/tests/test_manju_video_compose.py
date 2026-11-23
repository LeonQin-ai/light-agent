"""Synthetic MP4 -> motion comic regression; runs offline with Manju dependencies."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import cv2
import imageio_ffmpeg
import numpy as np
import soundfile as sf


PIPELINE = Path(__file__).resolve().parents[1] / "tools" / "manju" / "manju_pipeline.py"
FPS, SR = 20, 44100


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def read_pixel(video, frame):
    cap = cv2.VideoCapture(str(video))
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, pixels = cap.read()
        assert ok, (video, frame)
        return tuple(int(c) for c in pixels[640, 360])
    finally:
        cap.release()


def main():
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="light-agent-video-compose-") as tmp:
        root = Path(tmp)
        clips, audio = root / "clips", root / "audio"
        clips.mkdir()
        audio.mkdir()
        storyboard = root / "shots.json"
        write_json(storyboard, {"title": "Short motion comic", "badge": "Fixture",
                                "shots": [{"kind": "dialogue", "text": "A long line."}]})
        fingerprint = 0xcbf29ce484222325
        for byte in storyboard.read_bytes():
            fingerprint = ((fingerprint ^ byte) * 0x100000001b3) & 0xffffffffffffffff
        source = clips / "source.mp4"
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=180x320:r=30:d=4",
            "-vf", "drawbox=t=fill:color=green:enable='between(t,1,2.99)',"
                   "drawbox=t=fill:color=blue:enable='gte(t,3)'",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ], check=True)
        clip_entries, audio_entries = [], []
        for role, kind, length in (("title", "title", 1), ("shot", "dialogue", 5), ("end", "end", 1)):
            filename = role + ".mp4"
            shutil.copyfile(source, clips / filename)
            clip_entries.append(dict(file=filename, role=role, kind=kind))
            samples = (np.sin(2 * np.pi * 220 * np.arange(length * SR) / SR) * .1).astype(np.float32)
            sf.write(audio / (role + ".wav"), samples, SR, subtype="PCM_16")
            audio_entries.append(dict(file=role + ".wav", kind=kind, frames=length * FPS))
        write_json(audio / "audio.json", {"entries": audio_entries})
        clip_manifest = clips / "clips.json"
        output, manifest = root / "episode.mp4", root / "manifest.json"
        args = [
            sys.executable, "-X", "utf8", str(PIPELINE), "compose", "--shots", str(storyboard),
            "--clips", str(clips), "--audio", str(audio), "--out", str(output),
            "--manifest", str(manifest), "--ep", "2",
        ]
        for seconds in (3, 4):
            write_json(clip_manifest, {"clip_seconds": seconds, "video_model": "fixture-t2v",
                                       "storyboard_fingerprint": f"{fingerprint:016x}",
                                       "clips": clip_entries})
            subprocess.run(args, check=True, timeout=120)
            rec = json.loads(manifest.read_text(encoding="utf-8"))[0]
            assert rec["frames"] == (seconds * 2 + 5) * FPS, rec
            assert rec["visual_mode"] == "video" and rec["clip_seconds"] == seconds
            assert rec["video_model"] == "fixture-t2v" and rec["has_audio"]
            assert Path(rec["poster"]).is_file()
            b, g, r = read_pixel(output, seconds * FPS + int(4.5 * FPS))
            # At 4.5s into the voiced shot the image holds the final frame.
            # Requesting 3 seconds discards the blue fourth second.
            expected = (g > b and g > r) if seconds == 3 else (b > g and b > r)
            assert expected, (b, g, r)
            b, g, r = read_pixel(output, seconds * FPS + 5)
            assert r > b and r > g, (b, g, r)
        proof = {path: hashlib.sha256(path.read_bytes()).digest()
                 for path in (output, manifest, Path(rec["poster"]))}
        original_storyboard = storyboard.read_bytes()
        storyboard.write_bytes(original_storyboard + b"\n")
        stale = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert stale.returncode != 0 and "视频片段清单与当前分镜不匹配" in stale.stderr, stale.stderr
        storyboard.write_bytes(original_storyboard)
        (clips / "shot.mp4").write_bytes(b"not an mp4")
        failed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert failed.returncode != 0 and "视频片段解码失败" in failed.stderr, failed.stderr
        assert all(hashlib.sha256(path.read_bytes()).digest() == digest for path, digest in proof.items())
        print("PASS: 3s and 4s motion, trimmed clips, voice tail, stale storyboard and corruption rejection")


if __name__ == "__main__":
    main()
