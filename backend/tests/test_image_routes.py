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
        payload = json.dumps({"data": [{"b64_json": image}]} if valid else {}).encode()
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
