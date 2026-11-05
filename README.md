# Light Agent

SuperAgent Bot + On-Demand Sleep/Wake + Firecracker MicroVM.

A hybrid Agent Operating Runtime built on Cognitive OS. Agent identity and memory persist; runtimes are disposable. Host controls and authorizes; Guest executes untrusted code.

## Components
- Go Control Plane (Host): Feishu ingress, Agent Registry, Task API, durable inbox and lifecycle coordination.
- C11 Cognitive OS: SuperAgent reasoning, Memory, Context, Policy and DAG orchestration.
- Rust cos-sandboxd (Host): Firecracker/Jailer lifecycle, cgroups, TAP, vsock and VM cleanup.
- Firecracker MicroVM (Guest): Worker Agent and all untrusted command, build, test, script, MCP stdio and plugin execution.
- Rust cos-execd (Guest): structured command execution with cgroups, namespaces, seccomp and process-tree cancellation.
- Host File Broker: least-privilege file access over per-VM vsock; read-only source plus private overlay and validated diff commit.

## Security invariants
1. No generic Host exec, shell, Python or command API.
2. Every untrusted executable runs inside a Worker MicroVM Guest.
3. If Guest execution is unavailable, execution fails closed. Never fall back to Host.
4. Guest cannot access Host rootfs, Docker socket, Firecracker API socket or /dev/kvm.
5. Host files are exposed only through authorized broker capabilities; source is read-only by default.
6. Agent Identity, Memory, Session, Task Journal, Workspace metadata and policy versions persist independently of processes and VM snapshots.

See [docs/cognitive-os-integration.md](docs/cognitive-os-integration.md) for the pinned C11 adapter boundary and required Host capability restrictions.\n\n## Current implementation
- M0: Go module, docs, protobuf contracts and CI.
- M1: Go Control Plane, SQLite Agent/Task/Event inbox, Feishu direct-message ingress, idempotency and non-executing Mock Coordinator.
- M2 foundation: durable Runtime records, single-flight wake reservation, generation fencing, sleep admission checks and injectable lifecycle Driver contract.
- Not yet implemented: production C11 driver, Firecracker manager, Guest Executor and Host File Broker.

The Mock Coordinator only verifies durable task dispatch. It does not call an LLM, start a process/VM or execute commands. Runtime wake/sleep endpoints fail closed until a trusted SuperAgent driver is configured.

## Quick start
Requirements: Go 1.23+. Copy .env.example, then run `make test`, `make build` and `make run`. Default listen address: `127.0.0.1:8080`.
The API is loopback-only by default. If binding to a non-loopback address, configure `LIGHT_AGENT_API_TOKEN`; send it as `Authorization: Bearer <token>`. Health and Feishu webhook routes are separate.

Create an Agent with `POST /v1/agents`, submit work with `POST /v1/agents/{id}/messages`, and poll `GET /v1/tasks/{task_id}`. Agent creation does not start a process or VM.

## Feishu
The webhook is disabled unless verification token, encryption key and sender Open ID allowlist are configured. Initial support is direct messages only; group messages are rejected. Signatures are verified and events/tasks are committed atomically before ACK.
