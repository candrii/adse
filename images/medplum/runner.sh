#!/usr/bin/env bash
# Medplum sandbox runner.
#
# Same lifecycle contract as the eShop runner: structured stages, stable
# exit codes, result.json + JUnit artifacts. See eshop/runner.sh for the
# exit-code table.

set -uo pipefail

readonly RESULTS_DIR=/results
readonly REPO=/workspace/repo
readonly RUN_ID=${RUN_ID:-$(date +%s)-$$}
readonly PG_HOST=${PG_HOST:-postgres}
readonly REDIS_HOST=${REDIS_HOST:-redis}
readonly DB_NAME="medplum_${RUN_ID//[^a-zA-Z0-9]/_}"

mkdir -p "${RESULTS_DIR}"

RESULT_JSON="${RESULTS_DIR}/result.json"
START_EPOCH=$(date +%s)

cat > "${RESULT_JSON}" <<EOF
{
  "run_id":  "${RUN_ID}",
  "project": "medplum",
  "status":  "running",
  "exit_code": null,
  "stages":  {},
  "artifacts": []
}
EOF

stage() {
  local name=$1 fail_code=$2; shift 2
  local log="${RESULTS_DIR}/${name}.log"
  local started ended ok
  started=$(date +%s)
  echo "=== ${name} ===" | tee -a "${RESULTS_DIR}/run.log"
  if "$@" >"${log}" 2>&1; then ok=true; else ok=false; fi
  ended=$(date +%s)
  jq --arg n "${name}" --arg ok "${ok}" --arg log "${log}" \
     --argjson dur "$((ended - started))" \
     '.stages[$n] = {ok: ($ok=="true"), duration_s: $dur, log: $log}' \
     "${RESULT_JSON}" > "${RESULT_JSON}.tmp" && mv "${RESULT_JSON}.tmp" "${RESULT_JSON}"
  if [[ "${ok}" != "true" && -z "${STAGE_NONFATAL:-}" ]]; then
    finalize "${fail_code}" "${name}_fail"
    exit "${fail_code}"
  fi
}

finalize() {
  local code=$1 status=$2
  local ended; ended=$(date +%s)
  jq --argjson c "${code}" --arg s "${status}" \
     --argjson dur "$((ended - START_EPOCH))" \
     '.exit_code = $c | .status = $s | .duration_s = $dur' \
     "${RESULT_JSON}" > "${RESULT_JSON}.tmp" && mv "${RESULT_JSON}.tmp" "${RESULT_JSON}"
}

cd "${REPO}" || { finalize 80 checkout_fail; exit 80; }

if [[ -n "${GIT_REF:-}" ]]; then
  stage checkout 80 git fetch --depth=1 origin "${GIT_REF}"
  stage checkout 80 git checkout FETCH_HEAD
fi

if [[ -f /workspace/patch.diff ]]; then
  stage patch 70 git apply --whitespace=fix /workspace/patch.diff
fi

# Re-install in case the lockfile changed; --prefer-offline hits the warm
# store first so this is fast if deps are unchanged.
stage install 10 pnpm install --frozen-lockfile --prefer-offline

stage build 10 pnpm --filter '@medplum/server' build

# Per-task DB so concurrent agent runs don't clobber each other.
export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}"
stage db_provision 30 psql -h "${PG_HOST}" -U medplum -d postgres \
    -c "CREATE DATABASE \"${DB_NAME}\";"

# Point Medplum config at our shared sidecars.
cat > /workspace/medplum.config.json <<EOF
{
  "port": 8103,
  "baseUrl": "http://localhost:8103/",
  "database": {
    "host":     "${PG_HOST}",
    "port":     5432,
    "dbname":   "${DB_NAME}",
    "username": "medplum",
    "password": "${POSTGRES_PASSWORD}"
  },
  "redis": {
    "host": "${REDIS_HOST}",
    "port": 6379
  },
  "logLevel": "info"
}
EOF

stage migrate 30 pnpm --filter '@medplum/server' run migrate \
    -- --config /workspace/medplum.config.json

stage server_start 40 bash -c '
  cd packages/server && \
  nohup pnpm start --config /workspace/medplum.config.json \
        >/results/server.log 2>&1 &
  echo $! > /tmp/server.pid
  for i in $(seq 1 30); do
    curl -fsS http://localhost:8103/healthcheck >/dev/null && exit 0
    sleep 2
  done
  exit 40
'

# Run server tests with JUnit reporter. Medplum uses vitest; jest-junit-
# style output via vitest reporter.
stage test 20 pnpm --filter '@medplum/server' test \
    -- --reporter=junit --outputFile="${RESULTS_DIR}/results.junit.xml"

kill "$(cat /tmp/server.pid 2>/dev/null)" 2>/dev/null || true

# Clean up per-task DB to keep the shared postgres tidy.
psql -h "${PG_HOST}" -U medplum -d postgres \
     -c "DROP DATABASE IF EXISTS \"${DB_NAME}\";" || true

ARTIFACTS=$(find "${RESULTS_DIR}" -maxdepth 2 -type f \( -name '*.xml' -o -name '*.log' -o -name 'result.json' \) | jq -R -s 'split("\n")[:-1]')
jq --argjson a "${ARTIFACTS}" '.artifacts = $a' "${RESULT_JSON}" > "${RESULT_JSON}.tmp" \
  && mv "${RESULT_JSON}.tmp" "${RESULT_JSON}"

finalize 0 pass
exit 0
