# Linux validation — 2026-11-06

Validated the working changes on top of main `d742daa` over SSH in an isolated
directory. No production service was stopped or reconfigured.

## Environment

- OpenCloudOS 9.4, x86_64, Linux 6.6.117.
- Go 1.23.12, Rust/Cargo 1.99.1, GCC.
- Docker 29.7.2, runc, cgroup v2 with the systemd driver.
- `/dev/kvm` is absent; Firecracker boot was not tested.
- Remote workspace: `/root/light-agent-validation/20261106-c1047518`.

## Results

| Check | Result |
|---|---|
| `go vet ./...` | Passed |
| `go test -race -p 2 ./... -timeout 120s` | Passed, including dispatcher cleanup retry and concurrent capability registry tests |
| Native Linux Go binary build | Passed |
| `cargo test --locked --manifest-path rust/Cargo.toml --workspace -j 2` | Passed, 12 tests |
| Rust workspace release build | Passed; two existing `unused_mut` warnings in `workspace.rs` |
| Real Docker end-to-end smoke | Passed, all seven groups below |
| Default deployment Dockerfile image build | Blocked fetching Docker Hub base-image metadata; mirror retry also encountered slow base-image downloads |

The real Docker smoke used the native-built `cos-execd` in a temporary image based
on the host's preloaded `swe-agent:py3.11`. The Manager received its immutable image
ID and enforced the normal Worker security profile. This validates the actual
execution path, while the complete deployment Dockerfile build remains a separate
network-limited check.

### Docker smoke coverage

1. Authenticated Host capabilities and persistent observation version.
2. Durable command submission, output, idempotency and capability audit version.
3. Nonzero exit and timeout persisted as failures.
4. Actual non-root, read-only, no-network, runc, capability/resource restrictions;
   running cancellation destroyed its Worker.
5. Control Plane SIGKILL followed by restart destroyed the recorded Worker,
   failed interrupted work without replay, and advanced the observation version.
6. External-untrusted execution and unavailable reasoning driver failed closed.
7. A real Sandbox Manager outage revoked readiness on refresh; restarting it
   restored execution without restarting the Control Plane.

The final smoke exited successfully and left no pending task/Worker associations
or container records. Its service processes were stopped by the harness.

## Issue found and fixed on the real host

Docker's `local` log driver rejects its default compression setting together with
`max-file=1` when starting a container. The provider now sets `compress=false`
explicitly while retaining the 10MiB single-file log bound. A Rust regression
assertion and the real start/execute/cleanup smoke cover this configuration.

The Go Manager client now includes a bounded structured error description on
non-success responses so such backend failures can be diagnosed from task state.

## Artifacts and reproduction

Local logs are retained in `.validation/20261106-c1047518/` (Git-ignored). Remote
logs are under the workspace's `logs/` directory. The successful smoke artifacts
are in `smoke-final-1793968305-4024824/`.

Run `bash scripts/validate-linux.sh` for native checks and follow
[durable-execution.md](durable-execution.md) to reproduce the Docker smoke.

## Remaining integration boundaries

The restricted Cognitive OS driver and real Feishu reasoning-to-Worker path are
still pending; messages fail with `runtime_driver_unavailable`. Distributed
database ownership, creation-intent reconciliation for a lost Create response,
remote Worker routing, gVisor/WASM providers and KVM boot validation remain outside
this completed validation batch.
