#!/usr/bin/env python3
"""Measure independent Cognitive OS runtimes with the built-in Mock LLM.

Linux/root and cgroup v2 are required. This operator-side load generator opens
loopback sockets in each Agent's network namespace, then immediately restores
its own namespace. Agents retain network=none, and no Docker socket is mounted.
Only containers labeled with this unique benchmark run are cleaned up.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import ctypes
import csv
import http.client
import json
import math
import os
from pathlib import Path
import secrets
import statistics
import subprocess
import threading
import time


MIB = 1024 * 1024
LIBC = ctypes.CDLL(None, use_errno=True)
CLONE_NEWNET = 0x40000000
HOST_NET = None


def docker(*args, timeout=45):
    result = subprocess.run(["docker", *map(str, args)], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-2000:])
    return result.stdout.strip()


def host_memory():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0]) * 1024
    return values


def cpu_ticks():
    values = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
    return sum(values), values[3] + values[4]


def percentile(values, p):
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)]


class AgentConnection(http.client.HTTPConnection):
    def __init__(self, namespace):
        super().__init__("127.0.0.1", 8080, timeout=5)
        self.namespace = namespace

    def connect(self):
        if LIBC.setns(self.namespace, CLONE_NEWNET) != 0:
            raise OSError(ctypes.get_errno(), "enter Agent network namespace")
        try:
            super().connect()
        finally:
            if LIBC.setns(HOST_NET, CLONE_NEWNET) != 0:
                # Never continue a load-generator thread in the wrong namespace.
                os._exit(90)


class Agent:
    def __init__(self, index, args, root, tag, token):
        self.index, self.token = index, token
        self.root = root / f"agent-{index:04d}"
        self.root.mkdir()
        for name in ["state", "workspace"]:
            path = self.root / name
            path.mkdir(mode=0o700)
            os.chown(path, 65532, 65532)
        self.container = docker(
            "create", "--name", f"la-capacity-{tag}-{index:04d}",
            "--label", f"com.light-agent.capacity-run={tag}",
            "--runtime=runc", "--init", "--user=65532:65532", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--network=none",
            "--ipc=private", "--memory", f"{args.memory_mib}m", "--memory-swap", f"{args.memory_mib}m",
            "--cpus=0.5", "--pids-limit=64", "--ulimit=nofile=1024:1024",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--mount", f"type=bind,src={self.root}/state,dst=/state",
            "--mount", f"type=bind,src={self.root}/workspace,dst=/workspace",
            "--log-driver=local", "--log-opt=max-size=1m", "--log-opt=max-file=1", "--log-opt=compress=false",
            "--env", f"COA_AUTH_KEY={token}", "--env=COA_LLM_PROVIDER=mock",
            "--env=COA_LLM_MODEL=mock", "--env=COA_HTTP_BIND=127.0.0.1",
            "--env", f"COA_SCHEDULER_WORKERS={args.workers}",
            "--entrypoint=/usr/local/bin/cognitive-os-agent", args.image,
            "--state", "/state", "--workspace", "/workspace", "serve", "8080")
        docker("start", self.container)
        self.open_namespace()
        self.cgroup = self.cgroup_path()
        deadline = time.monotonic() + 20
        while True:
            try:
                self.request("GET", "/v1/tools")
                break
            except (OSError, http.client.HTTPException):
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Agent {index} did not become ready")
                time.sleep(0.1)
        # Invalid tokens must not reach the task API.
        self.request("GET", "/v1/tools", token="wrong-token", expected=401)
        self.task(f"capacity readiness token {index}")

    def open_namespace(self):
        info = json.loads(docker("inspect", self.container))[0]
        profile = info["HostConfig"]
        assert profile["NetworkMode"] == "none" and profile["ReadonlyRootfs"]
        assert profile["Runtime"] == "runc" and "ALL" in profile["CapDrop"]
        assert info["Config"]["User"] == "65532:65532"
        mounts = {m["Destination"]: m["Source"] for m in info["Mounts"] if m["Type"] == "bind"}
        assert mounts["/state"] == str(self.root / "state")
        assert mounts["/workspace"] == str(self.root / "workspace")
        self.pid = info["State"]["Pid"]
        self.namespace = os.open(f"/proc/{self.pid}/ns/net", os.O_RDONLY)

    def cgroup_path(self):
        for line in Path(f"/proc/{self.pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                path = Path("/sys/fs/cgroup") / line.split("::", 1)[1].lstrip("/")
                if (path / "memory.current").exists():
                    return path
        raise RuntimeError("Agent cgroup v2 metrics are unavailable")

    def request(self, method, path, body=None, token=None, expected=200):
        connection = AgentConnection(self.namespace)
        try:
            connection.request(method, path, body=None if body is None else json.dumps(body),
                               headers={"Authorization": "Bearer " + (self.token if token is None else token),
                                        "Content-Type": "application/json", "Connection": "close"})
            response = connection.getresponse()
            raw = response.read()
            if response.status != expected:
                raise RuntimeError(f"Agent {self.index} HTTP {response.status}: {raw[:500]!r}")
            return json.loads(raw)
        finally:
            connection.close()

    def task(self, prompt):
        start = time.monotonic()
        task = self.request("POST", "/v1/tasks", {"prompt": prompt})
        deadline = start + 10
        while time.monotonic() < deadline:
            current = self.request("GET", f"/v1/tasks/{task['id']}")
            status = current["status"].lower()
            if status == "done":
                return (time.monotonic() - start) * 1000, current
            if status in {"failed", "cancelled", "timeout"}:
                raise RuntimeError(f"Agent {self.index} task failed: {current}")
            if current.get("approval"):
                raise RuntimeError("benchmark task unexpectedly requires approval")
            time.sleep(0.01)
        raise TimeoutError(f"Agent {self.index} task did not finish")

    def metrics(self):
        current = int((self.cgroup / "memory.current").read_text())
        stats = dict(line.split() for line in (self.cgroup / "cpu.stat").read_text().splitlines())
        events = dict(line.split() for line in (self.cgroup / "memory.events").read_text().splitlines())
        return current, int(stats["usage_usec"]), int(events.get("oom_kill", 0))


def main():
    global HOST_NET
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--levels", default="1,4,8,16,32,64,96,128,192,256")
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument("--period", type=float, default=0.5)
    parser.add_argument("--soak", type=float, default=60)
    parser.add_argument("--memory-mib", type=int, default=128)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--reserve-mib", type=int, default=600)
    parser.add_argument("--p95-ms", type=float, default=1000)
    parser.add_argument("--workload", choices=["ping", "file"], default="ping")
    args = parser.parse_args()
    if os.uname().sysname != "Linux" or os.geteuid() != 0:
        raise SystemExit("Linux root is required for Docker and network-namespace measurement")
    if not args.image.startswith("sha256:"):
        raise SystemExit("use an immutable operator-built local image ID")
    image_info = json.loads(docker("image", "inspect", args.image))[0]
    revision = (image_info["Config"].get("Labels") or {}).get("com.light-agent.runtime-commit")
    if revision != "6bdfc849bba6d196739372bf18d005076062f068":
        raise SystemExit("capacity image must identify the project's pinned Cognitive OS revision")
    levels = sorted(set(int(n) for n in args.levels.split(",")))
    if not levels or levels[0] < 1 or levels[-1] > 512 or args.period <= 0 or args.duration <= 0:
        raise SystemExit("invalid levels/duration/period (maximum test guard is 512 Agents)")
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o700)
    HOST_NET = os.open("/proc/self/ns/net", os.O_RDONLY)
    tag = secrets.token_hex(6)
    token = secrets.token_hex(32)
    agents, stages = [], []
    baseline = host_memory()
    abort = threading.Event()
    summary = {"model": "builtin_mock", "runtime_commit": "6bdfc849bba6d196739372bf18d005076062f068",
               "image": args.image, "host_cpus": os.cpu_count(), "host_total_mib": baseline["MemTotal"] / MIB,
               "host_initial_available_mib": baseline["MemAvailable"] / MIB,
               "profile": {"memory_limit_mib": args.memory_mib, "cpu_limit": 0.5,
                           "workload": args.workload,
                           "scheduler_workers": args.workers, "target_tasks_per_second_per_agent": 1 / args.period,
                           "p95_slo_ms": args.p95_ms, "host_memory_reserve_mib": args.reserve_mib},
               "stages": stages}

    def save():
        (root / "results.json").write_text(json.dumps(summary, indent=2))

    def run_stage(duration, soak=False):
        idle = [a.metrics()[0] for a in agents]
        start_cpu = sum(a.metrics()[1] for a in agents)
        start_ticks = cpu_ticks()
        start_client_cpu = time.process_time()
        gate = threading.Event()
        finish_sampling = threading.Event()
        samples = []

        def sample():
            while not finish_sampling.is_set():
                available = host_memory()["MemAvailable"]
                metrics = [a.metrics() for a in agents]
                samples.append({"available": available, "memory": sum(m[0] for m in metrics),
                                "oom_kills": sum(m[2] for m in metrics)})
                if available < args.reserve_mib * MIB or samples[-1]["oom_kills"]:
                    abort.set()
                finish_sampling.wait(0.5)

        def load(agent):
            gate.wait()
            end = time.monotonic() + duration
            latencies, errors = [], []
            sequence = 0
            while time.monotonic() < end and not abort.is_set():
                tick = time.monotonic()
                marker = f"capacity token {agent.index} sequence {sequence}"
                try:
                    prompt = marker if args.workload == "ping" else "创建文件 benchmark.txt 写入内容为 " + marker
                    latency, result = agent.task(prompt)
                    if args.workload == "ping" and marker not in result.get("output", ""):
                        raise AssertionError("runtime output did not contain its input marker")
                    if args.workload == "file" and (agent.root / "workspace/benchmark.txt").read_text().strip() != marker:
                        raise AssertionError("real file tool did not persist this task's marker")
                    latencies.append(latency)
                except Exception as exc:
                    errors.append(str(exc)[:500])
                    if len(errors) >= 3:
                        break
                sequence += 1
                abort.wait(max(0, args.period - (time.monotonic() - tick)))
            return latencies, errors

        sampler = threading.Thread(target=sample)
        sampler.start()
        started = time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=len(agents)) as pool:
                futures = [pool.submit(load, a) for a in agents]
                gate.set()
                outputs = [f.result() for f in futures]
        finally:
            finish_sampling.set()
            sampler.join()
        elapsed = time.monotonic() - started
        latencies = [v for values, _ in outputs for v in values]
        errors = [v for _, values in outputs for v in values]
        final_ticks = cpu_ticks()
        total = final_ticks[0] - start_ticks[0]
        busy = 100 * (1 - (final_ticks[1] - start_ticks[1]) / total) if total else 0
        row = {"agents": len(agents), "soak": soak, "duration_seconds": round(elapsed, 3),
               "completed": len(latencies), "errors": len(errors), "error_examples": errors[:8],
               "tasks_per_second": round(len(latencies) / elapsed, 3),
               "latency_p50_ms": percentile(latencies, .50), "latency_p95_ms": percentile(latencies, .95),
               "latency_p99_ms": percentile(latencies, .99),
               "idle_memory_total_mib": sum(idle) / MIB,
               "idle_memory_per_agent_mib": statistics.mean(idle) / MIB,
               "peak_memory_total_mib": max(s["memory"] for s in samples) / MIB,
               "host_min_available_mib": min(s["available"] for s in samples) / MIB,
               "host_cpu_busy_percent": round(busy, 2),
               "agent_cpu_cores": round((sum(a.metrics()[1] for a in agents) - start_cpu) / 1e6 / elapsed, 3),
               "load_generator_cpu_cores": round((time.process_time() - start_client_cpu) / elapsed, 3),
               "oom_kills": max(s["oom_kills"] for s in samples)}
        target = len(agents) * duration / args.period
        row["stable"] = bool(not abort.is_set() and not errors and len(latencies) >= .95 * target
                             and row["latency_p95_ms"] is not None and row["latency_p95_ms"] <= args.p95_ms)
        stages.append(row)
        save()
        print(json.dumps(row), flush=True)
        return row

    try:
        for level in levels:
            for index in range(len(agents), level):
                if host_memory()["MemAvailable"] < (args.reserve_mib + args.memory_mib) * MIB:
                    summary["stop_reason"] = "memory headroom guard before creating next Agent"
                    abort.set()
                    break
                agents.append(Agent(index, args, root, tag, token))
                summary["peak_started_agents"] = len(agents)
                if len(agents) % 16 == 0:
                    print("STARTED", len(agents), flush=True)
            if abort.is_set():
                break
            if level == levels[0]:
                marker = "capacity-isolation-token"
                _, result = agents[0].task("创建文件 capacity.txt 写入内容为 " + marker)
                assert (agents[0].root / "workspace/capacity.txt").read_text().strip() == marker, result
                summary["file_tool_verified"] = True
                old_namespace = agents[0].namespace
                docker("restart", "--time=2", agents[0].container)
                os.close(old_namespace)
                agents[0].open_namespace()
                agents[0].cgroup = agents[0].cgroup_path()
                time.sleep(.5)
                agents[0].task("capacity token after restart")
                assert (agents[0].root / "workspace/capacity.txt").read_text().strip() == marker
                summary["restart_persistence_verified"] = True
            row = run_stage(args.duration)
            if not row["stable"]:
                summary["stop_reason"] = "load SLO or resource guard reached"
                break
        stable = [r for r in stages if r["stable"]]
        summary["largest_stable_agents"] = max((r["agents"] for r in stable), default=0)
        if stable:
            keep = summary["largest_stable_agents"]
            for agent in agents[keep:]:
                docker("rm", "--force", agent.container)
                os.close(agent.namespace)
            agents = agents[:keep]
            if host_memory()["MemAvailable"] >= (args.reserve_mib + args.memory_mib) * MIB:
                abort.clear()
            if args.soak > 0 and not abort.is_set():
                summary["soak_passed"] = run_stage(args.soak, True)["stable"]
                summary["largest_soak_stable_agents"] = keep if summary["soak_passed"] else 0
        summary.setdefault("stop_reason", "configured maximum tested; physical maximum not established")
        save()
    finally:
        # Query by this run's unique label, covering ambiguous Create responses.
        ids = docker("ps", "-aq", "--filter", f"label=com.light-agent.capacity-run={tag}").split()
        if ids:
            docker("rm", "--force", *ids, timeout=120)
        for agent in agents:
            os.close(agent.namespace)
        os.close(HOST_NET)
        summary["cleanup_remaining_containers"] = docker("ps", "-aq", "--filter", f"label=com.light-agent.capacity-run={tag}")
        save()
        if stages:
            fields = [k for k in stages[0] if k != "error_examples"]
            with (root / "results.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(stages)


if __name__ == "__main__":
    main()
