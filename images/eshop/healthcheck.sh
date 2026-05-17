#!/bin/sh
# Container-level liveness. The actual app health is probed by runner.sh
# during the `app_start` stage; this just confirms the sandbox is reachable
# and the workspace mount is present.
[ -d /workspace/repo ] && [ -d /results ] && exit 0
exit 1
