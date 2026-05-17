#!/usr/bin/env bash
# Confirm that gVisor is installed AND that containers launched with the
# runsc runtime actually run under the userspace kernel.
#
# Two distinct checks:
#   1. Docker daemon knows about the runtime (`docker info` lists it).
#   2. A container launched with `--runtime=runsc` sees gVisor's
#      synthesized /proc/version, not the host kernel's.

set -euo pipefail

# ─── 1. Docker daemon knows about runsc ───
echo "==> checking Docker registered runtimes"
if ! docker info --format '{{json .Runtimes}}' | grep -q '"runsc"'; then
  echo "✗ runsc not registered with Docker."
  echo "  Run: make install-gvisor"
  exit 1
fi
echo "✓ runsc registered"

# ─── 2. Container under runsc sees gVisor kernel ───
echo "==> launching a probe container under runsc"
PROBE_OUT=$(docker run --rm --runtime=runsc alpine:3.20 \
              sh -c 'uname -a && cat /proc/version' 2>&1) || {
  echo "✗ probe container failed to start under runsc"
  echo "  Output:"; echo "${PROBE_OUT}" | sed 's/^/    /'
  echo ""
  echo "  Common causes:"
  echo "  - Docker daemon wasn't restarted after install (restart Docker Desktop / sudo systemctl restart docker)"
  echo "  - WSL2 without nested-virt while PLATFORM=kvm was forced (re-run install with PLATFORM=ptrace)"
  exit 1
}

echo "  uname -a:        $(echo "${PROBE_OUT}" | head -1)"
echo "  /proc/version:   $(echo "${PROBE_OUT}" | tail -1)"

if echo "${PROBE_OUT}" | grep -qi 'gvisor'; then
  echo "✓ gVisor kernel is intercepting syscalls"
else
  echo "⚠ probe ran but /proc/version doesn't say gVisor."
  echo "  Runtime may be misconfigured. Inspect /etc/docker/daemon.json."
  exit 1
fi

echo ""
echo "✓ gVisor verified. To use it for sandbox containers:"
echo "    make up-secure       # spins up the stack with sandbox.gvisor.toml"
