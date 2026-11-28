"""Test video generation routing, saved task IDs, and ordered Manju clips."""

import argparse
import base64
import collections
import concurrent.futures
import http.server
import json
import pathlib
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zlib

from test_image_routes import probe, request


MODEL = "happyhorse-1.1-t2v"
FIRST_FRAME = "happyhorse-1.1-i2v"
REFERENCE = "happyhorse-1.1-r2v"
KLING = "kling/kling-v3-turbo-video-generation"
SEEDANCE = "doubao-seedance-2-0-260128"
SUBMIT = "/api/v1/services/aigc/video-generation/video-synthesis"
POLL = "/api/v1/tasks/"
ARK_TASKS = "/api/v3/contents/generations/tasks"
TOKEN = "Bearer video-test"


def box(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


# Minimal MP4 container for the provider transport test. Actual decoding and
# stitching are exercised with independently encoded motion clips.
MP4 = (box(b"ftyp", b"isom\0\0\0\0isom") +
       box(b"moov", box(b"trak", box(b"mdia", box(b"hdlr", b"\0" * 8 + b"vide" + b"\0" * 13)))) +
       box(b"mdat", b"\0\x01\x02\x03"))


def solid_png(color):
    def chunk(kind, payload):
        return struct.pack(">I4s", len(payload), kind) + payload + struct.pack(">I", zlib.crc32(kind + payload))

    header = struct.pack(">IIBBBBB", 400, 400, 8, 2, 0, 0, 0)
    pixels = (b"\0" + color * 400) * 400
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
            chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class Provider(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, payload, mime):
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Type", mime)
        self.end_headers()
        self.wfile.write(payload)

    def reply(self, body, status=200):
        self.send(status, json.dumps(body).encode(), "application/json")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/chat/completions" and self.headers.get("Authorization") == TOKEN:
            messages = body.get("messages", [])
            stage = messages[0]["content"].split()[0] if len(messages) == 2 else ""
            with self.server.lock:
                self.server.director_calls.append((stage, messages[-1]["content"] if messages else ""))
                output = self.server.director_steps.get(stage)
            if output is None:
                self.reply({"error": {"message": "Unexpected director request"}}, 400)
                return
            self.reply({"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(output, ensure_ascii=False),
            }}], "usage": {"prompt_tokens": 20, "completion_tokens": 20}})
            return
        params = body.get("parameters", {})
        model = body.get("model")
        ark = model == SEEDANCE
        images = []
        if ark:
            prompt = next((entry.get("text") for entry in body.get("content", [])
                           if entry.get("type") == "text"), "")
            duration = body.get("duration")
            valid = (self.path == ARK_TASKS and duration == 4 and body.get("ratio") == "9:16"
                     and body.get("generate_audio") is False)
        else:
            source = body.get("input", {})
            prompt = source.get("prompt", "")
            duration = params.get("duration")
            media = source.get("media", [])
            visual_input = False
            if (model == FIRST_FRAME and len(media) == 1) or (model == REFERENCE and 1 <= len(media) <= 9):
                expected_type = "first_frame" if model == FIRST_FRAME else "reference_image"
                prefix = "data:image/png;base64,"
                for item in media:
                    url = item.get("url", "")
                    if item.get("type") != expected_type or not url.startswith(prefix):
                        break
                    image = base64.b64decode(url[len(prefix):], validate=True)
                    if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) < 24:
                        break
                    if min(struct.unpack(">II", image[16:24])) < 400:
                        break
                    images.append(image)
                visual_input = len(images) == len(media) and (
                    "ratio" not in params if model == FIRST_FRAME else
                    params.get("ratio") == "9:16" and all(f"[Image {i + 1}]" in prompt for i in range(len(images))))
            valid = (self.path == SUBMIT and self.headers.get("X-DashScope-Async") == "enable"
                     and duration in (3, 4)
                     and ((model == MODEL and not media and params.get("ratio") == "9:16") or
                          (model == KLING and params.get("aspect_ratio") == "9:16" and params.get("mode") == "std")
                          or visual_input))
        if not valid or self.headers.get("Authorization") != TOKEN or not prompt:
            self.reply({"code": "InvalidFixtureRequest"}, 400)
            return
        with self.server.lock:
            self.server.durations[model, duration] += 1
            self.server.submissions.append(prompt)
            if model == REFERENCE:
                self.server.reference_inputs.append((prompt, images))
            task_id = str(len(self.server.submissions))
        self.reply({"id": task_id} if ark else {"output": {"task_id": task_id, "task_status": "PENDING"}})

    def do_GET(self):
        ark = self.path.startswith(ARK_TASKS + "/")
        if (self.path.startswith(POLL) or ark) and self.headers.get("Authorization") == TOKEN:
            task_id = self.path.rsplit("/", 1)[-1]
            with self.server.lock:
                poll_count = self.server.polls[task_id] + 1
                self.server.polls[task_id] = poll_count
                pending = poll_count == 1 and self.server.defer_completion
            if pending:
                self.reply({"status": "running"} if ark else {"output": {"task_status": "RUNNING"}})
            else:
                url = f"http://127.0.0.1:{self.server.server_port}/result/{task_id}"
                self.reply({"status": "succeeded", "content": {"video_url": url}} if ark else
                           {"output": {"task_status": "SUCCEEDED", "video_url": url}})
        elif self.path.startswith("/result/") and not self.headers.get("Authorization"):
            with self.server.lock:
                fail = self.server.fail_download
                self.server.fail_download = False
                cut = self.server.cut_download
                self.server.cut_download = False
            if cut:
                self.send_response(200)
                self.send_header("Content-Length", str(len(MP4)))
                self.send_header("Content-Type", "video/mp4")
                self.end_headers()
                self.wfile.write(MP4[:-3])
                return
            self.send(503 if fail else 200, b"" if fail else MP4, "video/mp4")
        else:
            self.reply({}, 400)


