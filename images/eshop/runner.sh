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
# Stable DB names — NOT run-id-scoped. The warm baseline holds the
# already-migrated schema in these databases; reusing the names lets the
# next run start with do_migrate as a sub-second no-op rather than running
# every EF migration against an empty database. Isolation between runs is
# guaranteed by `compose down -v` (wipes the sqlserver container) and the
# warm baseline is rehydrated by `:warm` image restart, not shared volumes.
readonly DB_CATALOG=${DB_CATALOG:-eShopOnWeb_Catalog}
readonly DB_IDENTITY=${DB_IDENTITY:-eShopOnWeb_Identity}
readonly SQL_HOST=${SQL_HOST:-sqlserver}

readonly STAGE=${1:-all}

mkdir -p "${RESULTS_DIR}"

# ───────────────────────── source override (incremental builds) ─────────────────────────
#
# If the CLI bind-mounted host source at /workspace/src, sync it onto
# /workspace/repo before any stage runs. Lets the agent edit on the host
# between iterations without rebuilding the image.
#
# Critical: this is an OVERLAY sync, not a wipe+copy. The :warm image
# carries a fully-built /workspace/repo with bin/ and obj/ trees populated
# from the previous `dotnet build`. Wiping those means the next build
# starts from zero (~18s); preserving them lets `dotnet build` do
# incremental compilation (~1–3s for small edits).
#
# We use rsync with --exclude='bin/' --exclude='obj/' --exclude='.git/' so:
#   - source files from /workspace/src overwrite their counterparts in repo
#   - files deleted from /workspace/src are also deleted from repo (--delete)
#   - bin/obj/ in repo are untouched — incremental build can reuse them
#   - .git/ is preserved from the baked /workspace/repo clone; we don't
#     stomp the canonical history with whatever local commits / hooks /
#     untracked refs the agent's working tree happens to carry. (Also: a
#     real repo's .git can be hundreds of MB; copying it every iteration
#     would dominate the sync.)
# Fallback: cp -a if rsync isn't present (image installs rsync; this is
# defensive). cp -a copies-into rather than replaces, so existing bin/obj
# survive too, but deleted files leak.
#
# /dev/null is the safe default (CLI omits the source flag) — a regular
# file, not a directory, so the test below is false and we fall back to
# the image's baked /workspace/repo.

if [[ -d /workspace/src && -n "$(ls -A /workspace/src 2>/dev/null)" ]]; then
  src_files=$(find /workspace/src -type f 2>/dev/null | wc -l)
  if command -v rsync >/dev/null 2>&1; then
    echo "syncing /workspace/src → /workspace/repo (${src_files} files, rsync, preserving bin/obj)"
    rsync -a --delete --exclude='bin/' --exclude='obj/' --exclude='.git/' \
          /workspace/src/ "${REPO}/"
  else
    echo "syncing /workspace/src → /workspace/repo (${src_files} files, cp -a fallback, bin/obj preserved but no delete)"
    cp -a /workspace/src/. "${REPO}/"
  fi
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

# stage_skip <name> <reason>
# Records a stage as ok=true with duration=0 and a "skipped" note. Used by
# `do_migrate` when it detects the DB schema is already current (saving
# ~5s of `dotnet ef` tool startup per context).
stage_skip() {
  local name=$1 reason=$2
  echo "=== ${name} (skipped: ${reason}) ===" | tee -a "${RESULTS_DIR}/run.log"
  jq --arg n "${name}" --arg r "${reason}" \
     '.stages[$n] = {ok: true, duration_s: 0, skipped: true, reason: $r}' \
     "${RESULT_JSON}" > "${RESULT_JSON}.tmp" && mv "${RESULT_JSON}.tmp" "${RESULT_JSON}"
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
  export ConnectionStrings__CatalogConnection="Server=${SQL_HOST};Database=${DB_CATALOG};User=sa;Password=${MSSQL_SA_PASSWORD};TrustServerCertificate=true"
  export ConnectionStrings__IdentityConnection="Server=${SQL_HOST};Database=${DB_IDENTITY};User=sa;Password=${MSSQL_SA_PASSWORD};TrustServerCertificate=true"

  # eShop has two DbContexts (CatalogContext + AppIdentityDbContext);
  # dotnet ef refuses to pick implicitly. Migrate each explicitly — but
  # only if the schema is actually behind. `dotnet ef` startup costs
  # ~4-5s per context EVEN WHEN IT'S A NO-OP, so skipping when the DB is
  # already current saves ~10s on the warm path.
  migrate_or_skip migrate_catalog  "${DB_CATALOG}"  CatalogContext        src/Infrastructure/Data/Migrations
  migrate_or_skip migrate_identity "${DB_IDENTITY}" AppIdentityDbContext  src/Infrastructure/Identity/Migrations
}

