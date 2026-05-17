#!/bin/sh
# Simple liveness check; tinyproxy returns 400 on bare GET / which is fine.
wget -q -O- --tries=1 --timeout=2 http://localhost:8888 >/dev/null 2>&1 || \
  exit 1
echo "ok"