def terminal(base, flow, endpoint="/v1/flows"):
    status, started = request(base, endpoint, flow)
    assert status == 200, (status, started)
    return wait_task(base, started["id"])


def wait_task(base, task_id):
    deadline = time.monotonic() + 60
    last_task = None
    while time.monotonic() < deadline:
        status, task = request(base, f"/v1/tasks/{task_id}")
        assert status == 200, (status, task)
        if task["status"] not in ("RUNNING", "QUEUED"):
            return task
        last_task = {"status": task["status"], "nodes": task.get("flow_nodes", [])}
        time.sleep(0.1)
    raise RuntimeError(f"Video flow {task_id} did not finish: {last_task}")


def check_flow(base, provider, work):
    assert request(base, "/v1/agents", {"name": "manju-artist", "role": "Develop video clips"})[0] == 200
    episode = work / "manju" / "ep02"
    episode.mkdir(parents=True)
    board = {"title": "Fixture", "badge": "Fixture",
             "title_image_prompt": "Opening wide shot",
             "end_image_prompt": "Closing wide shot", "shots": [
                 {"kind": "dialogue", "text": "A lamp turns on.", "video_prompt": "A lamp flickers to life."}
             ]}
    (episode / "shots.json").write_text(json.dumps(board), encoding="utf-8")
    flow = {"nodes": [{"id": "step2", "agent": "manju-artist", "task": "Produce motion clips",
                       "manju_video_episode": 2, "manju_clip_seconds": 4,
                       "manju_video_route": "fixture-video"}], "edges": []}

    with provider.lock:
        provider.fail_download = True
    failed = terminal(base, flow)
    assert failed["status"] == "FAILED", failed
    assert not (episode / "clips" / "clips.json").exists()

    complete = terminal(base, flow)
    assert complete["status"] == "DONE", complete
    doc = json.loads((episode / "clips" / "clips.json").read_text(encoding="utf-8"))
    assert doc["clip_seconds"] == 4 and doc["video_model"] == MODEL
    assert [clip["role"] for clip in doc["clips"]] == ["title", "shot", "end"]
    assert [clip["kind"] for clip in doc["clips"]] == ["title", "dialogue", "end"]
    assert all((episode / "clips" / clip["file"]).read_bytes() == MP4 for clip in doc["clips"])
    assert sum("A lamp flickers to life." in prompt for prompt in provider.submissions) == 1
    assert provider.durations[MODEL, 4] == 3, provider.durations
    cached = terminal(base, flow)
    assert cached["status"] == "DONE" and cached["flow_nodes"][0]["video_requests"] == 0, cached
    assert provider.durations[MODEL, 4] == 3
    print("PASS: ordered 4s clips and resume after failed download without duplicate submissions")


