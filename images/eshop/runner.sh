#!/usr/bin/env bash
# eShopOnWeb sandbox runner — subcommand CLI.
#
# Invoked by the harness via OpenSandbox's exec API. Each subcommand emits a
# structured /results/result.json with per-stage timing and exit codes so
# the harness can route on outcomes without parsing logs.
#
# Subcommands:
#   build  → checkout (if GIT_REF) → patch (if /workspace/patch.diff) → restore → build
#   test   → ensure-built (restore --no-cache + build) → migrate → app_start → test
#   all    → both, in order, in one sandbox
#
# `test` re-runs restore+build because each Temporal activity gets a fresh
# sandbox; build artifacts don't persist across activities by default. NuGet
# packages are baked into the image so restore is incremental + fast.
#
# Exit codes (stable contract with the harness):
#   0  pass        10 build_fail     20 test_fail
#   30 migrate     40 health_fail    50 timeout
#   60 infra       70 patch_fail     80 checkout_fail

set -uo pipefail

readonly RESULTS_DIR=/results
readonly REPO=/workspace/repo
readonly RUN_ID=${RUN_ID:-$(date +%s)-$$}
readonly DB_NAME="eshop_${RUN_ID//[^a-zA-Z0-9]/_}"
readonly SQL_HOST=${SQL_HOST:-sqlserver}

readonly STAGE=${1:-all}

mkdir -p "${RESULTS_DIR}"

# ───────────────────────── source override (substrate memory) ─────────────────────────
#
# If the CLI bind-mounted host source at /workspace/src, replicate it onto
# /workspace/repo before any stage runs. Lets the agent edit on the host
# between iterations without rebuilding the image.
#
# /dev/null is the safe default (CLI omits the source flag) — a regular
# file, not a directory, so the test below is false and we fall back to
# the image's baked /workspace/repo.

if [[ -d /workspace/src && -n "$(ls -A /workspace/src 2>/dev/null)" ]]; then
  echo "syncing /workspace/src → /workspace/repo ($(find /workspace/src -type f 2>/dev/null | wc -l) files)"
  rm -rf "${REPO}"
  cp -a /workspace/src "${REPO}"
fi

# ───────────────────────── result.json scaffold ─────────────────────────
RESULT_JSON="${RESULTS_DIR}/result.json"
START_EPOCH=$(date +%s)

cat > "${RESULT_JSON}" <<EOF
{
  "run_id":  "${RUN_ID}",
  "task_id": "${TASK_ID:-}",
  "project": "eshop",
  "stage":   "${STAGE}",
  "status":  "running",
  "exit_code": null,
  "memory_dir": $( [[ -d /memory ]] && echo '"/memory"' || echo 'null' ),
  "source_override": $( [[ -d /workspace/src && -n "$(ls -A /workspace/src 2>/dev/null)" ]] && echo true || echo false ),
  "stages":  {},
  "artifacts": []
}
EOF

# stage <name> <exit_code_on_fail> -- <cmd>...
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
    # Show the failure tail to make remote debugging less painful
    echo "── last 40 lines of ${log} ──" >&2
    tail -n 40 "${log}" >&2 || true
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

# ───────────────────────── stage functions ─────────────────────────

do_checkout_and_patch() {
  if [[ -n "${GIT_REF:-}" ]]; then
    stage checkout 80 git fetch --depth=1 origin "${GIT_REF}"
    stage checkout 80 git checkout FETCH_HEAD
  fi
  if [[ -f /workspace/patch.diff ]]; then
    stage patch 70 git apply --whitespace=fix /workspace/patch.diff
  fi
}

do_build() {
  stage restore 10 dotnet restore eShopOnWeb.sln
  stage build   10 dotnet build   eShopOnWeb.sln --no-restore -c Release
}

