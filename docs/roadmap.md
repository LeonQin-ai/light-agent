# Implementation roadmap

| Milestone | Scope | Status / exit criteria |
|---|---|---|
| M0 | Repo baseline, dependency pin, models, lifecycle/RPC contracts, CI | Implemented baseline; CI green |
| M1 | Feishu ingress, durable inbox, Agent Registry, Task API, audit, Mock Coordinator | Initial implementation; idempotency, cancellation guards and CI tests |
| M2a | Durable Runtime registry and lifecycle contract | Implemented: single-flight reservation, generation fencing, sleep admission, injectable Driver |
| M2b | Restricted C11 SuperAgent driver | Pending: isolate C11 tool capabilities, persistent state restore, task submission and graceful shutdown |
| M3 | Rust cos-sandboxd + Firecracker/Jailer | Pending: per-VM cgroup/TAP/vsock/volume, Guest ready handshake, deterministic cleanup |
| M4 | Rust Guest cos-execd | Pending: cgroup v2, namespaces, seccomp, limits, cancellation and fail-closed |
| M5 | Host File Broker | Pending: vsock protocol, read-only source, private overlay, path/symlink defense and diff commit |
| M6 | Multi-Agent DAG | Pending: concurrency/depth/budget limits, cancellation propagation and conflict handling |
| M7 | Snapshot and warm-pool optimization | Deferred: compatibility checks, cold-boot fallback, tenant-clean pool and P50/P95 measurement |

Before production validate on a real Linux/KVM host: Firecracker/Jailer and Guest kernel compatibility, Host and Guest cgroup v2, seccomp compatibility, vsock reconnect/cancellation, File Broker traversal/symlink/race defenses, Guest escape and resource exhaustion.


## Additional execution backend track (for hosts without nested virtualization)

| Milestone | Scope | Exit criteria |
|---|---|---|
| M3-D | Docker/runc Worker provider | Create/exec/cancel/destroy OCI Worker; non-root, no capabilities, read-only rootfs, cgroups and default-deny network |
| M3-S | Docker/gVisor Worker provider | Explicit `runsc` runtime selection; startup preflight; fail closed if unavailable; prove runtime in container metadata |
| M3-F | Firecracker provider | Existing KVM/Jailer path validated on KVM-capable hosts |
| M3-I | Unified execution provider API | Go scheduler selects docker/sandbox/firecracker; lifecycle reconciliation, quotas and audit consistent across providers |
| M3-E2E | Feishu-to-Worker end-to-end | Feishu task reaches Cognitive OS, runs in selected backend, returns result and cleans up without Host command execution |

The Compose file currently deploys only the Go Control Plane. It deliberately does not mount the Docker socket. A trusted Host Sandbox Manager must be the only Docker API client.
