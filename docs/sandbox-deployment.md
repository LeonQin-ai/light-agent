# Firecracker sandbox deployment (Linux/KVM)

This repository now contains the first runnable Rust control path for Firecracker and the Guest vsock executor. Hardware/KVM validation is still required on the target host before production use.

## Trust domains

- Go light-agentd: identity, durable queue, Feishu/API ingress and lifecycle coordination.
- Rust cos-sandboxd: privileged Host service. It is the only component allowed to launch Jailer/Firecracker and access configured workspace roots.
- Firecracker/Jailer: one disposable microVM per Agent Runtime generation.
- Guest cos-execd: AF_VSOCK port 5000; accepts structured JSON requests, never shell command strings from the Host API.
- Cognitive OS: starts inside the Guest as a systemd service, binds only 127.0.0.1:8080 and requires a Guest-local bearer token.
- Workspace broker: syncs a bounded, symlink-free workspace snapshot into the Guest; commit exports changed files and applies optimistic SHA-256 conflict checks through openat2 and atomic rename. Source files are not mounted into the Guest.

## Host prerequisites

1. Linux with KVM enabled, /dev/kvm, and a kernel that supports openat2 and cgroup v2.
2. Install Firecracker and Jailer from the same verified release. The Jailer binary and Firecracker binary must be root-owned and not writable by the service user.
3. Build a Firecracker-compatible kernel with virtio-vsock, virtio-blk, serial console, ext4 and cgroup v2 support.
4. Create a raw ext4 Guest rootfs and install Cognitive OS plus cos-execd using guest-image/install.sh.
5. Configure FIRECRACKER_KERNEL, FIRECRACKER_ROOTFS, JAILER_BIN, FIRECRACKER_BIN, JAILER_CHROOT_BASE, and a 32+ character LIGHT_AGENT_SANDBOX_API_TOKEN.
6. Keep the sandbox API on loopback or place it behind mutually authenticated service networking. Do not expose it directly to the Internet.

## Build and run

Build Rust services:

    cargo test --manifest-path rust/Cargo.toml --workspace
    cargo build --manifest-path rust/Cargo.toml --workspace --release

Install the binaries:

    install -m 0755 rust/target/release/cos-sandboxd /usr/local/bin/cos-sandboxd
    install -m 0755 rust/target/release/cos-execd /usr/local/bin/cos-execd

Create the Guest image. Mount an ext4 raw rootfs at /mnt/guest-root, then run:

    ROOTFS_MOUNT=/mnt/guest-root CAGENT_BIN=/path/to/cognitive-os-agent COSEXECD_BIN=/usr/local/bin/cos-execd ./guest-image/install.sh

Replace the placeholder values in /etc/light-agent/guest.env inside the image. COA_AUTH_KEY and COA_GUEST_API_TOKEN must be the same high-entropy secret. Configure the actual LLM provider/API key in the Guest environment or its private config file. Never put credentials in the Guest image or source repository; inject them through a protected image provisioning step.

Start cos-sandboxd with the variables in .env.sandbox.example. The API requires a bearer token. The Guest boot path is fail-closed: if /dev/kvm, the Jailer, Firecracker API socket, vsock Guest Agent or Guest health check is unavailable, VM start fails.

## Workspace protocol

- The Host walks only regular files; symlinks and special files are rejected.
- Initial sync is capped at 5,000 files, 4 MiB per file and 32 MiB total.
- The Guest receives files under /workspace using openat2 with BENEATH/NO_SYMLINKS and atomic rename.
- The Guest returns changed files and deletions with the original SHA-256 manifest.
- The Host commits only if the current source hash still matches the manifest. Conflicts are reported; they are never silently overwritten.
- This first implementation intentionally limits each changed file to 4 MiB and the total diff to 32 MiB.

## Important implementation boundaries

- No Host command execution API exists. The only Host process manager launches the trusted Jailer/Firecracker binaries.
- Guest commands require an absolute executable path and argv array. The Guest executor applies process groups, timeout, bounded output, rlimits, mount/IPC/UTS namespaces, a default-deny network namespace, seccomp deny rules and optional cgroup v2 limits.
- The Guest Cognitive OS service is itself inside the Firecracker VM. It is not launched as a Host process.
- The current Rust service is an MVP lifecycle manager, not a substitute for a production security review. In particular, validate Jailer device-node setup, cgroup delegation, kernel compatibility, guest image provenance, VM process recovery, filesystem race handling and escape resistance on the actual target kernel.
- Network egress is not configured by default. Add an explicit TAP/bridge and egress policy before enabling model API access from a Guest.
