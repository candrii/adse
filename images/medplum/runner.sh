#!/usr/bin/env bash
# Medplum sandbox runner — subcommand CLI.
#
# Mirrors images/eshop/runner.sh's contract: structured stages, stable exit
# codes, /results/result.json with per-stage timing. See eshop/runner.sh
# for the exit-code table and result.json shape.
#
# Subcommands:
#   build   → install deps + tsc build of @medplum/server
#   warmup  → build + migrate (no test; for `sandbox warmup`)
#   test    → ensure-built → migrate (skipped if schema current) → test
#   all     → build + test
#
# Medplum monorepo specifics:
#   - npm workspaces, NOT pnpm; package.json pins `packageManager: npm@...`
#   - jest, NOT vitest; jest's --testNamePattern is the equivalent of
#     `dotnet test --filter`
#   - turbo coordinates cross-package builds; we scope to @medplum/server
#     via `npm run build --workspace=packages/server` to keep iterations fast

set -uo pipefail

readonly RESULTS_DIR=/results
readonly REPO=/workspace/repo
readonly RUN_ID=${RUN_ID:-$(date +%s)-$$}
readonly PG_HOST=${PG_HOST:-postgres}
readonly REDIS_HOST=${REDIS_HOST:-redis}
# Stable DB name — see eshop runner for the warm-snapshot rationale.
# Postgres data lives on tmpfs (image declares a VOLUME so commit can't
# capture it anyway), so this DB is re-created every cold DB start. Within
# a session (DB stays up) it persists across workload iterations.
readonly DB_NAME=${DB_NAME:-medplum}

readonly STAGE=${1:-all}

mkdir -p "${RESULTS_DIR}"

# ───────────────────────── source override (incremental builds) ─────────────────────────
# Same overlay pattern as eshop. Excludes node_modules/dist/.git to
# preserve the warm install + build artifacts.

if [[ -d /workspace/src && -n "$(ls -A /workspace/src 2>/dev/null)" ]]; then
  src_files=$(find /workspace/src -type f 2>/dev/null | wc -l)
  if command -v rsync >/dev/null 2>&1; then
    echo "syncing /workspace/src → /workspace/repo (${src_files} files, rsync, preserving node_modules/dist)"
    rsync -a --delete \
          --exclude='node_modules/' --exclude='dist/' \
          --exclude='.git/' --exclude='.turbo/' \
          /workspace/src/ "${REPO}/"
  else
    echo "syncing /workspace/src → /workspace/repo (${src_files} files, cp -a fallback)"
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
  "project": "medplum",
  "stage":   "${STAGE}",
  "status":  "running",
  "exit_code": null,
  "memory_dir": $( [[ -d /memory ]] && echo '"/memory"' || echo 'null' ),
  "source_override": $( [[ -d /workspace/src && -n "$(ls -A /workspace/src 2>/dev/null)" ]] && echo true || echo false ),
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
    echo "── last 40 lines of ${log} ──" >&2
    tail -n 40 "${log}" >&2 || true
    exit "${fail_code}"
  fi
}

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

do_install() {
  # The image bakes node_modules at build time. On a warm container that
  # work is already done — re-running `npm ci` wipes node_modules and
  # restores it (~70-90s for medplum's 30-package monorepo), even though
  # the lockfile hasn't changed. Skip if node_modules looks intact.
  #
  # Trigger conditions for a real install:
  #   1. node_modules missing (cold container, somehow)
  #   2. package-lock.json was modified by the agent (via --source overlay)
  #
  # We compare lockfile mtimes against node_modules' marker to detect (2).
  if [[ -d node_modules && -f node_modules/.package-lock.json ]]; then
    if [[ "node_modules/.package-lock.json" -nt "package-lock.json" ]] || \
       ! [[ "package-lock.json" -nt "node_modules/.package-lock.json" ]]; then
      stage_skip install "node_modules present and lockfile unchanged"
      return 0
    fi
  fi
  stage install 10 npm ci --include=dev --prefer-offline --no-audit --no-fund
}

do_build() {
  # Build @medplum/server PLUS its in-repo workspace deps (@medplum/core,
  # @medplum/fhirtypes, @medplum/definitions, ...). Plain
  # `npm run build --workspace=...` only builds the requested workspace,
  # not its deps, so the server build can't resolve `@medplum/core`.
  # turbo's `--filter=...^...` syntax means "this package AND its
  # workspace dependencies"; that's exactly what we want for warm-start
  # plus incremental builds. Turbo also caches per-package build state
  # under .turbo/, so unchanged deps are skipped on iteration N+1.
  stage build 10 npx turbo run build --filter='@medplum/server^...'
}

