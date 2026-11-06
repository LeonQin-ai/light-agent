# Durable trusted execution

The Control Plane admits operator-approved structured commands through
`POST /v1/agents/{agent_id}/executions`. It persists a queued task before returning
202. The dispatcher checks current Host capabilities when it claims the task,
creates a Docker Worker, persists the Worker association before starting it,
executes the command, and destroys the Worker before completing the task.

```bash
curl -fsS "http://127.0.0.1:8080/v1/agents/$AGENT_ID/executions" \
  -H "Authorization: Bearer $LIGHT_AGENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id":"build-001",
    "mode":"strict",
    "cpu_millis":1000,
    "memory_bytes":1073741824,
    "pids_limit":128,
    "command":{
      "executable":"/bin/echo",
      "argv":["hello-from-durable-worker"],
      "timeout_ms":15000
    }
  }'
```

An `Idempotency-Key` header can replace `request_id`. Repeating the same normalized
payload returns the existing task. Reusing the key with another payload returns
409. The endpoint assigns the trusted-internal tier; requests cannot supply a
different trust tier, backend, image or mount. Network-enabled commands and path
escapes are rejected. Configure API authentication whenever exposing the service.

Use `GET /v1/tasks/{id}` for durable state and output, and
`POST /v1/tasks/{id}/cancel` to cancel queued or running work. Running cancellation
is observed within the dispatcher's 100ms polling cadence and destroys the Worker
using a separate cleanup deadline. Cancellation state is terminal and cannot be
overwritten by a delayed command result. Cleanup completion may follow the
cancellation response; the durable Worker association remains until cleanup succeeds.

Nonzero exit, timeout, missing exit status, provider failures and cleanup failures
persist FAILED rather than COMPLETED. The dispatcher retries recorded pending
cleanup before claiming another task. On startup it destroys recorded Workers
before accepting requests, then marks interrupted RUNNING tasks FAILED with
`execution_interrupted`. It does not replay commands with unknown side effects.
If cleanup cannot be verified, startup fails and leaves the records for recovery.

Deploy exactly one Control Plane owner per SQLite database. Distributed ownership
and leases are not implemented. There is still a creation ambiguity if a Manager
creates a container but the Create response is lost before the Control Plane can
persist its ID. The Manager retains its own container records and cleans them on
restart; transactional creation intents and periodic orphan reconciliation are
follow-up work. These semantics do not provide exactly-once side effects.

Ordinary API and Feishu messages remain separate from trusted execution envelopes.
Until the restricted Cognitive OS driver is configured, they fail with
`runtime_driver_unavailable`. They are never converted into shell commands or
reported as completed by a mock dispatcher.

## Linux validation

Run `bash scripts/validate-linux.sh` with Go, Rust and a C compiler available. It
runs Go vet, all Go tests with the race detector, Go build, Rust workspace tests,
and Rust release builds using the committed lockfile.

For real Docker validation, preload an image with `cos-execd` and run:

```bash
python3 scripts/smoke-docker.py \
  --sandboxd rust/target/release/cos-sandboxd \
  --control bin/light-agentd \
  --image 'sha256:<64-hex-image-id>' \
  --work-dir /tmp/light-agent-smoke-unique
```

The work directory must not exist. The script uses random loopback ports,
ephemeral tokens and private state paths; it stops its own services and cleans
only its own recorded containers. It tests output/idempotency, failures/timeouts,
running cancellation, actual Docker restrictions, crash recovery, capability
revocation during a real Manager outage, and recovery without restarting the
Control Plane. Firecracker boot requires a separate host exposing `/dev/kvm`.
