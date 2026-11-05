# Implementation roadmap

| Milestone | Scope | Exit criteria |
|---|---|---|
| M0 | Repo baseline, dependency pin, models, lifecycle/RPC contracts, CI | Reproducible build/test and versioned contracts |
| M1 | Feishu ingress, durable inbox, Agent Registry, Task API, audit, Mock Coordinator | Duplicate events/tasks idempotent; ACK after durable commit |
| M2 | C11 SuperAgent cold sleep/wake | Memory/Session/Journal restore, single-flight, generation fencing |
| M3 | Rust cos-sandboxd + Firecracker/Jailer | Per-VM cgroup/TAP/vsock/volume, Guest ready, cleanup |
| M4 | Rust Guest cos-execd | cgroup v2, namespaces, seccomp, limits, cancellation, fail-closed |
| M5 | Host File Broker | vsock, read-only source, private overlay, path defense, diff commit |
| M6 | Multi-Agent DAG | concurrency/depth/budget limits, cancellation, conflict handling |
| M7 | Snapshot and warm-pool | compatibility, cold-boot fallback, tenant-clean pool, P50/P95 |

Before production validate on real Linux/KVM: Firecracker/Jailer and Guest kernel compatibility, cgroup v2, seccomp, vsock reconnect/cancel, File Broker traversal/symlink/race defense, Guest escape and resource exhaustion.
