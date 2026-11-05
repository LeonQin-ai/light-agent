# Light Agent

SuperAgent Bot + On-Demand Sleep/Wake + Firecracker MicroVM.

A hybrid Agent Operating Runtime built on Cognitive OS. Agent identity and memory persist; runtimes are disposable. Host controls and authorizes; Guest executes untrusted code.

## Components
- Go Control Plane (Host): Feishu ingress, Agent Registry, Task API, durable inbox and lifecycle.
- C11 Cognitive OS: SuperAgent reasoning, Memory, Context, Policy and DAG orchestration.
- Rust cos-sandboxd (Host): Firecracker/Jailer, cgroups, TAP, vsock and VM cleanup.
- Firecracker MicroVM (Guest): Worker Agent and all untrusted command/build/test/script/MCP/plugin execution.
- Rust cos-execd (Guest): structured execution with cgroups, namespaces, seccomp and process-tree cancellation.
- Host File Broker: least-privilege file access over per-VM vsock; read-only source plus private overlay and validated diff commit.

## Non-negotiable invariants
1. No generic Host exec, shell, Python or command API.
2. Every untrusted executable runs inside a Worker MicroVM Guest.
3. If Guest execution is unavailable, execution fails closed. Never fall back to Host.
4. Guest cannot access Host rootfs, Docker socket, Firecracker API socket or /dev/kvm.
5. Host files are exposed only through authorized broker capabilities; source is read-only by default.
6. Identity, Memory, Session, Task Journal, Workspace metadata and policy versions persist independently of processes and VM snapshots.

## Roadmap
M0 engineering baseline; M1 Go Control Plane + durable Feishu inbox; M2 C11 cold sleep/wake; M3 Firecracker manager; M4 Guest Executor; M5 File Broker; M6 multi-agent; M7 snapshot/warm-pool optimization.
The initial Mock Coordinator is non-executing: it does not call an LLM or run commands.
Requirements: Go 1.23+. Copy .env.example, then run make test, make build, make run. Default listen address: 127.0.0.1:8080.
Feishu webhook is disabled until verification token, encryption key and sender Open ID allowlist are configured. Initial support is direct messages only.
