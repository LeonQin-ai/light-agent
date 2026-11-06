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
| M3-D | Docker/runc trusted Worker provider | Create/exec/cancel/destroy OCI Worker; non-root, no capabilities, read-only rootfs, cgroups and default-deny network |
| M3-S | gVisor alternative provider | Explicit `runsc` runtime selection for approved no-KVM isolation profiles; startup preflight; fail closed if unavailable; never silently downgrade a MicroVM-required task |
| M3-F | Firecracker MicroVM provider | External/untrusted code execution; existing KVM/Jailer path validated on KVM-capable hosts; no-KVM tasks route remotely or fail closed |
| M3-W | WASM plugin provider | Wasmtime/WasmEdge, explicit WASI imports, capability allowlist, fuel/memory/time limits and plugin ABI |
| M3-H | Host Capability Manager | Go Linux probe, authenticated Rust Host API and Go fetch client implemented; runtime boot/smoke preflights, periodic refresh and durable versioned registry pending |
| M3-L | Local Degraded Sandbox policy | Strict/Compatible/Availability modes; no-remote behavior; explicit gVisor alternate approval; structured no-compliant-executor response; no silent downgrade |
| M3-I | Unified execution provider API | Go scheduler selects docker/microvm/wasm/gvisor based on trust policy and node capabilities; remote worker routing, lifecycle reconciliation, quotas and audit |
| M3-E2E | Feishu-to-Worker end-to-end | Feishu task reaches Cognitive OS, runs in selected backend, returns result and cleans up without Host command execution |

See [execution-policy.md](execution-policy.md) for the three-tier trust model (Docker, MicroVM, WASM), no-KVM routing and policy-engine requirements. The Compose file currently deploys only the Go Control Plane. It deliberately does not mount the Docker socket. A trusted Host Sandbox Manager must be the only Docker API client.
