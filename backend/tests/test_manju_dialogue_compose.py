"""Check dialogue timing and media with short, offline voice and clip fixtures."""

from argparse import Namespace
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

import cv2
import imageio_ffmpeg
import numpy as np
import soundfile as sf


def read_frame(path, index):
    cap = cv2.VideoCapture(str(path))
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        assert ok, index
        return frame
    finally:
        cap.release()


def make_clips(ffmpeg, folder, seconds):
    clips = [dict(file="unused-title.mp4", role="title", kind="title")]
    for index, (kind, color) in enumerate((("dialogue", "red"), ("action", "green"), ("dialogue", "blue")), 1):
        filename = "shot-%02d.mp4" % index
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"color=c={color}:s=180x320:r=20:d={seconds}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(folder / filename)], check=True)
        clips.append(dict(file=filename, role="shot", kind=kind))
    clips.append(dict(file="unused-end.mp4", role="end", kind="end"))
    return dict(clip_seconds=seconds, clips=clips)


def run(pipeline, root):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    clips = root / "clips"
    clips.mkdir()
    shots, audio, video = root / "shots.json", root / "audio", root / "dialogue.mp4"
    voices = {"hero": ("zh-CN-YunxiNeural", "+12%"), "mother": ("zh-CN-XiaoxiaoNeural", "-6%")}
    board = {
        "title": "Dragon Gate", "badge": "Preview", "audio_mode": "dialogue",
        "shots": [dict(kind="dialogue", speaker="hero", text="Listen!"),
                  dict(kind="action", action="The bell rings", ambience="wind", sound_effect="bell"),
                  dict(kind="dialogue", speaker="mother", text="Hold the lamp.")],
        "voice_cast": {speaker: dict(voice=voice, rate=rate) for speaker, (voice, rate) in voices.items()},
    }
    pipeline.save_json(shots, board)
    speech_seconds = {voice: 2.9 for voice, _ in voices.values()}

    def synthesize(text, voice, pitch, rate, cache):
        key = pipeline.voice_cache_key(text, voice, pitch, rate)
        count = int(pipeline.SR * speech_seconds[voice])
        pitch_hz = 200 if voice == voices["hero"][0] else 320
        samples = 0.16 * np.sin(2 * np.pi * pitch_hz * np.arange(count) / pipeline.SR)
        sf.write(Path(cache) / (key + ".wav"), samples, pipeline.SR, subtype="PCM_16")
        return count, False

    tts = Namespace(shots=shots, out=audio, ep=1, clip_seconds=3)
    compose = Namespace(shots=shots, clips=clips, panels=None, audio=audio, out=video, ep=1, manifest=None)
    audio_manifest = audio / "audio.json"
    with patch.object(pipeline, "voice_of", side_effect=synthesize) as synth:
        pipeline.cmd_tts(tts)
        assert synth.call_count == 2
        assert {(call.args[1], call.args[3]) for call in synth.call_args_list} == set(voices.values())
        entries = pipeline.load_json(audio_manifest)["entries"]
        assert all("file" not in entries[i] and entries[i]["samples"] == 0 for i in (0, 2, 4))
        assert all(entry["frames"] == 3 * pipeline.FPS for entry in entries)

        audio_before = audio_manifest.read_bytes()
        speech_seconds[voices["hero"][0]] = 3.2
        try:
            pipeline.cmd_tts(tts)
        except ValueError as error:
            assert "请拆成更短的台词" in str(error)
        else:
            raise AssertionError("Overlong dialogue was accepted")
        assert audio_manifest.read_bytes() == audio_before
        speech_seconds[voices["hero"][0]] = 2.9
        # Recreate the fixture waveform overwritten by the overlong test.
        pipeline.cmd_tts(tts)

    for seconds in (3, 4):
        pipeline.save_json(clips / "clips.json", make_clips(ffmpeg, clips, seconds))
        pipeline.cmd_compose(compose)
        episode = pipeline.load_json(root / "manifest.json")[0]
        assert episode["frames"] == seconds * 3 * pipeline.FPS, episode
        assert episode["audio_mode"] == "dialogue"

        for index, dominant in enumerate((2, 1, 0)):
            frame = read_frame(video, index * seconds * pipeline.FPS + 8)
            assert np.argmax(frame[pipeline.H // 2, pipeline.W // 2]) == dominant
            if index == 1:
                # Entire action frame matches the solid fixture, including the subtitle area.
                assert np.max(np.ptp(frame, axis=(0, 1))) <= 3
            else:
                assert np.max(frame[-235:-80, 50:-50, :].min(axis=2)) >= 205

        pcm = subprocess.check_output([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
                                       "-vn", "-f", "f32le", "-c:a", "pcm_f32le", "-"])
        samples = np.frombuffer(pcm, dtype="<f4")
        assert abs(len(samples) - seconds * 3 * pipeline.SR) <= 1024
        for index in range(3):
            start = int((index * seconds + 0.2) * pipeline.SR)
            assert np.max(np.abs(samples[start:start + pipeline.SR // 2])) > 0.04

    prior_video = video.read_bytes()
    board["shots"][0]["text"] = "A new line"
    pipeline.save_json(shots, board)
    try:
        pipeline.cmd_compose(compose)
    except ValueError as error:
        assert "配音清单与当前对白分镜不匹配" in str(error)
    else:
        raise AssertionError("Compose accepted stale speech")
    assert video.read_bytes() == prior_video


def main():
    source = Path(__file__).resolve().parents[1] / "tools" / "manju" / "manju_pipeline.py"
    spec = importlib.util.spec_from_file_location("manju_pipeline", source)
    pipeline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipeline)
    with tempfile.TemporaryDirectory(prefix="light-agent-dialogue-") as folder:
        run(pipeline, Path(folder))
    print("PASS: two role voices, no narration or action subtitles, audible action cues, 3s/4s A/V and stale audio rejection")


if __name__ == "__main__":
    main()
