"""Test video generation routing, saved task IDs, and ordered Manju clips."""

import argparse
import base64
import collections
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
                       {**direction, "kind": "action", "ambience": "wind", "sound_effect": "bell"}]}
    payload = {"workspace": str(project), "audio_mode": "dialogue", "character_roster": roster,
               "storyboard": board, "video_route": "fixture-video", "clip_seconds": 4}
    with provider.lock:
        before = len(provider.submissions)
    invalid = {**board, "shots": [{**direction, "kind": "narration", "text": "旁白"}]}
    assert request(base, "/v1/manju/import", {**payload, "storyboard": invalid})[0] == 400
    assert not (episode / "shots.json").exists()

    # Fail preflight deterministically, without requiring a real TTS provider.
    (episode / "audio").write_text("unwritable as a directory", encoding="utf-8")
    status, started = request(base, "/v1/manju/import", payload)
    assert status == 200 and started["episodes"][0]["audio_mode"] == "dialogue", (status, started)
    saved = json.loads((episode / "shots.json").read_text(encoding="utf-8"))
    assert saved["characters"] == [hero] and saved["voice_cast"] == {"hero": voice}, saved
    assert saved["shots"][1]["kind"] == "action" and "text" not in saved["shots"][1]
    history = request(base, "/v1/flows/list")[1]
    dag = json.loads(next(item["dag"] for item in history["records"] if item["id"] == started["id"]))
    assert {"from": "step3", "to": "step2"} in dag["edges"], dag
    assert any(node.get("manju_voice_episode") == 1 and node["manju_clip_seconds"] == 4 for node in dag["nodes"])
    assert wait_task(base, started["id"])["status"] == "FAILED"
    with provider.lock:
        assert len(provider.submissions) == before
    print("PASS: workspace cast persists, dialogue boards are validated, and failed speech preflight prevents video requests")


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
            check_dialogue_import(base, provider, pathlib.Path(tmp))
        finally:
            process.terminate()
            process.wait(timeout=10)
            provider.shutdown()


if __name__ == "__main__":
    main()
