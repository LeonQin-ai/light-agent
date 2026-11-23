"""Test video generation routing, saved task IDs, and ordered Manju clips."""

import argparse
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
import urllib.request

from test_image_routes import probe, request


MODEL = "happyhorse-1.1-t2v"
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
        if ark:
            prompt = next((entry.get("text") for entry in body.get("content", [])
                           if entry.get("type") == "text"), "")
            duration = body.get("duration")
            valid = (self.path == ARK_TASKS and duration == 4 and body.get("ratio") == "9:16"
                     and body.get("generate_audio") is False)
        else:
            prompt = body.get("input", {}).get("prompt", "")
            duration = params.get("duration")
            valid = (self.path == SUBMIT and self.headers.get("X-DashScope-Async") == "enable"
                     and duration in (3, 4)
                     and ((model == MODEL and params.get("ratio") == "9:16") or
                          (model == KLING and params.get("aspect_ratio") == "9:16" and params.get("mode") == "std")))
        if not valid or self.headers.get("Authorization") != TOKEN or not prompt:
            self.reply({"code": "InvalidFixtureRequest"}, 400)
            return
        with self.server.lock:
            self.server.durations[model, duration] += 1
            self.server.submissions.append(prompt)
            task_id = str(len(self.server.submissions))
        self.reply({"id": task_id} if ark else {"output": {"task_id": task_id, "task_status": "PENDING"}})

    def do_GET(self):
        ark = self.path.startswith(ARK_TASKS + "/")
        if (self.path.startswith(POLL) or ark) and self.headers.get("Authorization") == TOKEN:
            task_id = self.path.rsplit("/", 1)[-1]
            with self.server.lock:
                poll_count = self.server.polls[task_id] + 1
                self.server.polls[task_id] = poll_count
            if poll_count == 1:
                self.reply({"status": "running"} if ark else {"output": {"task_status": "RUNNING"}})
            else:
                url = f"http://127.0.0.1:{self.server.server_port}/result/{task_id}"
                self.reply({"status": "succeeded", "content": {"video_url": url}} if ark else
                           {"output": {"task_status": "SUCCEEDED", "video_url": url}})
        elif self.path.startswith("/result/") and not self.headers.get("Authorization"):
            with self.server.lock:
                fail = self.server.fail_download
                self.server.fail_download = False
            self.send(503 if fail else 200, b"" if fail else MP4, "video/mp4")
        else:
            self.reply({}, 400)


def terminal(base, flow):
    status, started = request(base, "/v1/flows", flow)
    assert status == 200, (status, started)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status, task = request(base, f"/v1/tasks/{started['id']}")
        assert status == 200, (status, task)
        if task["status"] not in ("RUNNING", "QUEUED"):
            return task
        time.sleep(0.1)
    raise RuntimeError("Video flow did not finish")


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=pathlib.Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="manju-video-") as tmp, (
        http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    ) as provider:
        provider.lock = threading.Lock()
        provider.submissions = []
        provider.durations = collections.Counter()
        provider.polls = collections.Counter()
        provider.fail_download = False
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
            for model, duration, prefix in ((KLING, 3, ""), (SEEDANCE, 4, "/api/v3")):
                branch = {**route, "name": model, "model": model,
                          "base_url": f"http://127.0.0.1:{provider.server_port}{prefix}"}
                assert request(base, "/v1/routes", branch)[0] == 200
                test = probe(base, {"route_name": branch["name"]})
                assert test["ok"] and provider.durations[model, duration] == 1, (model, test)
                print(f"PASS: {model} uses the corresponding asynchronous protocol")
            check_flow(base, provider, pathlib.Path(tmp))
        finally:
            process.terminate()
            process.wait(timeout=10)
            provider.shutdown()


if __name__ == "__main__":
    main()
