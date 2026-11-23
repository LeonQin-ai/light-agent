"""Exercise saved image-route probes with a local Images API and optional live Qwen."""

import argparse
import collections
import http.server
import json
import pathlib
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGOQCT4CAAHCATTzUQtLAAAAAElFTkSuQmCC"
CORRUPT_PNG = PNG.replace("zUQt", "zVQt")


class ImagesProvider(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/chat/completions":
            valid = (
                self.headers.get("Authorization") == "Bearer local-story"
                and body.get("model") == "deepseek-v4.1-flash-1017"
                and body.get("thinking", {}).get("type") == "disabled"
                and not body.get("tools")
                and "BEGIN_CHAPTER" in body["messages"][-1]["content"]
                and body["messages"][-1]["content"].endswith("END_CHAPTER")
            )
            self.server.story_calls += 1
            board = {"title": "Episode 2", "badge": "Fixture",
                     "title_image_prompt": "Opening landscape", "end_image_prompt": "Closing landscape",
                     "characters": [], "shots": [{"kind": "narration", "text": "A new day.",
                         "speaker": "narrator", "character_ids": [], "scene": "Village",
                         "image_prompt": "Village morning, portrait 9:16"}]}
            content = "{\"shots\":[" if self.server.story_calls == 1 else json.dumps(board)
            self.reply(valid, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 80, "completion_tokens": 40}})
            return
        valid = (
            self.path == "/v1/images/generations"
            and self.headers.get("Authorization") == "Bearer local-fixture"
            and body.get("model") in ("local-drawing-model", "local-broken-model")
            and isinstance(body.get("prompt"), str)
            and "messages" not in body
        )
        image = CORRUPT_PNG if body.get("model") == "local-broken-model" else PNG
        fixture = next((sample for sample in ("first-scene", "second-scene")
                        if valid and sample in body["prompt"]), None)
        if valid and fixture:
            with self.server.paint_lock:
                self.server.paint_calls[fixture] += 1
                if fixture == "second-scene" and self.server.fail_second:
                    valid = False
        self.reply(valid, {"data": [{"b64_json": image}]})

    def reply(self, valid, body):
        payload = json.dumps(body if valid else {}).encode()
        self.send_response(200 if valid else 400)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def request(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as result:
            return result.status, json.load(result)
    except urllib.error.HTTPError as err:
        return err.code, json.load(err)


def probe(base, payload):
    status, started = request(base, "/v1/config/llm/test", payload)
    assert status == 200 and started["pending"], (status, started)
    deadline = time.monotonic() + 160
    while time.monotonic() < deadline:
        status, result = request(base, f"/v1/config/llm/test?request_id={started['request_id']}")
        assert status == 200, (status, result)
        if not result["pending"]:
            return result
        time.sleep(0.25)
    raise RuntimeError("Image route test timed out")


def story(base, provider, workspace):
    chapter = workspace / "novel" / "chapter02.md"
    chapter.parent.mkdir()
    chapter.write_text("BEGIN_CHAPTER\n第二章\nEND_CHAPTER", encoding="utf-8")
    assert request(base, "/v1/agents", {"name": "manju-director", "role": "Storyboard"})[0] == 200
    assert request(base, "/v1/routes", {
        "name": "local-story", "provider": "openai", "model": "deepseek-v4.1-flash-1017",
        "api_key": "local-story", "base_url": f"http://127.0.0.1:{provider.server_port}",
    })[0] == 200
    flow = {"nodes": [{"id": "step1", "agent": "manju-director", "task": "Adapt chapter 2",
                       "manju_story_episode": 2, "manju_chapter": "novel/chapter02.md",
                       "manju_text_route": "local-story"}], "edges": []}
    for expected_calls in (2, 0):
        status, started = request(base, "/v1/flows", flow)
        assert status == 200, (status, started)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            _, task = request(base, f"/v1/tasks/{started['id']}")
            if task["status"] not in ("RUNNING", "QUEUED"):
                break
            time.sleep(0.1)
        assert task["status"] == "DONE", task
        assert task["flow_nodes"][0]["llm_calls"] == expected_calls, task
    assert provider.story_calls == 2
    board = json.loads((workspace / "manju" / "ep02" / "shots.json").read_text(encoding="utf-8"))
    assert board["shots"][0]["speaker"] == "narrator"
    print("PASS: Storyboard sends the complete chapter, repairs malformed JSON once and reuses valid output")


def paint(base, provider, workspace):
    assert request(base, "/v1/agents", {"name": "manju-artist", "role": "Paint the episode"})[0] == 200
    episode = workspace / "manju" / "ep01"
    episode.mkdir(parents=True)
    shots = {"title": "Test episode", "badge": "Fixture", "shots": [
        {"kind": "narration", "text": "The scene opens.", "image_prompt": "first-scene"},
        {"kind": "dialogue", "text": "This is a line.", "image_prompt": "second-scene"}
    ]}
    (episode / "shots.json").write_text(json.dumps(shots), encoding="utf-8")
    flow = {"name": "Painting integration fixture", "flow": {"nodes": [{
        "id": "step2", "agent": "manju-artist", "task": "Produce validated episode panels",
        "manju_paint_episode": 1, "manju_image_route": "local-image"
    }], "edges": []}}
    frames = episode / "frames"
    manifest = frames / "panels.json"

    def run():
        status, started = request(base, "/v1/flows", flow)
        assert status == 200, (status, started)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status, task = request(base, "/v1/tasks/" + str(started["id"]))
            assert status == 200, (status, task)
            if task["status"] not in ("RUNNING", "QUEUED"):
                return task
            time.sleep(0.1)
        raise RuntimeError("Painting flow timed out")

    failed = run()
    assert failed["status"] == "FAILED" and "HTTP 400" in failed["output"], failed
    assert not manifest.exists(), "A partial manifest was published"
    assert len(list(frames.glob("ai_*.png"))) == 1
    with provider.paint_lock:
        provider.fail_second = False
    # A saved flow from before native painting should resume through the
    # configured image route instead of recreating procedural placeholders.
    legacy = flow["flow"]["nodes"][0]
    del legacy["manju_paint_episode"], legacy["manju_image_route"]
    legacy["task"] = ('依据镜头脚本 shots.json 渲染漫剧第1集全部画面：'
                      'python "tools/manju/manju_pipeline.py" panels --shots "shots.json" --out "frames" --ep 1。')
    completed = run()
    assert completed["status"] == "DONE", completed
    with provider.paint_lock:
        assert provider.paint_calls == {"first-scene": 1, "second-scene": 2}, provider.paint_calls
    panels = json.loads(manifest.read_text(encoding="utf-8"))
    assert panels["image_model"] == "local-drawing-model"
    assert [item["role"] for item in panels["panels"]] == ["title", "shot", "shot", "end"]
    assert [item["kind"] for item in panels["panels"]] == ["title", "narration", "dialogue", "end"]
    assert all((frames / item["file"]).is_file() for item in panels["panels"])
    assert (frames / panels["poster"]).is_file()
    print("PASS: Painting keeps valid panels, retries only the failed panel and publishes a complete manifest")


def character_views(base, workspace):
    episode = workspace / "manju" / "ep01"
    board = json.loads((episode / "shots.json").read_text(encoding="utf-8"))
    board["characters"] = [{"id": "hero", "name": "Hero", "visual": "Short black hair, blue jacket"}]
    (episode / "shots.json").write_text(json.dumps(board), encoding="utf-8")
    flow = {"nodes": [{"id": "character", "agent": "manju-artist", "task": "Produce a turnaround",
                       "manju_character_episode": 1, "manju_image_route": "local-image"}],
            "edges": []}
    for expected_calls in (1, 0):
        status, started = request(base, "/v1/flows", flow)
        assert status == 200, (status, started)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status, task = request(base, f"/v1/tasks/{started['id']}")
            assert status == 200, (status, task)
            if task["status"] not in ("RUNNING", "QUEUED"):
                break
            time.sleep(0.1)
        assert task["status"] == "DONE", task
        assert task["flow_nodes"][0]["image_requests"] == expected_calls, task
    directory = episode / "characters"
    cast = json.loads((directory / "characters.json").read_text(encoding="utf-8"))
    assert cast["image_model"] == "local-drawing-model"
    assert cast["characters"][0]["id"] == "hero" and (directory / cast["characters"][0]["file"]).is_file()
    print("PASS: Character views use the selected image route, preserve cast IDs, and reuse completed assets")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("--live-routes", type=pathlib.Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="light-agent-images-") as tmp, (
        http.server.ThreadingHTTPServer(("127.0.0.1", 0), ImagesProvider)
    ) as provider:
        provider.paint_lock = threading.Lock()
        provider.paint_calls = collections.Counter()
        provider.fail_second = True
        provider.story_calls = 0
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            number = port.getsockname()[1]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [str(args.binary.resolve()), "--state", tmp, "--workspace", tmp, "serve", str(number)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
        )
        try:
            base = f"http://127.0.0.1:{number}"
            for _ in range(100):
                try:
                    request(base, "/v1/routes")
                    break
                except urllib.error.URLError:
                    assert process.poll() is None, "light-agent exited before serving"
                    time.sleep(0.1)
            else:
                raise RuntimeError("light-agent did not start")
            image_route = {
                "name": "local-image",
                "provider": "openai",
                "api_key": "local-fixture",
                "base_url": f"http://127.0.0.1:{provider.server_port}/v1/",
                "model": "local-drawing-model",
                "caps": "image_generation",
            }
            status, routes = request(base, "/v1/routes", image_route)
            assert status == 200
            assert next(r for r in routes if r["name"] == "local-image")["image_generation"]
            status, _ = request(base, "/v1/config/llm", {
                k: v for k, v in image_route.items() if k != "caps"
            })
            assert status == 400, "Image route was accepted as the chat model"
            result = probe(base, {"route_name": "local-image"})
            assert result["ok"] and "image/png" in result["reply"], result
            print("PASS: Saved image route uses Images API and returns a PNG")
            corrupt = probe(base, {**image_route, "model": "local-broken-model"})
            assert not corrupt["ok"] and "complete PNG" in corrupt["error"], corrupt
            print("PASS: Corrupt PNG response is rejected")
            paint(base, provider, pathlib.Path(tmp))
            character_views(base, pathlib.Path(tmp))
            story(base, provider, pathlib.Path(tmp))
            if args.live_routes:
                routes = json.loads(args.live_routes.read_text(encoding="utf-8-sig"))
                live = next(r for r in routes if r["model"] == "qwen-image-3.0-pro")
                result = probe(base, {
                    k: live[k] for k in ("provider", "model", "base_url", "api_key")
                })
                assert result["ok"] and "image/png" in result["reply"], result
                print(f"PASS: Configured Qwen Token Plan generated an image. {result['reply']}")
        finally:
            process.terminate()
            process.wait(timeout=10)
            provider.shutdown()


if __name__ == "__main__":
    main()
