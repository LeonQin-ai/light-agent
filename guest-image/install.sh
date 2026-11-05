#!/usr/bin/env bash
set -euo pipefail

ROOTFS_MOUNT=${ROOTFS_MOUNT:?set ROOTFS_MOUNT to the mounted ext4 guest rootfs}
CAGENT_BIN=${CAGENT_BIN:?set CAGENT_BIN to the built cognitive-os-agent binary}
COSEXECD_BIN=${COSEXECD_BIN:?set COSEXECD_BIN to the built cos-execd binary}
test -x "$CAGENT_BIN"
test -x "$COSEXECD_BIN"
test -d "$ROOTFS_MOUNT/etc"

install -D -m 0755 "$CAGENT_BIN" "$ROOTFS_MOUNT/usr/local/bin/cognitive-os-agent"
install -D -m 0755 "$COSEXECD_BIN" "$ROOTFS_MOUNT/usr/local/bin/cos-execd"
install -D -m 0644 guest-image/systemd/cognitive-os-agent.service "$ROOTFS_MOUNT/etc/systemd/system/cognitive-os-agent.service"
install -D -m 0644 guest-image/systemd/cos-execd.service "$ROOTFS_MOUNT/etc/systemd/system/cos-execd.service"

mkdir -p "$ROOTFS_MOUNT/etc/light-agent" "$ROOTFS_MOUNT/var/lib/light-agent" "$ROOTFS_MOUNT/workspace" "$ROOTFS_MOUNT/home/agent"
if ! grep -q '^agent:' "$ROOTFS_MOUNT/etc/passwd"; then
  echo 'agent:x:10000:10000:Light Agent:/home/agent:/usr/sbin/nologin' >> "$ROOTFS_MOUNT/etc/passwd"
  echo 'agent:x:10000:' >> "$ROOTFS_MOUNT/etc/group"
fi
chown -R 10000:10000 "$ROOTFS_MOUNT/home/agent" "$ROOTFS_MOUNT/workspace" "$ROOTFS_MOUNT/var/lib/cognitive-os-agent" 2>/dev/null || true
chmod 0750 "$ROOTFS_MOUNT/workspace" "$ROOTFS_MOUNT/var/lib/light-agent"
mkdir -p "$ROOTFS_MOUNT/etc/systemd/system/multi-user.target.wants"
ln -sfn ../cognitive-os-agent.service "$ROOTFS_MOUNT/etc/systemd/system/multi-user.target.wants/cognitive-os-agent.service"
ln -sfn ../cos-execd.service "$ROOTFS_MOUNT/etc/systemd/system/multi-user.target.wants/cos-execd.service"

if [[ ! -f "$ROOTFS_MOUNT/etc/light-agent/guest.env" ]]; then
  install -m 0600 /dev/null "$ROOTFS_MOUNT/etc/light-agent/guest.env"
  cat > "$ROOTFS_MOUNT/etc/light-agent/guest.env" <<'EOF'
# Replace before boot. COA_AUTH_KEY and COA_GUEST_API_TOKEN must match.
COA_AUTH_KEY=REPLACE_WITH_RANDOM_32_BYTE_TOKEN
COA_GUEST_API_TOKEN=REPLACE_WITH_RANDOM_32_BYTE_TOKEN
COA_HTTP_BIND=127.0.0.1
COA_HTTP_PORT=8080
COA_LLM_PROVIDER=mock
COA_LLM_MODEL=mock
COSEXECD_UID=10000
COSEXECD_GID=10000
EOF
fi
echo "Guest binaries and systemd units installed. Configure guest.env and rebuild the raw ext4 image."
