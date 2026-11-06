# Docker/runc Worker Provider

## Trust boundary

The Rust Sandbox Manager is the only component allowed to access the Docker Engine socket. Do not mount the socket into the Go Control Plane, Cognitive OS SuperAgent, or Worker containers.

This provider is a hardened container boundary, not a MicroVM. It shares the Host kernel. Use Firecracker for arbitrary high-risk code when KVM is available. Do not silently downgrade a MicroVM-required task to runc.

## Build and preload an immutable Worker image

Build the image from the repository root:

\`\`\`bash
docker build -f deploy/Dockerfile.worker -t light-agent/worker:dev .
docker image inspect light-agent/worker:dev --format '{{index .RepoDigests 0}}'
\`\`\`

Push the image to a trusted registry, then preload the exact digest on the Host:

\`\`\`bash
docker pull registry.example.com/light-agent/worker@sha256:<64-hex-digest>
\`\`\`

Set \`LIGHT_AGENT_WORKER_IMAGE\` to that full digest reference. The Sandbox Manager does not pull task-selected images and rejects mutable tags.

## Host configuration

The Rust daemon must run on the Docker Host with access to the Docker socket and workspace base. Workspace preparation changes ownership to UID/GID 65532, so the daemon needs root or `CAP_CHOWN` plus the required directory permissions. Access to `docker.sock` is effectively Host-root access; protect the daemon API token and restrict the listener to loopback or a private, firewall-restricted management network.

\`\`\`bash
export LIGHT_AGENT_SANDBOX_API_TOKEN="$(openssl rand -hex 32)"
export LIGHT_AGENT_SANDBOX_LISTEN_ADDR="127.0.0.1:9090"
export LIGHT_AGENT_DOCKER_SOCKET="/var/run/docker.sock"
export LIGHT_AGENT_WORKER_IMAGE="registry.example.com/light-agent/worker@sha256:<64-hex-digest>"
export LIGHT_AGENT_WORKSPACE_BASE="/var/lib/light-agent/workspaces"
\`\`\`

## API lifecycle

All endpoints require \`Authorization: Bearer $LIGHT_AGENT_SANDBOX_API_TOKEN\`.

Create a stopped container:

\`\`\`bash
curl -fsS -X POST http://127.0.0.1:9090/v1/containers \\
  -H "Authorization: Bearer $LIGHT_AGENT_SANDBOX_API_TOKEN" \\
  -H 'Content-Type: application/json' \\
  -d '{"worker_id":"worker-01","workspace_id":"task-001","cpu_millis":1000,"memory_bytes":1073741824,"pids_limit":128}'
\`\`\`

Start it:

\`\`\`bash
curl -fsS -X POST http://127.0.0.1:9090/v1/containers/<container-id>/start \\
  -H "Authorization: Bearer $LIGHT_AGENT_SANDBOX_API_TOKEN"
\`\`\`

Execute a structured command (no shell command string):

\`\`\`bash
curl -fsS -X POST http://127.0.0.1:9090/v1/containers/<container-id>/exec \\
  -H "Authorization: Bearer $LIGHT_AGENT_SANDBOX_API_TOKEN" \\
  -H 'Content-Type: application/json' \\
  -d '{"executable":"/usr/bin/python3","argv":["-c","print(1+1)"],"cwd":"","timeout_ms":15000,"memory_bytes":536870912,"max_output_bytes":1048576,"network":false}'
\`\`\`

Stop/cancel and destroy:

\`\`\`bash
# stop or cancel terminates the Worker container; any in-flight exec is interrupted
curl -fsS -X POST http://127.0.0.1:9090/v1/containers/<container-id>/cancel \
  -H "Authorization: Bearer $LIGHT_AGENT_SANDBOX_API_TOKEN"
curl -fsS -X DELETE http://127.0.0.1:9090/v1/containers/<container-id> \
  -H "Authorization: Bearer $LIGHT_AGENT_SANDBOX_API_TOKEN"
\`\`\`

## Enforced container profile

- Immutable, operator-preloaded image digest; no task-supplied image.
- Explicit Docker runtime \`runc\`; no automatic fallback.
- UID/GID 65532; read-only root filesystem.
- Writable bind mount limited to one per-Worker workspace.
- \`--cap-drop=ALL\`, \`no-new-privileges\`, Docker default seccomp profile.
- No network (\`--network=none\`), no devices, no Host PID/network namespace.
- CPU, memory, swap, PID, tmpfs and log-size bounds.
- Structured executable + argv through \`cos-execd --stdio\`; no shell interpolation.
- Per-command timeout, RLIMIT_CPU/RLIMIT_AS, bounded stdout/stderr and seccomp.
- Docker containers created by this provider are removed on Sandbox Manager restart.

The initial implementation intentionally does not support network-enabled tasks, gVisor, arbitrary mounts, image pulls, privileged containers, or Docker API passthrough.

## Operational notes

The Sandbox Manager must run on the Docker Host. The Docker CLI path and socket path are configurable. Worker workspaces are reserved until the associated container record is destroyed; the manager rejects a second Worker using the same workspace ID. The Worker workspace is owned by UID/GID 65532 to permit non-root writes.

When the Go Control Plane runs in Docker, do not point it at the Host daemon's loopback-only address. Put the Host API behind an HTTPS reverse proxy bound to a private Host interface, preserve the Authorization header, and firewall the listener to the Control Plane network. The Go Host-capability client rejects non-loopback plain HTTP URLs. The Rust Sandbox Manager's own API token must be at least 32 random characters when its listener is not loopback.

A Docker/runc container still shares the Host kernel. Treat it as a useful boundary for approved internal work, not as equivalent to Firecracker.


## Limits and current gaps

- No per-workspace disk quota is configured by this provider. Put `LIGHT_AGENT_WORKSPACE_BASE` on a quota-controlled filesystem and monitor free space.
- Docker block-I/O throttling is not configured yet.
- The provider exposes a direct authenticated Rust API. Go task-dispatcher integration, task-tier admission and remote-worker routing are separate follow-up work.
- This provider is runc only. It is not a gVisor provider and must not be used as an automatic fallback for MicroVM-required tasks.

## Go Control Plane execution entrypoint

Configure the Go service with the same Host Manager URL and a dedicated manager token:

```bash
export LIGHT_AGENT_SANDBOX_MANAGER_URL="http://127.0.0.1:9090"
export LIGHT_AGENT_SANDBOX_MANAGER_TOKEN="$LIGHT_AGENT_SANDBOX_API_TOKEN"
```

The authenticated Control Plane exposes a synchronous trusted-internal execution route. This route is intended for operator-approved internal maintenance/build tasks; do not use it to run arbitrary external prompts or user-submitted code.

```bash
curl -fsS -X POST http://127.0.0.1:8080/v1/execution/run \
  -H "Authorization: Bearer $LIGHT_AGENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "mode":"strict",
    "trust_tier":"trusted_internal",
    "task_id":"smoke-001",
    "cpu_millis":1000,
    "memory_bytes":1073741824,
    "pids_limit":128,
    "executable":"/bin/echo",
    "argv":["hello-from-worker"],
    "cwd":"",
    "timeout_ms":15000,
    "max_output_bytes":1048576
  }'
```

The scheduler resolves the backend against the authenticated Host capability snapshot. Docker/runc is selected only for `trusted_internal`. `external_untrusted` is denied unless a preflight-ready Firecracker MicroVM is available; the current Go scheduler intentionally does not downgrade it to Docker. `constrained_extension` requires a ready WASM provider, which is not wired yet.

The endpoint is synchronous and currently intended for short-lived tasks. Durable queue dispatch, async task status, retries/reconciliation and Feishu-to-Worker integration remain separate milestones.
