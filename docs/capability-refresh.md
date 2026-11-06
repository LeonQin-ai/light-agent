# Live Host capability registry

When `LIGHT_AGENT_SANDBOX_MANAGER_URL` is configured, the Control Plane fetches
authenticated Host capabilities at startup and every 30 seconds. Each probe has
a 3-second context deadline (the HTTP client also has a 2-second timeout).

Both `GET /v1/runtime/capabilities` and `POST /v1/execution/run` read the current
registry snapshot. A successful refresh can restore execution after the Sandbox
Manager recovers without restarting the Control Plane. Every request uses one
consistent snapshot for its admission decision.

A failed refresh or a non-Host response immediately replaces the snapshot with
`observation_scope: unavailable`, with no enabled execution capabilities.
Snapshots also expire 90 seconds after receipt if refreshing stops. Policy
rejects these snapshots before any Worker is created. Existing executions are
not cancelled by a capability refresh; their provider lifecycle still owns
execution and cleanup. Successful and failed observations are journaled in SQLite
with monotonic `registry_version` values; the latest 1000 observations are retained.
The scheduler includes the observation version in its execution result for audit.
Journal failures disable execution. Readiness starts unavailable after a restart:
historical observations do not authorize new work until a fresh probe succeeds.

Without a Sandbox Manager URL, the API retains the existing process-local probe
for diagnostics. A process/container-scoped snapshot cannot authorize execution.

Validation covers expiration, refresh failures, non-Host responses, recovery,
cancelled probes, concurrent readers/refreshers, loop shutdown and HTTP admission
across an unavailable/healthy/failed/recovered sequence using a mock Manager.
Real Linux and Docker validation results are recorded in
[linux-validation-2026-11-06.md](linux-validation-2026-11-06.md). Firecracker boot
still requires a host exposing `/dev/kvm`.
