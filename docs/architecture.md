# Architecture baseline

Feishu Bot/API -> Go Gateway + Control Plane (Host, always on) -> persistent Agent Identity/Memory/Session/Task Journal -> SuperAgent Coordinator (Host, on demand; C11 Cognitive OS) -> Host Capability Broker and Agent Manager -> Rust cos-sandboxd -> Firecracker + Jailer -> Worker MicroVM -> C11 Cognitive OS Worker -> Rust cos-execd -> Shell/Build/Test/MCP/Plugin.

## Language and trust boundaries
| Component | Language | Trust domain | Responsibility |
|---|---|---|---|
| Gateway / Control Plane | Go | Host trusted | HTTP, Feishu ingress, identity, queue, lifecycle, quotas |
| SuperAgent / Worker | C11 | Host coordinator / Guest worker | Cognitive OS reasoning, Memory, Context, Policy and DAG |
| Sandbox manager | Rust | Host trusted infrastructure | Firecracker/Jailer, VM lifecycle and cleanup |
| Guest executor | Rust | Guest | Structured process execution and per-task isolation |
| File Broker | Rust | Host trusted infrastructure | Authorized file operations over vsock |

The Host Coordinator is a restricted control-plane client. It must not receive generic process execution capabilities. The current Cognitive OS runtime includes tools and plugin execution; do not expose it as a Host SuperAgent until its Host capability set is restricted and audited.

## Identity and runtime are separate
Agent is a persistent logical identity, not a process or VM. Persist tenant/user binding, capability profile, Memory and Session IDs, Task Journal, idempotency keys, DAG state, Workspace metadata, volume IDs, artifact references, policy version, runtime image digest and generation. Runtime process and Worker VMs are disposable.

SQLite `runtime_instances` stores runtime ID, generation, state, endpoint and last error separately from Agent identity. `BeginWake` reserves one generation transactionally; concurrent wake requests reuse the existing STARTING/READY runtime. A stale generation cannot transition to READY. Sleep admission is rejected while queued or running tasks exist.

## Lifecycle
Agent states: CREATED, IDLE, WAKING, ACTIVE, SLEEP_PENDING, SLEEPING, STOPPING, STOPPED, FAILED and DESTROYED.
Runtime states: ABSENT, STARTING, READY, BUSY, STOPPING, STOPPED and FAILED.
Wakeup is single-flight. Use monotonically increasing generation fencing. Runtime drivers must reconcile process/VM state after timeout or restart before retrying. Do not infer that a process is dead merely because a control-plane request timed out.

## Cold sleep
M2 uses cold sleep, not VM snapshots. Quiesce only when no running task/Worker, tool/MCP callback, uncommitted transaction or artifact upload remains; persist Memory, Session, Journal, Workspace and final response first. Initial Coordinator idle timeout target is five minutes. Gateway and Control Plane stay resident. Snapshot and warm pools are deferred to M7.

## Workspace
Host-authorized source is read-only. Each Worker gets a private overlay and produces a diff/artifact. Host validates conflicts and policy before commit. Firecracker has no native virtio-fs device: use a Rust Host File Broker over per-VM vsock. Never expose Host rootfs or management sockets to Guest.
