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


def make_clips(ffmpeg, folder, seconds, shot_seconds=None, kinds=("dialogue", "action", "dialogue")):
    clips = [dict(file="unused-title.mp4", role="title", kind="title")]
    for index, (kind, color) in enumerate(zip(kinds, ("red", "green", "blue")), 1):
        entry = dict(file=None, role="shot", kind=kind)
        duration = shot_seconds[index - 1] if shot_seconds else seconds
        if shot_seconds:
            entry["duration_seconds"] = duration
        clips.append(entry)
        if kind == "card":
            continue
        filename = "shot-%02d.mp4" % index
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"color=c={color}:s=180x320:r=20:d={duration}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(folder / filename)], check=True)
        entry["file"] = filename
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


def check_sound_cues(pipeline):
    def shot(**sound):
        return dict(n=pipeline.FPS * 3, seed=92, audio=np.empty(0, dtype=np.float32),
                    sound=pipeline.ShotAudio(**sound))

    sr = pipeline.SR
    pipeline_mix = pipeline.mix_timeline_audio
    prelap = pipeline_mix([shot(), shot(effect="bell", bridge="prelap", gain=0.9)])
    assert len(prelap) == 6 * sr and np.max(np.abs(prelap[11 * sr // 4:3 * sr])) > 0.025
    carry = pipeline_mix([shot(effect="bell", bridge="carry"), shot()])
    assert np.max(np.abs(carry[3 * sr:13 * sr // 4])) > 0.025
    blocked = pipeline_mix([shot(silence=True), shot(effect="bell", bridge="prelap"),
                            shot(silence=True)])
    assert not np.any(blocked[:3 * sr]) and not np.any(blocked[6 * sr:])
    blocked_tail = pipeline_mix([shot(effect="bell", bridge="carry"), shot(silence=True)])
    assert not np.any(blocked_tail[3 * sr:])
    soft = pipeline_mix([shot(effect="bell", gain=0.5)])
    strong = pipeline_mix([shot(effect="bell", gain=1.8)])
    assert np.max(np.abs(strong)) > 3 * np.max(np.abs(soft))
    assert (np.max(np.abs(strong[sr:2 * sr])) / np.max(np.abs(strong))
            > 2 * np.max(np.abs(soft[sr:2 * sr])) / np.max(np.abs(soft)))
    assert np.max(np.abs(pipeline_mix([shot(effect="fire_crackle")]))) > 0.05


def check_cards(pipeline, root):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    clips = root / "clips"
    clips.mkdir(parents=True)
    shots, audio, video = root / "shots.json", root / "audio", root / "dialogue.mp4"
    pipeline.save_json(shots, {
        "title": "Dragon Gate", "badge": "Preview", "audio_mode": "dialogue", "show_cards": False,
        "shots": [dict(kind="dialogue", speaker="hero", text="Hear that?", duration_seconds=4, delivery_mode="low"),
                  dict(kind="action", action="The bell rings", sound_effect="bell", duration_seconds=3),
                  dict(kind="card", subtitle="A bell brought him home", duration_seconds=4, ambience="none",
                       sound_effect="none", silence=True)],
    })
    manifest = make_clips(ffmpeg, clips, 3, (4, 3, 4), ("dialogue", "action", "card"))
    pipeline.save_json(clips / "clips.json", manifest)

    def synthesize(text, voice, pitch, rate, cache):
        assert pitch == "-7Hz" and rate == "-3%"
        count = int(pipeline.SR * 3.25)
        samples = 0.13 * np.sin(2 * np.pi * 200 * np.arange(count) / pipeline.SR)
        key = pipeline.voice_cache_key(text, voice, pitch, rate)
        sf.write(Path(cache) / (key + ".wav"), samples, pipeline.SR, subtype="PCM_16")
        return count, False

    with patch.object(pipeline, "voice_of", side_effect=synthesize) as synth:
        pipeline.cmd_tts(Namespace(shots=shots, out=audio, ep=1, clip_seconds=3))
        assert synth.call_count == 1
    audio_entries = pipeline.load_json(audio / "audio.json")["entries"]
    assert audio_entries[3] == dict(kind="card", samples=0, frames=80)
    compose = Namespace(shots=shots, clips=clips, panels=None, audio=audio, out=video, ep=1, manifest=None)
    pipeline.cmd_compose(compose)
    episode = pipeline.load_json(root / "manifest.json")[0]
    assert episode["frames"] == 220 and episode["seconds"] == 11, episode
    card = read_frame(video, 145)
    assert np.max(card[100, 100]) < 25 and np.max(card[500:750, 80:-80].min(axis=2)) > 215
    np.testing.assert_array_equal(read_frame(video, 145), read_frame(video, 200))
    pcm = subprocess.check_output([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
                                   "-vn", "-f", "f32le", "-c:a", "pcm_f32le", "-"])
    samples = np.frombuffer(pcm, dtype="<f4")
    assert np.max(np.abs(samples[pipeline.SR * 8:pipeline.SR * 10])) < 0.001

    manifest["clips"][1]["duration_seconds"] = 3
    pipeline.save_json(clips / "clips.json", manifest)
    try:
        pipeline.cmd_compose(compose)
    except ValueError as error:
        assert "视频清单的时长与分镜不一致" in str(error)
    else:
        raise AssertionError("Compose accepted mismatched shot duration")


def main():
    source = Path(__file__).resolve().parents[1] / "tools" / "manju" / "manju_pipeline.py"
    spec = importlib.util.spec_from_file_location("manju_pipeline", source)
    pipeline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipeline)
    check_sound_cues(pipeline)
    with tempfile.TemporaryDirectory(prefix="light-agent-dialogue-") as folder:
        root = Path(folder)
        run(pipeline, root)
        check_cards(pipeline, root / "mixed-timeline")
    print("PASS: dialogue/bridge mix, silence, title cards, fixed/mixed 3s/4s A/V and manifest validation")


if __name__ == "__main__":
    main()
