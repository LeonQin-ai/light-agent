# Sandbox deployment

The Rust `cos-sandboxd` currently implements the Firecracker/Jailer provider and requires Linux KVM. See [runtime-backends.md](runtime-backends.md) for the Docker and gVisor deployment design for hosts without nested virtualization.

For a 4-vCPU/8-GiB VM with no `/dev/kvm`, deploy the Go Control Plane as a hardened Docker service using `deploy/compose.yaml`, and run the trusted Sandbox Manager on the Host. The Docker Worker provider should use Docker Engine with either:
- `runc` for compatibility/trusted jobs (`docker` mode);
- gVisor `runsc` for untrusted jobs (`sandbox` mode).

Do not start the current Firecracker provider on this host and do not silently fall back from Firecracker to runc. Backend selection must be explicit and fail closed.

## Deploy the Control Plane

    cp deploy/.env.example deploy/.env
    # Edit deploy/.env and set a unique LIGHT_AGENT_API_TOKEN.
    docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build

Check health:

    curl http://127.0.0.1:8080/healthz

The compose file publishes only to loopback, runs as non-root, uses a read-only root filesystem, drops Linux capabilities and persists SQLite in a named volume. It intentionally does not mount `/var/run/docker.sock` into the Control Plane. The Agent process must never receive Docker daemon access.

## Install gVisor on the Host

Install and pin the gVisor `runsc` release using the official gVisor installation instructions for the target distribution. Register it as a Docker runtime in the Host Docker daemon, then verify:

    runsc --version
    docker info --format '{{json .Runtimes}}'

A Worker started in `sandbox` mode must explicitly request `--runtime=runsc`. If the runtime is missing, the job must fail closed. Do not transparently retry with `runc`.

## Important implementation status

The current repository contains a Firecracker provider and a hardened Control Plane Compose deployment. The Docker/runc and Docker/gVisor Worker provider, its lifecycle API and end-to-end task integration are not implemented yet. Do not treat the environment variables in `deploy/.env.example` as active configuration until the provider lands.

The Docker socket is effectively root-equivalent. Only a narrowly scoped, trusted Host Sandbox Manager may access it. Never mount the Docker socket into Cognitive OS, SuperAgent, Worker or user-controlled containers.
