# Light Agent

SuperAgent Bot + On-Demand Sleep/Wake + pluggable Docker / gVisor Sandbox / Firecracker execution backends.

A hybrid Agent Operating Runtime built on Cognitive OS. Agent identity and memory persist; runtimes are disposable. Host controls and authorizes; every Agent runtime and untrusted workload runs inside a Firecracker Guest.

## Components
- Go Control Plane (Host): Feishu ingress, Agent Registry, Task API, durable inbox and lifecycle coordination.
- C11 Cognitive OS: SuperAgent reasoning, Memory, Context, Policy and DAG orchestration; deployed inside a Firecracker Guest, never as an unrestricted Host process.
- Rust Sandbox Manager (Host): trusted execution broker; Firecracker/Jailer provider exists, Docker/runc and Docker/gVisor providers are planned.
- Docker deployment: hardened Compose profile for the Go Control Plane; Agents never receive the Docker socket.
- Firecracker MicroVM (Guest): Cognitive OS SuperAgent and Worker Agent runtime.
- Rust cos-execd (Guest): structured command execution with namespaces, seccomp, rlimits, optional cgroup v2 and process-tree cancellation.
- Host Workspace Broker: bounded snapshot transfer over vsock, read-only source semantics, private Guest workspace and SHA-256 optimistic commit.

## Security invariants
1. No generic Host exec, shell, Python or command API.
2. Every Agent runtime and untrusted executable runs inside a Worker/SuperAgent MicroVM Guest.
3. If Guest execution is unavailable, execution fails closed. Never fall back to Host.
4. Guest cannot access Host rootfs, Docker socket, Firecracker API socket or /dev/kvm.
5. Host files are exposed only through the workspace broker; source is not mounted into Guest.
6. Agent Identity, Memory, Session, Task Journal, Workspace metadata, policy versions and artifact references persist independently of processes and VM snapshots.

## Current implementation
- M0: Go module, docs, models, lifecycle/RPC contracts and CI.
- M1: Go Control Plane, SQLite Agent/Task/Event inbox, Feishu direct-message ingress, idempotency and API.
- M2a: durable Runtime records, single-flight wake reservation, generation fencing and sleep admission.
- M2b/M3 foundation: Rust cos-sandboxd, Jailer/Firecracker API, virtio-vsock, Guest health and Cognitive OS RPC.
- M4 foundation: Rust cos-execd with structured exec, timeouts, bounded output, namespaces, seccomp, rlimits and optional cgroup v2.
- M5 foundation: workspace snapshot sync and SHA-256 conflict-checked file commit.

The production Go Runtime Driver and task dispatcher wiring are the next integration step. Docker deployment files and backend design are in `deploy/` and `docs/runtime-backends.md`. Docker/runc and Docker/gVisor Worker providers are not implemented yet. Firecracker hardware validation is mandatory before production use.

## Quick start
Requirements: Go 1.23+, Rust stable. For a VM without nested virtualization, use `deploy/compose.yaml` for the Control Plane and follow `docs/runtime-backends.md`. Firecracker execution additionally requires Linux/KVM. Run make test, make rust-test, make build and make rust-build.

The Control Plane API is loopback-only by default. If binding to a non-loopback address, configure LIGHT_AGENT_API_TOKEN; send it as Authorization: Bearer <token>. Health and Feishu webhook routes are separate.

## Feishu
The webhook is disabled unless verification token, encryption key and sender Open ID allowlist are configured. Initial support is direct messages only; group messages are rejected. Signatures are verified and events/tasks are committed atomically before ACK.