do_migrate() {
  : "${MSSQL_SA_PASSWORD:?MSSQL_SA_PASSWORD required for migrate}"
  export ConnectionStrings__CatalogConnection="Server=${SQL_HOST};Database=${DB_NAME}-Catalog;User=sa;Password=${MSSQL_SA_PASSWORD};TrustServerCertificate=true"
  export ConnectionStrings__IdentityConnection="Server=${SQL_HOST};Database=${DB_NAME}-Identity;User=sa;Password=${MSSQL_SA_PASSWORD};TrustServerCertificate=true"

  # eShop has two DbContexts (CatalogContext + AppIdentityDbContext);
  # dotnet ef refuses to pick implicitly. Migrate each explicitly.
  # Idempotent: on a warm baseline where migrations have been applied
  # already, these are sub-second no-ops.
  stage migrate_catalog 30 dotnet ef database update \
      --project src/Infrastructure --startup-project src/Web \
      --context CatalogContext

  stage migrate_identity 30 dotnet ef database update \
      --project src/Infrastructure --startup-project src/Web \
      --context AppIdentityDbContext
}

do_test() {
  do_migrate

  # eShop's Program.cs registers two MapHealthChecks: /home_page_health_check
  # and /api_health_check. We probe both. Up to 120s for Blazor + EF
  # initialization at first start. Surface the app log on any failure so
  # the test caller can see *why* it didn't come up.
  stage app_start 40 bash -c '
    cd src/Web
    nohup dotnet run --no-build -c Release --urls http://0.0.0.0:5000 \
          >/results/app.log 2>&1 &
    pid=$!
    echo $pid > /tmp/app.pid
    echo "started dotnet run pid=$pid"
    for i in $(seq 1 60); do
      if curl -fsS http://localhost:5000/home_page_health_check >/dev/null 2>&1 ||
         curl -fsS http://localhost:5000/api_health_check       >/dev/null 2>&1; then
        echo "health probe ok after ${i}*2s"
        exit 0
      fi
      if ! kill -0 $pid 2>/dev/null; then
        echo "dotnet process died early; tail of /results/app.log:"
        tail -n 60 /results/app.log 2>/dev/null
        exit 40
      fi
      sleep 2
    done
    echo "health probe timed out after 120s; tail of /results/app.log:"
    tail -n 60 /results/app.log 2>/dev/null
    exit 40
  '

  # Use the built-in trx logger only (junit logger needs JUnitXml.TestLogger
  # in each test csproj — we don't modify eShop). TRX is structured XML and
  # easy to convert downstream.
  stage test 20 dotnet test eShopOnWeb.sln --no-build -c Release \
      --logger "trx;LogFileName=results.trx" \
      --results-directory "${RESULTS_DIR}"

  kill "$(cat /tmp/app.pid 2>/dev/null)" 2>/dev/null || true
}

record_artifacts() {
  local artifacts
  artifacts=$(find "${RESULTS_DIR}" -maxdepth 2 -type f \
              \( -name '*.xml' -o -name '*.trx' -o -name '*.log' -o -name 'result.json' \) \
              | jq -R -s 'split("\n")[:-1]')
  jq --argjson a "${artifacts}" '.artifacts = $a' "${RESULT_JSON}" \
     > "${RESULT_JSON}.tmp" && mv "${RESULT_JSON}.tmp" "${RESULT_JSON}"
}

# ───────────────────────── dispatch ─────────────────────────
cd "${REPO}" || { finalize 80 checkout_fail; exit 80; }

case "${STAGE}" in
  build)
    do_checkout_and_patch
    do_build
    record_artifacts
    finalize 0 pass
    ;;
  warmup)
    # Used by `sandbox warmup eshop` — does the slow one-time prep so the
    # caller can `docker commit` the running containers as :warm. Skips
    # app/start and tests; those run per-task.
    do_checkout_and_patch
    do_build
    do_migrate
    record_artifacts
    finalize 0 pass
    ;;
  test)
    do_checkout_and_patch
    # On cold start this rebuilds; on warm baseline it's incremental
    # (artifacts already in /workspace from the committed image).
    do_build
    do_test
    record_artifacts
    finalize 0 pass
    ;;
  all)
    do_checkout_and_patch
    do_build
    do_test
    record_artifacts
    finalize 0 pass
    ;;
  *)
    echo "unknown stage: ${STAGE} (expected: build | warmup | test | all)" >&2
    finalize 60 infra_fail
    exit 60
    ;;
esac
exit 0
