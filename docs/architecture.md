# Architecture baseline (Light Agent v0.3)

Feishu Bot/API -> Go Gateway + Control Plane (Host, always on) -> persistent Agent Identity/Memory/Session/Task Journal -> SuperAgent Coordinator (Host, on demand; C11 Cognitive OS) -> Host Capability Broker and Agent Manager -> Rust cos-sandboxd -> Firecracker + Jailer -> Worker MicroVM -> C11 Cognitive OS Worker -> Rust cos-execd -> Shell/Build/Test/MCP/Plugin.

## Language boundaries
| Component | Language | Responsibility |
|---|---|---|
| Gateway/Control Plane | Go | HTTP, Feishu ingress, identity, queue, lifecycle, quotas |
| SuperAgent/Worker | C11 | Cognitive OS reasoning, Memory, Context, Policy, DAG |
| Sandbox manager | Rust | Firecracker/Jailer lifecycle and cleanup |
| Guest executor | Rust | Structured process execution and per-task isolation |
| File Broker | Rust | Authorized file operations over vsock |

Agent identity is not a process or VM. Persist tenant/user binding, capability profile, Memory/Session, Task Journal, idempotency keys, DAG, Workspace metadata, artifact references, policy version, image digest and generation. Runtime processes and VMs are disposable.

## Lifecycle
Agent states: CREATED, IDLE, WAKING, ACTIVE, SLEEP_PENDING, SLEEPING, STOPPING, STOPPED, FAILED, DESTROYED. Runtime states are independent: ABSENT, STARTING, READY, BUSY, QUIESCING, STOPPING, STOPPED, FAILED.
Wakeup per Agent is single-flight. Persist inbound events before ACK. Use monotonic generation fencing. Reconcile runtime state before retrying a timed-out start.

## Cold sleep
M2 starts with cold sleep, not snapshots. Quiesce only when no running task/worker, tool/MCP callback, uncommitted transaction or artifact upload remains; persist Memory, Session, Journal, Workspace metadata and final response. Initial Coordinator idle timeout: five minutes. Gateway/Control Plane stay resident. Snapshot and warm pools are M7.

## Workspace
Host authorized source is read-only. Each Worker has a private overlay and emits diff/artifacts; Host validates conflicts and policy before commit. Firecracker has no native virtio-fs device: use a Rust Host File Broker over per-VM vsock. Never expose Host rootfs or management sockets to Guest.

Cognitive OS dependency is pinned in COGNITIVE_OS_REF; do not silently track a moving branch.
