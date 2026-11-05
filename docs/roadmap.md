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
