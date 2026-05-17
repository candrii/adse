#!/usr/bin/env bash
# Install gVisor (`runsc`) on the host and register it as a Docker runtime.
#
# This is the *advanced* mode of the sandbox: agent-spawned containers run
# under gVisor's userspace kernel, shrinking the host-kernel attack surface
# without changing the application image. The Compose control-plane services
# (sqlserver, postgres, redis, opensandbox itself) keep running on runc —
# they're trusted infra and SQL Server's syscall mix is grumpy on gVisor.
#
# Requires sudo for /usr/local/bin install + /etc/docker/daemon.json edit.
# Idempotent: re-running upgrades runsc to the latest release.

set -euo pipefail

# ─────────────── arch detection ───────────────
ARCH=$(uname -m)
case "${ARCH}" in
  x86_64|amd64)  ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "✗ unsupported arch: ${ARCH}" >&2; exit 1 ;;
esac

URL="https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}"

# ─────────────── download + verify ───────────────
TMP=$(mktemp -d)
trap 'rm -rf "${TMP}"' EXIT

echo "==> downloading runsc + containerd-shim-runsc-v1 (${ARCH})"
for f in runsc runsc.sha512 containerd-shim-runsc-v1 containerd-shim-runsc-v1.sha512; do
  curl -fsSL "${URL}/${f}" -o "${TMP}/${f}"
done

echo "==> verifying SHA-512 checksums"
( cd "${TMP}" && sha512sum -c runsc.sha512 containerd-shim-runsc-v1.sha512 )

# ─────────────── install ───────────────
chmod +x "${TMP}/runsc" "${TMP}/containerd-shim-runsc-v1"
echo "==> installing to /usr/local/bin (sudo)"
sudo mv "${TMP}/runsc" "${TMP}/containerd-shim-runsc-v1" /usr/local/bin/

# ─────────────── platform detection ───────────────
# gVisor's KVM platform is faster (~5–10% overhead vs ~20% for ptrace) but
# needs /dev/kvm, which on WSL2 requires nestedVirtualization=true in
# .wslconfig. Default to whichever the host actually supports.
PLATFORM=ptrace
if [[ -r /dev/kvm ]] && sudo -n true 2>/dev/null && sudo test -w /dev/kvm; then
  PLATFORM=kvm
fi
echo "==> gVisor platform: ${PLATFORM}"
if [[ "${PLATFORM}" == "ptrace" ]]; then
  cat <<-NOTE

	  Using the ptrace platform (~20% syscall overhead).
	  To switch to kvm (faster, ~5–10% overhead) on WSL2:
	    1. Add to %USERPROFILE%\.wslconfig:
	         [wsl2]
	         nestedVirtualization=true
	    2. Restart WSL:  wsl --shutdown
	    3. Re-run this installer.

	NOTE
fi

# ─────────────── register with Docker ───────────────
echo "==> registering runsc with Docker daemon"
sudo runsc install --runtime=runsc -- --platform="${PLATFORM}"

# Restart Docker so daemon.json reload takes effect.
if systemctl list-unit-files 2>/dev/null | grep -q '^docker\.service'; then
  echo "==> restarting docker (systemctl)"
  sudo systemctl restart docker
elif command -v service >/dev/null 2>&1 && service docker status >/dev/null 2>&1; then
  echo "==> restarting docker (service)"
  sudo service docker restart
else
  cat <<-NOTE

	⚠ Could not auto-restart Docker.
	  - On Docker Desktop:  restart it from the system tray / dashboard.
	  - On bare Docker:     sudo systemctl restart docker

	NOTE
fi

echo ""
echo "✓ runsc installed."
echo "  Verify it works:  make verify-gvisor"
echo "  Use it:           make up-secure"