def check_mixed(base, provider, work):
    (work / "hero.png").write_bytes(solid_png(b"\x60\x70\x80"))
    episode = work / "manju" / "ep03"
    episode.mkdir(parents=True)
    board = {"title": "Same hero", "badge": "Fixture", "shots": [
        {"kind": "narration", "text": "A ship appears", "video_mode": "text"},
        {"kind": "narration", "text": "The hero wakes", "first_frame": "hero.png"},
        {"kind": "narration", "text": "The hero returns", "reference_images": ["hero.png"]},
    ]}
    (episode / "shots.json").write_text(json.dumps(board), encoding="utf-8")
    flow = {"nodes": [{"id": "step2", "agent": "manju-artist", "task": "Produce mixed clips",
        "manju_video_episode": 3, "manju_clip_seconds": 4, "manju_video_routes": {
            "text": "fixture-video", "first_frame": FIRST_FRAME, "reference": REFERENCE,
        }}], "edges": []}
    with provider.lock:
        provider.cut_download = True
    complete = terminal(base, flow)
    assert complete["status"] == "DONE", complete
    clips = json.loads((episode / "clips" / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [entry["model"] for entry in clips[1:-1]] == [MODEL, FIRST_FRAME, REFERENCE]
    assert clips[0]["file"] == clips[1]["file"] and clips[3]["file"] == clips[4]["file"]
    assert complete["flow_nodes"][0]["video_requests"] == 3, complete
    assert provider.durations[MODEL, 4] == 4
    assert provider.durations[FIRST_FRAME, 4] == provider.durations[REFERENCE, 4] == 1
    cached = terminal(base, flow)
    assert cached["status"] == "DONE" and cached["flow_nodes"][0]["video_requests"] == 0, cached
    print("PASS: same episode routes three clips by input type with no title/end or resume submissions")


def check_character_references(base, provider, work):
    lulu = solid_png(b"\xa0\x10\x30")
    mei = solid_png(b"\x10\x30\xa0")

    def upload(name, data):
        return {"name": name, "filename": "../hero.png",
                "data_url": "data:image/png;base64," + base64.b64encode(data).decode()}

    payload = {
        "storyboard": {
            "title": "两位主角", "badge": "Fixture", "voice_cast": [],
            "characters": [{"id": "lulu", "name": "噜噜"}, {"id": "mei", "name": "噜妹"}],
            "shots": [{"kind": "narration", "text": "两位主角来到灯塔",
                       "character_ids": ["lulu", "mei", "lulu"], "video_mode": "text"}],
        },
        "episode": 4, "character_views": True, "clip_seconds": 3,
        "video_routes": {"text": "fixture-video", "first_frame": FIRST_FRAME, "reference": REFERENCE},
        "character_references": [upload("噜妹", mei), upload("噜噜", lulu)],
    }
    endpoint = "/v1/manju/import"
    invalid = {**payload, "character_references": [upload("噜噜", b"not a PNG")]}
    status, response = request(base, endpoint, invalid)
    assert status == 400, response
    assert not (work / "manju" / "ep04" / "shots.json").exists()

    task = terminal(base, payload, endpoint)
    nodes = {node["id"]: node for node in task["flow_nodes"]}
    assert nodes["step2"]["status"] == "ok" and nodes["step2"]["video_requests"] == 1, nodes
    assert nodes["character"]["status"] == "ok" and nodes["character"]["image_requests"] == 0, nodes
    assert nodes["step3"]["status"] == "error", nodes  # Invalid voice_cast keeps this fixture offline.
    with provider.lock:
        prompt, images = provider.reference_inputs[-1]
    assert images == [lulu, mei], (len(images), prompt)
    assert "[Image 1] 对应角色「噜噜」" in prompt and "[Image 2] 对应角色「噜妹」" in prompt, prompt
    stored = {p.read_bytes() for p in (work / "manju" / "references").glob("*.png")}
    assert stored == {lulu, mei}, len(stored)
    assert not (work / "manju" / "hero.png").exists()
    print("PASS: uploaded role references control two ordered R2V inputs without regenerating either role")


def check_episode_batch(base, provider, work):
    work = work / "five-episode-workspace"
    work.mkdir()
    assert request(base, "/v1/workspaces", {"path": str(work)})[0] == 200
    hero = solid_png(b"\x32\x94\xb0")
    boards = [
        {"title": f"分镜第{i}集", "badge": "Fixture", "voice_cast": [],
         "characters": [{"id": "hero", "name": "小舟"}],
         "shots": [{"kind": "narration", "text": f"第{i}集，小舟继续送信。",
                    "video_prompt": f"Story beat {i}", "character_ids": ["hero"]}]}
        for i in range(1, 6)
    ]
    payload = {"workspace": str(work), "all_episodes": True, "storyboards": boards,
               "clip_seconds": 3, "video_route": REFERENCE,
               "character_references": [{"name": "小舟", "filename": "hero.png",
                   "data_url": "data:image/png;base64," + base64.b64encode(hero).decode()}]}
    endpoint = "/v1/manju/import"
    status, rejected = request(base, endpoint, {**payload, "storyboards": [*boards[:-1], {}]})
    assert status == 400 and rejected.get("error"), (status, rejected)
    assert not (work / "manju").exists()

    with provider.lock:
        before = len(provider.reference_inputs)
        # Earlier cases verify polling pending jobs. Finish on the first poll
        # here so five dependent episodes do not accumulate real poll delays.
        provider.defer_completion = False
    # request decodes the entire HTTP response as one JSON document.
    status, started = request(base, endpoint, payload)
    assert status == 200 and "error" not in started, (status, started)
    assert started["id"] == started["task_id"] and started["chapters"] == 5, started
    assert started["all_episodes"] is True
    assert started["episodes"] == [
        {"episode": i, "video": f"manju/ep{i:02d}/videos/ep{i:02d}-motion.mp4",
         "compose_node": f"ep{i:02d}-step4"} for i in range(1, 6)
    ], started
    status, history = request(base, "/v1/flows/list")
    assert status == 200, history
    flow = next(item for item in history["records"] if item["id"] == started["task_id"])
    dag = json.loads(flow["dag"])
    video_nodes = [node for node in dag["nodes"] if "manju_video_episode" in node]
    assert [node["manju_video_episode"] for node in video_nodes] == list(range(1, 6)), video_nodes
    refsets = {node["manju_reference_set"] for node in video_nodes}
    assert len(refsets) == 1 and all(refsets), refsets

    task = wait_task(base, started["task_id"])
    nodes = {node["id"]: node for node in task["flow_nodes"]}
    for i, board in enumerate(boards, 1):
        episode = work / "manju" / f"ep{i:02d}"
        assert json.loads((episode / "shots.json").read_text(encoding="utf-8")) == board
        assert nodes[f"ep{i:02d}-step2"]["status"] == "ok", nodes
        assert nodes[f"ep{i:02d}-step2"]["video_requests"] == 1, nodes
    with provider.lock:
        inputs = provider.reference_inputs[before:]
    assert len(inputs) == 5
    for index, (prompt, images) in enumerate(inputs, 1):
        assert f"Story beat {index}" in prompt and images == [hero], prompt
    print("PASS: a five-episode import returns one JSON receipt and sends the same uploaded role to each episode")


def check_character_batch_import(base, work):
    workspace = work / "character-batch-workspace"
    workspace.mkdir()
    assert request(base, "/v1/workspaces", {"path": str(workspace)})[0] == 200
    url = "/v1/manju/characters"
    workspace_query = url + "?" + urllib.parse.urlencode({"workspace": str(workspace)})

    def profile(identifier, name=None):
        return {"id": identifier, "name": name or identifier, "role": "supporting",
                "visual": "深蓝围巾", "outfit_id": "blue-scarf"}

    def batch(profiles, **fields):
        return request(base, url + "/import", {"workspace": str(workspace), "characters": profiles, **fields})

    hero = profile("hero", "阿铃")
    voice = {"voice": "zh-CN-YunxiNeural", "rate": "-5%"}
    roster = {"characters": [hero], "voice_cast": {"hero": voice}}
    assert request(base, url, {"workspace": str(workspace), **roster})[0] == 200
    mother, friend = profile("mother", "阿娘"), profile("friend", "小孟")
    status, added = batch([mother, friend], voice_cast={"mother": {"pitch": "-5Hz"}})
    assert status == 200 and (added["imported"], added["updated"]) == (2, 0), added
    assert added["characters"] == [hero, mother, friend]
    assert added["voice_cast"] == {"hero": voice, "mother": {"pitch": "-5Hz"}}

    baseline = request(base, workspace_query)[1]
    assert baseline["characters"] == added["characters"] and baseline["voice_cast"] == added["voice_cast"]
    assert "imported" not in baseline and "updated" not in baseline
    stranger, replacement = profile("stranger", "旅人"), {**hero, "visual": "灰蓝围巾"}
    status, conflict = batch([stranger, replacement])
    assert status == 409 and conflict["conflicts"] == ["hero"], conflict
    assert request(base, workspace_query)[1] == baseline
    assert batch([stranger, stranger], overwrite=True)[0] == 400
    assert batch([stranger], voice_cast={"hero": voice})[0] == 400
    assert batch([stranger], overwrite="true")[0] == 400
    assert batch([])[0] == 400
    assert request(base, workspace_query)[1] == baseline

    status, merged = batch([stranger, replacement], voice_cast={"stranger": {"rate": "+3%"}}, overwrite=True)
    assert status == 200 and (merged["imported"], merged["updated"]) == (1, 1), merged
    assert merged["characters"] == [replacement, mother, friend, stranger]
    assert merged["voice_cast"] == {**baseline["voice_cast"], "stranger": {"rate": "+3%"}}
    assert request(base, url)[1]["characters"] == []

    from_left = [profile(f"left-{index}") for index in range(10)]
    from_right = [profile(f"right-{index}") for index in range(10)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        concurrent_status = [status for status, _ in pool.map(batch, (from_left, from_right))]
    assert concurrent_status == [200, 200]
    full = request(base, workspace_query)[1]
    assert {entry["id"] for entry in full["characters"]} == {
        entry["id"] for entry in merged["characters"] + from_left + from_right}
    assert batch([profile("extra")])[0] == 422
    assert request(base, workspace_query)[1] == full
    print("PASS: atomic character imports add several profiles, preserve existing voices, and respect workspace capacity")


def check_dialogue_import(base, provider, work):
    project = work / "dialogue-workspace"
    episode = project / "manju" / "ep01"
    episode.mkdir(parents=True)
    assert request(base, "/v1/workspaces", {"path": str(project)})[0] == 200
    hero = {"id": "hero", "name": "阿铃", "visual": "青色衣领和铜铃",
            "role": "protagonist", "appearance_id": "hero-v2"}
    voice = {"voice": "zh-CN-YunxiNeural", "pitch": "+0Hz", "rate": "-5%"}
    roster = {"characters": [hero], "voice_cast": {"hero": voice}}
    url = "/v1/manju/characters"
    assert request(base, url, {"workspace": str(project), **roster})[0] == 200
    own = request(base, url + "?" + urllib.parse.urlencode({"workspace": str(project)}))[1]
    assert own["characters"] == [hero] and own["voice_cast"] == {"hero": voice}, own
    assert request(base, url)[1]["characters"] == []
    assert request(base, url, {"workspace": str(project), "characters": [hero, hero]})[0] == 400

    direction = {"scene": "院子", "background": "夜色下的门和石阶",
                 "action": "举起铜铃看向山口", "image_prompt": "青衣少年背对镜头", "character_ids": ["hero"]}
    board = {"title": "铜铃", "badge": "Fixture", "characters": [{**hero, "visual": "旧设定"}],
             "shots": [{**direction, "kind": "dialogue", "speaker": "hero", "text": "娘，你快看！"},
                       {**direction, "kind": "action", "ambience": "wind", "sound_effect": "bell",
                        "duration_seconds": 3, "sound_bridge": "prelap"},
                       {"kind": "card", "subtitle": "回家", "ambience": "none", "duration_seconds": 4}]}
    payload = {"workspace": str(project), "audio_mode": "dialogue", "character_roster": roster,
               "storyboard": board, "video_route": "fixture-video", "clip_seconds": 4}
    with provider.lock:
        before = len(provider.submissions)
    invalid = {**board, "shots": [{**direction, "kind": "narration", "text": "旁白"}]}
    assert request(base, "/v1/manju/import", {**payload, "storyboard": invalid})[0] == 400
    overlong = {**board, "shots": [{**board["shots"][1], "duration_seconds": 5}, board["shots"][2]]}
    assert request(base, "/v1/manju/import", {**payload, "storyboard": overlong})[0] == 400
    assert not (episode / "shots.json").exists()

    # Fail preflight deterministically, without requiring a real TTS provider.
    (episode / "audio").write_text("unwritable as a directory", encoding="utf-8")
    status, started = request(base, "/v1/manju/import", payload)
    assert status == 200 and started["episodes"][0]["audio_mode"] == "dialogue", (status, started)
    saved = json.loads((episode / "shots.json").read_text(encoding="utf-8"))
    assert saved["characters"] == [hero] and saved["voice_cast"] == {"hero": voice}, saved
    assert saved["shots"][1]["kind"] == "action" and "text" not in saved["shots"][1]
    assert saved["shots"][2] == board["shots"][2]
    history = request(base, "/v1/flows/list")[1]
    dag = json.loads(next(item["dag"] for item in history["records"] if item["id"] == started["id"]))
    assert {"from": "step3", "to": "step2"} in dag["edges"], dag
    assert any(node.get("manju_voice_episode") == 1 and node["manju_clip_seconds"] == 4 for node in dag["nodes"])
    assert wait_task(base, started["id"])["status"] == "FAILED"
    with provider.lock:
        assert len(provider.submissions) == before
        counts = provider.durations.copy()
    # Exercise the visual node independently of the intentional audio failure.
    task = terminal(base, {"workspace": str(project), "nodes": [{
        "id": "visual", "agent": "manju-artist", "task": "Generate the motion shots",
        "manju_video_episode": 1, "manju_clip_seconds": 4, "manju_video_route": "fixture-video",
    }], "edges": []})
    assert task["status"] == "DONE", task
    clips = json.loads((episode / "clips" / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [(entry["kind"], entry["duration_seconds"]) for entry in clips[1:-1]] == [
        ("dialogue", 4), ("action", 3), ("card", 4)]
    assert clips[3]["file"] is None and clips[-1]["file"] == clips[2]["file"]
    with provider.lock:
        assert len(provider.submissions) == before + 2
        assert provider.durations[MODEL, 4] == counts[MODEL, 4] + 1
        assert provider.durations[MODEL, 3] == counts[MODEL, 3] + 1
    print("PASS: dialogue validates before rendering, mixes 3s/4s shots and skips provider jobs for authored cards")


def check_director_pipeline(base, provider, work):
    model = {"name": "fixture-director", "provider": "openai", "model": "fixture-chat",
             "base_url": f"http://127.0.0.1:{provider.server_port}/v1", "api_key": "video-test"}
    assert request(base, "/v1/routes", model)[0] == 200
    project = work / "director-workspace"
    episode = project / "manju" / "ep01"
    episode.mkdir(parents=True)
    assert request(base, "/v1/workspaces", {"path": str(project)})[0] == 200
    (episode / "audio").write_text("stop after storyboarding", encoding="utf-8")

    hero = {"id": "hero", "name": "阿铃", "role": "protagonist", "visual": "眉疤、布衣与铜铃"}
    mother = {"id": "mother", "name": "母亲", "role": "supporting"}
    direction = {"scene": "石桥", "background": "青雾掠过桥栏", "action": "少年握住铜铃",
                 "image_prompt": "竖屏墨青石桥，少年侧脸与手中缺口铜铃入镜",
                 "framing": "侧面近景", "character_ids": ["hero"], "transition": "铃声先入后硬切"}
    source_marker = "原稿只在陈默阶段出现"
    ready = {"status": "ready"}
    with provider.lock:
        provider.director_steps = {
            "X-03": {**ready, "summary": "守桥与怕雷的矛盾经铃声回收。", "narrative": {
                "logline": "怕雷的阿铃循铃声回到石桥。", "core_conflict": "阿铃需要顶住雷声守桥",
                "protagonist_flaw": "怕雷", "closure": "桥头听见铜铃，回到桥头再摇铃",
                "acts": [{"id": "act1", "title": "回桥", "beats": ["阿铃说出害怕，然后摇响铜铃"]}],
                "characters": [{**hero, "want": "守桥", "fear": "打雷", "flaw": "遇雷退缩", "growth": "留在桥头"},
                               {**mother, "want": "等儿子回家", "fear": "桥上打雷", "flaw": "着急",
                                "growth": "给孩子留出时间"}],
                "causal_chain": ["雷声使阿铃后退，铜铃使他回身"], "fill_ins": [], "removed": [],
            }},
            "X-01": {**ready, "summary": "对白接铜铃特写。", "title": "第1集 铜铃", "badge": "国风",
                     "title_image_prompt": "石桥起雾的竖屏全景", "end_image_prompt": "石桥边铜铃的独立特写",
                     "characters": [{"id": hero["id"], "name": "少年", "visual": hero["visual"],
                                     "voice_profile": "young_man"}, {**mother, "visual": "待定"}],
                     "shots": [{**direction, "id": "s01", "beat": "说出害怕", "kind": "dialogue",
                                "dialogue": {"speaker": "少年", "text": "我听见了。"}, "duration_seconds": 3},
                               {**direction, "id": "s02", "beat": "摇响铜铃", "kind": "action",
                                "action": "阿铃摇响铜铃", "duration_seconds": 4},
                               {**direction, "id": "s03", "beat": "安静落点", "kind": "action",
                                "action": "把铃放回手心", "duration_seconds": 3}],
                     "storyboard": {"tone": "克制奇幻", "camera_language": "从中景切向铜铃特写",
                                    "pacing_transitions": "响铃硬切", "emotion_curve": "0s 迟疑，3s 安定",
                                    "prohibitions": "禁止慢镜与叠化",
                                    "acts": [{"id": "act1", "last_shot_id": "s03", "audience_feeling": "安心"}]}},
            "X-02": {**ready, "summary": "墨青石桥，土红围巾定视觉重心。", "art": {
                "palette": ["墨青", "雾灰"], "warm_accent": "土红围巾", "materials": "细笔刻石纹",
                "lighting": "左侧云缝斜光", "composition": "桥栏斜向引出纵深", "prohibited_colors": "高饱和红",
                "character_consistency": "阿铃沿用靛青布衣、缺口铜铃和左眉伤疤",
                "scenes": [{"scene": "石桥", "anchor": "带裂口的桥栏", "light": "左侧云缝斜光"}],
                "characters": [{"id": "hero", "costume": "靛青布衣", "prop": "缺口铜铃", "face": "左眉疤"},
                               {"id": "mother", "pending:true": True}],
            }},
            "X-04": {**ready, "summary": "铜铃先入再升音量。", "sound": {
                "motifs": ["缺口铜铃"], "progression": "铜铃由轻到实", "environment": "石桥山风",
                "delivery": "压低说话", "hooks": "铃声提前衔接", "silence": "未使用",
            }, "shots": [{"id": "s01", "ambience": "wind", "sound_effect": "none", "sound_gain": 1,
                          "delivery": "低声", "delivery_mode": "low"},
                         {"id": "s02", "sound_effect": "bell", "sound_bridge": "prelap", "sound_gain": 1.2},
                         {"id": "s03", "silence": True, "ambience": "rain",
                          "sound_effect": "thunder", "sound_bridge": "prelap", "sound_gain": 1.6}]},
        }
    payload = {"workspace": str(project), "title": "石桥", "audio_mode": "dialogue", "text_route": model["name"],
               "video_route": "fixture-video", "text": f"{source_marker}。怕雷的阿铃听见铜铃，回到石桥守桥。"}
    task = terminal(base, payload, "/v1/manju/import")
    nodes = {node["id"]: node for node in task["flow_nodes"]}
    assert nodes["step1"]["status"] == "ok" and nodes["step1"]["llm_calls"] == 4, task
    assert nodes["step1"]["director_stage"] == "X-05", nodes
    board = json.loads((episode / "shots.json").read_text(encoding="utf-8"))
    production = board["production"]
    assert [item["id"] for item in production["stages"]] == ["X-03", "X-01", "X-02", "X-04"]
    assert len(production["director_brief"]) == 9 and len(production["director_prompt"]) <= 1200
    assert "裂口的桥栏" in board["shots"][0]["image_prompt"]
    assert board["shots"][1]["sound_bridge"] == "prelap" and board["shots"][0]["text"] == "我听见了。"
    assert board["shots"][0]["speaker"] == "hero" and "dialogue" not in board["shots"][0]
    assert board["characters"][0]["name"] == hero["name"] and board["characters"][0]["role"] == hero["role"]
    assert production["art"]["characters"][1] == {"id": "mother", "pending": True}
    assert "sound_gain" not in board["shots"][0] and board["shots"][2]["silence"] is True
    assert not {"ambience", "sound_effect", "sound_bridge", "sound_gain"} & board["shots"][2].keys()
    with provider.lock:
        calls = provider.director_calls[:]
        provider.director_steps["X-03"] = {"status": "needs_input", "missing": ["请补充故事结尾"]}
    assert [stage for stage, _ in calls] == ["X-03", "X-01", "X-02", "X-04"]
    assert source_marker in calls[0][1] and all(source_marker not in prompt for _, prompt in calls[1:])
    unresolved = terminal(base, {**payload, "episode": 2, "text": "他站在石桥上。"}, "/v1/manju/import")
    assert unresolved["status"] == "FAILED" and "请补充故事结尾" in unresolved["output"], unresolved
    assert not (project / "manju" / "ep02" / "shots.json").exists()
    with provider.lock:
        assert [stage for stage, _ in provider.director_calls[len(calls):]] == ["X-03"]
    print("PASS: four staged model requests, nine-part brief, locked shot handoffs, and early fallback for missing story")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=pathlib.Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="manju-video-") as tmp, (
        http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    ) as provider:
        provider.lock = threading.Lock()
        provider.submissions = []
        provider.reference_inputs = []
        provider.director_calls = []
        provider.director_steps = {}
        provider.durations = collections.Counter()
        provider.polls = collections.Counter()
        provider.defer_completion = True
        provider.fail_download = False
        provider.cut_download = False
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen(
            [str(args.binary.resolve()), "--state", tmp, "--workspace", tmp, "serve", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    request(base, "/v1/routes")
                    break
                except OSError:
                    assert process.poll() is None
                    time.sleep(0.1)
            else:
                raise RuntimeError("light-agent failed to start")
            route = {"name": "fixture-video", "provider": "openai", "model": MODEL,
                     "base_url": f"http://127.0.0.1:{provider.server_port}/compatible-mode/v1",
                     "api_key": "video-test"}
            status, routes = request(base, "/v1/routes", route)
            assert status == 200 and any(item.get("video_generation") for item in routes), routes
            assert request(base, "/v1/config/llm", route)[0] == 400
            test = probe(base, {"route_name": route["name"]})
            assert test["ok"] and provider.durations[MODEL, 3] == 1, test
            print("PASS: saved video route tests a 3s asynchronous generation and stays out of chat")
            for model, duration, prefix, mode in ((KLING, 3, "", "text"),
                    (SEEDANCE, 4, "/api/v3", "text"), (FIRST_FRAME, 3, "", "first_frame"),
                    (REFERENCE, 3, "", "reference")):
                branch = {**route, "name": model, "model": model,
                          "base_url": f"http://127.0.0.1:{provider.server_port}{prefix}"}
                status, routes = request(base, "/v1/routes", branch)
                assert status == 200
                assert next(item for item in routes if item["name"] == branch["name"])["video_input"] == mode
                assert request(base, "/v1/config/llm", branch)[0] == 400
                test = probe(base, {"route_name": branch["name"]})
                assert test["ok"] and provider.durations[model, duration] == 1, (model, test)
                print(f"PASS: {model} uses the corresponding asynchronous protocol")
            check_flow(base, provider, pathlib.Path(tmp))
            check_mixed(base, provider, pathlib.Path(tmp))
            check_character_references(base, provider, pathlib.Path(tmp))
            check_episode_batch(base, provider, pathlib.Path(tmp))
            check_character_batch_import(base, pathlib.Path(tmp))
            check_dialogue_import(base, provider, pathlib.Path(tmp))
            check_director_pipeline(base, provider, pathlib.Path(tmp))
        finally:
            process.terminate()
            process.wait(timeout=10)
            provider.shutdown()


if __name__ == "__main__":
    main()
