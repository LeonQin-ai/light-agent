# Security boundaries

- No host.exec, host.shell, host.python, system(), popen() or generic Host process API is exposed to an Agent.
- Shell, scripts, compilers, tests, Git hooks, MCP stdio and plugins execute only inside Worker Guest.
- Missing/unhealthy Guest Executor means fail closed. Never fall back to Host.
- Worker creation is mediated by Agent Manager admission control.
- Guest cannot mount Host rootfs or access /dev/kvm, Docker socket, Firecracker API socket or Host management UDS.

## Host File Broker
Bind each capability to tenant, Agent, Worker and immutable authorized root ID. Resolve paths relative to pre-opened dirfd; prefer openat2 RESOLVE_BENEATH and RESOLVE_NO_MAGICLINKS. Reject traversal, magic links, unauthorized symlinks and device nodes. Source is read-only; mutations go to private overlay. Enforce file-size/depth/concurrency/byte/rate limits. Audit operation metadata, never file contents or secrets.

## Guest Executor
Use structured executable/argv/cwd/timeout/resource fields, never a Host shell string. In Guest validate inputs; create task cgroup v2; set CPU/memory/PID/IO limits; apply namespaces; drop UID/GID and capabilities; set NoNewPrivileges and tested seccomp; execve/execveat; bound output; cancel entire task cgroup. Host cgroups separately constrain each Firecracker VMM.

## Feishu
Verify signature and token; decrypt encrypted payloads; deduplicate event ID; persist event and task atomically before ACK. Initial implementation permits only allowlisted direct-message senders; reject group messages. Never log or persist App Secrets in normal files, Agent Memory or Guest images.

Do not promise exactly-once command execution. Query stable task/execution IDs before retry; never automatically retry non-idempotent operations.