# migrate_or_skip <stage_name> <db_name> <context> <source_migrations_dir>
#
# Compares the number of *.cs files in the source migrations dir (excluding
# .Designer.cs and *Snapshot.cs) to the count of rows in __EFMigrationsHistory
# on the live DB. If DB >= source, skip the EF invocation. Otherwise run it.
#
# Edge cases:
# - DB count = 0 (e.g. __EFMigrationsHistory doesn't exist on a fresh DB,
#   sqlcmd errors out and we treat as 0): runs `dotnet ef` to migrate.
# - source count = 0 (Migrations dir missing): runs `dotnet ef` defensively.
# - Agent ADDED a migration to source: source count > DB count → runs
#   `dotnet ef` to apply.
# - Agent REMOVED a migration: not handled — schema state is undefined
#   anyway; would need EF rollback which we don't try to automate.
migrate_or_skip() {
  local stage_name=$1 db_name=$2 context=$3 mig_dir=$4

  local src_count
  src_count=$(find "${mig_dir}" -maxdepth 1 -name '*_*.cs' \
                ! -name '*.Designer.cs' ! -name '*Snapshot.cs' \
                2>/dev/null | wc -l)
  src_count=${src_count// /}

  local db_count
  db_count=$(/opt/mssql-tools18/bin/sqlcmd \
                -S "${SQL_HOST}" -U sa -P "${MSSQL_SA_PASSWORD}" -No \
                -d "${db_name}" -h -1 \
                -Q "SET NOCOUNT ON; SELECT COUNT(*) FROM __EFMigrationsHistory" \
                2>/dev/null \
              | tr -d '[:space:]' | head -c 12 || true)
  [[ "${db_count}" =~ ^[0-9]+$ ]] || db_count=0

  if [[ "${src_count}" -gt 0 && "${db_count}" -ge "${src_count}" ]]; then
    stage_skip "${stage_name}" "DB has ${db_count} applied migrations >= source's ${src_count}"
    return 0
  fi

  echo "DB has ${db_count} applied migrations, source has ${src_count} — running dotnet ef"
  stage "${stage_name}" 30 dotnet ef database update \
      --project src/Infrastructure --startup-project src/Web \
      --context "${context}"
}

do_test() {
  do_migrate

  # NOTE: we deliberately do NOT spin up a separate `dotnet run` here.
  # eShopOnWeb's tests use `WebApplicationFactory<Program>` (in-memory
  # test server), so the test host owns the app lifecycle. The previous
  # version of this runner started `dotnet run` and waited for
  # /home_page_health_check, but that work (~3s wall) was wasted — tests
  # don't talk to it. If a caller needs a running app for manual probing,
  # `sandbox exec eshop -- bash` and start it by hand.

  # Parallel test toggle. The image bakes xunit.runner.json files into each
  # tests/<proj>/ dir to enable parallel collection execution (see eshop
  # Dockerfile). Set PARALLEL_TESTS=0 in the workload env to disable; the
  # runner.json files are removed at runtime so xUnit reverts to defaults.
  if [[ "${PARALLEL_TESTS:-1}" == "0" ]]; then
    echo "PARALLEL_TESTS=0; removing xunit.runner.json overrides for this run"
    find "${REPO}/tests" -maxdepth 2 -name xunit.runner.json -delete 2>/dev/null || true
  fi

  # TEST_FILTER lets the caller scope the test run (e.g. to a single class
  # or a category). The agent loop usually doesn't need the full 113-test
  # suite — it just changed one thing. Format follows `dotnet test --filter`:
  #   "FullyQualifiedName~BasketServiceTests"
  #   "Category=Integration"
  #   "ClassName=BasketServiceTests"
  #
  # Empty → runs everything (back-compat default).
  local filter_args=()
  if [[ -n "${TEST_FILTER:-}" ]]; then
    echo "TEST_FILTER=${TEST_FILTER}"
    filter_args+=(--filter "${TEST_FILTER}")
  fi

  # Use the built-in trx logger only (junit logger needs JUnitXml.TestLogger
  # in each test csproj — we don't modify eShop). TRX is structured XML and
  # easy to convert downstream.
  stage test 20 dotnet test eShopOnWeb.sln --no-build -c Release \
      --logger "trx;LogFileName=results.trx" \
      --results-directory "${RESULTS_DIR}" \
      "${filter_args[@]}"
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
