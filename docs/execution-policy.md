# Cognitive OS execution risk policy

## Principle

Select the execution boundary from the task's trust level and required capabilities. The LLM may propose a task type, but it must not grant itself a lower isolation tier. A deterministic Policy Engine validates the task's origin, tenant, artifact provenance, requested capabilities and risk labels before the Scheduler chooses a backend.

## Three primary tiers

| Tier | Trust | Backend | Examples |
|---|---|---|---|
| T1 - trusted internal | Approved code and reviewed tools | Docker/runc | Internal scripts, approved business tools, trusted Agent services |
| T2 - external/untrusted | User-submitted or third-party arbitrary code | Firecracker MicroVM | Coding Agent builds/tests, unknown repositories, untrusted MCP executables |
| T3 - constrained extension | Reviewed or sandboxed module with narrow WASI imports | WASM | Data transforms, parsers, policy functions, lightweight plugins |

Docker/runc shares the Host kernel. It is a packaging and process-isolation boundary, not a strong hostile-code boundary. Apply non-root, dropped capabilities, read-only rootfs, seccomp, AppArmor/SELinux, cgroups, PID limits and default-deny networking even for T1.

Firecracker requires KVM. It is the preferred boundary for arbitrary external code when the Host supports it.

WASM is appropriate only for code that can operate through an explicit WASI or host-function capability interface. It does not provide a general Linux environment and is not a replacement for full compilers, arbitrary shell commands or tools requiring unrestricted syscalls. Enforce fuel/instruction budget, linear-memory limit, wall-clock timeout and explicit imports/capabilities.

## No-KVM deployment policy

On a VM without nested virtualization or usable `/dev/kvm`:
- T1 may run in hardened Docker/runc containers.
- T3 may run in a WASM runtime with no ambient filesystem/network access.
- T2 must be routed to a remote KVM-capable MicroVM worker pool.
- If remote MicroVM capacity is unavailable, reject/defer the T2 task by default.
- gVisor/runsc can be an explicitly configured alternative only if the organization accepts that isolation profile. It is not an automatic substitute for a MicroVM.

For the user's 4-vCPU/8-GiB no-KVM VM, this means Docker + WASM can run locally, while strict MicroVM jobs need a separate KVM-capable host. If an operator consciously enables gVisor as an alternate T2 profile, expose that fact in task audit records.

## Optional composition

A WASM plugin may run inside a MicroVM for defense in depth:
- MicroVM isolates the Guest kernel boundary.
- WASM restricts the plugin's capabilities inside the Guest.
- Neither layer receives Host Docker/KVM sockets or unrestricted Host mounts.

Do not create a MicroVM for every WASM function by default; use composition for high-risk plugins or tenant requirements.

## Required task policy fields

Each admitted task should carry immutable, auditable fields:
- `trust_tier`: `trusted_internal | external_untrusted | constrained_extension`
- `required_boundary`: `container | microvm | wasm`
- `allowed_capabilities`: explicit filesystem/network/clock/random/secrets/tool grants
- `network_policy_id`
- `resource_profile_id`
- `artifact_digest` and provenance
- `policy_version` and decision reason

The model can suggest labels, but only the Policy Engine can finalize or elevate permissions. A task cannot request a weaker boundary than its tenant or origin policy allows.

## Scheduler decision order

1. Resolve tenant and task origin.
2. Verify artifact provenance and tool/plugin approval.
3. Derive the minimum required isolation tier from policy.
4. Validate requested capabilities against an allowlist.
5. Select an available backend that meets or exceeds the required boundary.
6. If no compliant backend exists, route to an approved remote pool or fail closed.
7. Record backend, runtime/image digest, resource limits, policy version and decision reason.
8. Revoke task capabilities and destroy ephemeral execution state after completion.

## Initial acceptance tests

- External code is never admitted to runc by default.
- A MicroVM-required task on a no-KVM host is rejected or routed remotely, never silently downgraded.
- WASM modules cannot access files or network without explicit imported capabilities.
- WASM fuel, memory and wall-clock limits terminate runaway modules.
- Task policy cannot be weakened by an Agent prompt or plugin metadata.
- All backend decisions are auditable and reproducible.