# Medplum DB lifecycle notes:
#
# Unlike eshop, medplum doesn't ship a standalone migration tool that we
# can invoke against a DB. `packages/server/src/migrations/migrate-main.ts`
# is for AUTHORING migrations (it generates new migration files based on
# schema diff) — NOT applying them. Schema application happens via the
# server's startup path (when config.runMigrations=true) or via the test
# seed (jest packages/server/src/seed.test.ts).
#
# For our sandbox flow we use the seed path:
#   1. seed.test.ts populates `medplum_test` with full schema + fixtures
#   2. subsequent jest runs use the already-seeded DB
#
# The seed is idempotent — re-running it on a populated DB is a no-op
# checked by inspecting the `databasemigration` table. We still gate it
# behind a row-count check so warm iterations don't pay the ~30s setup.
#
# Connection: medplum's loadTestConfig reads POSTGRES_HOST + POSTGRES_PORT
# env vars and uses the password baked into medplum.config.json ("medplum").
# We pin POSTGRES_PASSWORD=medplum in .env so that matches.

patch_medplum_config() {
  # loadTestConfig (packages/server/src/config/loader.ts) reads
  # POSTGRES_HOST + POSTGRES_PORT from env but does NOT override
  # redis.host or redis.password. Those come from medplum.config.json
  # which ships with `redis.host=localhost` + `password=medplum`. In
  # our network namespace Redis is at `redis` and has no password.
  # Rewrite the file in place to point at our sidecars.
  #
  # Idempotent: if the config already has the right values we re-emit
  # the same content. The image's writable layer absorbs the change;
  # `compose down -v` discards it.
  local cfg=packages/server/medplum.config.json
  jq --arg host "${REDIS_HOST}" \
     '.redis.host = $host | .redis.password = ""' "${cfg}" \
     > "${cfg}.tmp" && mv "${cfg}.tmp" "${cfg}"
}

db_has_seed() {
  : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}"
  PGPASSWORD="${POSTGRES_PASSWORD}" \
    psql -h "${PG_HOST}" -U medplum -d medplum_test -tAc \
         "SELECT COUNT(*) FROM information_schema.tables \
          WHERE table_schema='public' AND table_name='DatabaseMigration'" \
         2>/dev/null \
    | tr -d '[:space:]' | head -c 4 | grep -q '^[1-9]'
}

do_seed() {
  patch_medplum_config
  if db_has_seed; then
    stage_skip seed "medplum_test schema already present (DatabaseMigration table exists)"
    return 0
  fi
  # First-iteration-of-session: populate the test DB. seed.test.ts is
  # tagged with --testTimeout=400000 in package.json's test:seed script
  # because it does a lot of work (~30-60s).
  stage seed 30 bash -c '
    cd packages/server &&
    POSTGRES_HOST="'"${PG_HOST}"'" \
    POSTGRES_PORT=5432 \
    npx jest seed.test.ts --testTimeout=400000
  '
}

do_test() {
  do_seed

  # TEST_FILTER for jest is interpreted as a `--testPathPattern` — i.e. a
  # FILE PATH regex that narrows which test files jest loads. This is the
  # only filter that actually saves time on a large monorepo: jest's
  # `--testNamePattern` would still load all 221 medplum test files (~10
  # minutes wall clock) to check which test() bodies match the name regex.
  # Path filtering avoids the file-load + transform cost entirely.
  #
  # Examples for the agent:
  #   TEST_FILTER=fhirpath     → only files whose path contains "fhirpath"
  #   TEST_FILTER='^src/fhir/' → anchored regex
  local filter_args=()
  if [[ -n "${TEST_FILTER:-}" ]]; then
    echo "TEST_FILTER=${TEST_FILTER} (interpreted as jest --testPathPatterns)"
    # `--testPathPatterns` (plural) is the modern form; `--testPathPattern`
    # (singular) was deprecated in jest 30+.
    filter_args+=(--testPathPatterns="${TEST_FILTER}")
  fi

  # Scope tests to @medplum/server. seed.test.ts is intentionally excluded
  # via `--testPathIgnorePatterns` since do_seed already handled it.
  stage test 20 bash -c '
    cd packages/server &&
    POSTGRES_HOST="'"${PG_HOST}"'" \
    POSTGRES_PORT=5432 \
    npx jest --passWithNoTests \
        --testPathIgnorePatterns=seed.test.ts \
        '"${filter_args[@]:+${filter_args[@]}}"'
  '
}

record_artifacts() {
  local artifacts
  artifacts=$(find "${RESULTS_DIR}" /workspace/repo/packages/server -maxdepth 3 -type f \
              \( -name 'junit.xml' -o -name '*.log' -o -name 'result.json' \) \
              2>/dev/null | jq -R -s 'split("\n")[:-1]')
  jq --argjson a "${artifacts}" '.artifacts = $a' "${RESULT_JSON}" \
     > "${RESULT_JSON}.tmp" && mv "${RESULT_JSON}.tmp" "${RESULT_JSON}"
}

# ───────────────────────── dispatch ─────────────────────────
cd "${REPO}" || { finalize 80 checkout_fail; exit 80; }

case "${STAGE}" in
  build)
    do_install
    do_build
    record_artifacts
    finalize 0 pass
    ;;
  warmup)
    do_install
    do_build
    do_seed
    record_artifacts
    finalize 0 pass
    ;;
  test)
    do_install
    do_build
    do_test
    record_artifacts
    finalize 0 pass
    ;;
  all)
    do_install
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
