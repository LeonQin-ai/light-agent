#!/usr/bin/env python3
"""Exercise real Control Plane -> Sandbox Manager -> Docker Worker execution.

Run on a disposable Linux test host with Docker access. The image must already
contain /usr/local/bin/cos-execd, /bin/echo, /bin/false and /bin/sleep. All service
state is isolated under --work-dir; no existing service is stopped or reconfigured.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check, description, timeout=15):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = check()
            if last:
                return last
        except (OSError, urllib.error.URLError, sqlite3.Error):
            pass
        time.sleep(0.1)
    raise AssertionError(f"Timed out: {description}; last value={last!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandboxd", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.uname().sysname != "Linux":
        raise SystemExit("Linux is required")
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o700)
    manager_url = f"http://127.0.0.1:{unused_port()}"
    control_url = f"http://127.0.0.1:{unused_port()}"
    manager_token, api_token = secrets.token_hex(32), secrets.token_hex(32)
    db_path = root / "control.db"
    docker_state = root / "containers.json"
    manager_env = dict(os.environ, LIGHT_AGENT_SANDBOX_LISTEN_ADDR=manager_url.split("//")[1],
                       LIGHT_AGENT_SANDBOX_API_TOKEN=manager_token,
                       LIGHT_AGENT_SANDBOX_STATE=str(root / "vms.json"),
                       LIGHT_AGENT_DOCKER_STATE=str(docker_state),
                       LIGHT_AGENT_WORKSPACE_BASE=str(root / "workspaces"),
                       JAILER_CHROOT_BASE=str(root / "jailer"),
                       LIGHT_AGENT_WORKER_IMAGE=args.image)
    control_env = dict(os.environ, LIGHT_AGENT_LISTEN_ADDR=control_url.split("//")[1],
                       LIGHT_AGENT_DB_PATH=str(db_path), LIGHT_AGENT_API_TOKEN=api_token,
                       LIGHT_AGENT_SANDBOX_MANAGER_URL=manager_url,
                       LIGHT_AGENT_SANDBOX_MANAGER_TOKEN=manager_token,
                       FEISHU_VERIFICATION_TOKEN="", FEISHU_ENCRYPT_KEY="",
                       FEISHU_ALLOWED_OPEN_IDS="")
    # Do not route loopback requests through a host's outbound HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    logs = []
    processes = []
    checks = []

    def request(base, token, method, path, body=None, expected=200):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
        try:
            response = opener.open(req, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            assert response.code == expected, (method, path, response.code, raw)
            return json.loads(raw) if raw else None

    def api(method, path, body=None, expected=200):
        return request(control_url, api_token, method, path, body, expected)

    def start(binary, env, name, url):
        log = (root / (name + ".log")).open("ab")
        logs.append(log)
        process = subprocess.Popen([str(binary.resolve())], env=env, stdout=log,
                                   stderr=subprocess.STDOUT)
        processes.append(process)
        wait_for(lambda: request(url, "", "GET", "/healthz"), name + " startup", 30)
        return process

    def stop(process, sig=signal.SIGTERM):
        if process.poll() is None:
            process.send_signal(sig)
            try:
                process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def submit(agent, executable, argv=(), timeout=15000, request_id=None):
        return api("POST", f"/v1/agents/{agent}/executions", {
            "request_id": request_id or secrets.token_hex(12),
            "memory_bytes": 512 * 1024 * 1024,
            "command": {"executable": executable, "argv": list(argv), "timeout_ms": timeout},
        }, 202)

    def terminal(task):
        current = api("GET", "/v1/tasks/" + task["task_id"])
        return current if current["state"] in {"COMPLETED", "FAILED", "CANCELLED"} else None

    def worker_count():
        with sqlite3.connect(db_path) as db:
            return db.execute("SELECT COUNT(*) FROM task_workers").fetchone()[0]

    def running_worker(task):
        with sqlite3.connect(db_path) as db:
            row = db.execute("SELECT container_id FROM task_workers WHERE task_id=?",
                             (task["task_id"],)).fetchone()
        if row:
            record = request(manager_url, manager_token, "GET", "/v1/containers/" + row[0])
            if record["state"] == "RUNNING":
                return record
        return None

    def passed(name):
        checks.append(name)
        print("PASS", name, flush=True)

    try:
        manager = start(args.sandboxd, manager_env, "sandboxd", manager_url)
        control = start(args.control, control_env, "control", control_url)
        caps = api("GET", "/v1/runtime/capabilities")
        assert caps["observation_scope"] == "host" and caps["docker"]["runc_available"], caps
        assert caps["registry_version"] > 0
        first_version = caps["registry_version"]
        agent = api("POST", "/v1/agents", {"tenant_id": "linux-smoke"}, 201)["agent_id"]
        request(control_url, "invalid", "GET", "/v1/runtime/capabilities", expected=401)
        passed("authenticated Host capabilities and persisted version")

        task = submit(agent, "/bin/echo", ["linux-worker-ok"], request_id="idempotent-smoke")
        result = wait_for(lambda: terminal(task), "echo task")
        assert result["state"] == "COMPLETED", result
        output = json.loads(result["result"])
        assert output["result"]["stdout"].strip() == "linux-worker-ok", output
        assert output["capability_version"] >= first_version
        request(manager_url, manager_token, "DELETE", "/v1/containers/" + output["worker_id"], expected=204)
        duplicate = submit(agent, "/bin/echo", ["linux-worker-ok"], request_id="idempotent-smoke")
        assert duplicate["task_id"] == task["task_id"]
        api("POST", f"/v1/agents/{agent}/executions", {
            "request_id": "idempotent-smoke", "command": {"executable": "/bin/false"}}, 409)
        passed("durable echo, output, capability audit version and idempotency")

        for executable, argv, timeout in [("/bin/false", [], 15000), ("/bin/sleep", ["2"], 100)]:
            result = wait_for(lambda t=submit(agent, executable, argv, timeout): terminal(t), "command failure")
            assert result["state"] == "FAILED" and result["error_code"] == "command_failed", result
        passed("nonzero exit and command timeout persist failure")

        task = submit(agent, "/bin/sleep", ["30"], 60000)
        record = wait_for(lambda: running_worker(task), "running Worker")
        profile = json.loads(subprocess.check_output(["docker", "inspect", record["docker_id"]]))[0]
        host = profile["HostConfig"]
        assert host["ReadonlyRootfs"] and host["NetworkMode"] == "none" and host["Runtime"] == "runc"
        assert "ALL" in host["CapDrop"] and "no-new-privileges:true" in host["SecurityOpt"]
        assert profile["Config"]["User"] == "65532:65532" and host["Memory"] > 0 and host["PidsLimit"] > 0
        api("POST", "/v1/tasks/" + task["task_id"] + "/cancel", {})
        wait_for(lambda: worker_count() == 0, "cancel cleanup")
        assert terminal(task)["state"] == "CANCELLED"
        passed("actual hardened container profile and running cancellation cleanup")

        task = submit(agent, "/bin/sleep", ["30"], 60000)
        wait_for(lambda: running_worker(task), "Worker before Control Plane crash")
        control.kill()
        control.wait(timeout=5)
        control = start(args.control, control_env, "control-restarted", control_url)
        result = wait_for(lambda: terminal(task), "interrupted task reconciliation")
        assert result["state"] == "FAILED" and result["error_code"] == "execution_interrupted", result
        assert worker_count() == 0
        assert api("GET", "/v1/runtime/capabilities")["registry_version"] > first_version
        passed("crash recovery destroys tracked Workers without replay and advances version")

        api("POST", "/v1/execution/run", {"task_id": "untrusted", "trust_tier": "external_untrusted",
                                         "executable": "/bin/true"}, 403)
        message = api("POST", f"/v1/agents/{agent}/messages", {
            "request_id": "message-smoke", "content": "run something"}, 202)
        result = wait_for(lambda: terminal(message), "missing reasoning driver")
        assert result["error_code"] == "runtime_driver_unavailable", result
        passed("untrusted execution and unavailable reasoning driver fail closed")

        stop(manager, signal.SIGINT)
        wait_for(lambda: api("GET", "/v1/runtime/capabilities")["observation_scope"] == "unavailable",
                 "capability refresh revocation", 40)
        task = submit(agent, "/bin/echo", ["must-not-run"])
        result = wait_for(lambda: terminal(task), "unavailable admission result")
        assert result["state"] == "FAILED" and worker_count() == 0, result
        manager = start(args.sandboxd, manager_env, "sandboxd-restarted", manager_url)
        wait_for(lambda: api("GET", "/v1/runtime/capabilities")["observation_scope"] == "host",
                 "capability recovery", 40)
        task = submit(agent, "/bin/echo", ["recovered"])
        result = wait_for(lambda: terminal(task), "execution after recovery")
        assert result["state"] == "COMPLETED", result
        passed("real Manager outage revokes readiness and recovery restores execution")

        assert worker_count() == 0
        assert not json.loads(docker_state.read_text())
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM capability_observations").fetchone()[0] >= 3
        (root / "result.json").write_text(json.dumps({"passed": checks, "kvm_available": caps["kvm"]["available"]}, indent=2))
        print("Docker end-to-end validation passed; artifacts:", root, flush=True)
    finally:
        for process in reversed(processes):
            stop(process, signal.SIGINT)
        # Remove only Docker IDs in this test's private Manager state file.
        if docker_state.exists():
            for record in json.loads(docker_state.read_text()).values():
                subprocess.run(["docker", "rm", "--force", record["docker_id"]],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
