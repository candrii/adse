#!/usr/bin/env python3
"""Thin CLI over `docker compose` for per-project sandboxes.

This is the *only* layer the harness offers above raw Docker. It doesn't
provide a stateful service or REST API — it's a script. The brief is about
the sandbox substrate, not orchestration; this CLI is what a CI runner,
an AI harness, or a developer would call to drive a sandbox.

Each project is split across TWO compose files with independent lifecycles:
  - compose/databases/<project>.yml   long-lived DB stack (project name
                                      ai-harness-<project>-db). Owns the
                                      network. Started once per session.
  - compose/<project>.yml             ephemeral workload stack (project
                                      name ai-harness-<project>). Joins the
                                      DB's network as `external: true`.
                                      Created + destroyed per iteration.

Subcommands:
  build    <project>                   docker build the sandbox image
  db-up    <project>                   start the DB stack (no-op if running)
  db-down  <project>                   tear down the DB stack
  warmup   <project>                   cold-up everything, run prep stages,
                                       commit DB + workload as `:warm`, tear
                                       down. One-time per image change.
  run      <project> <stage>           ensure DB up → workload up → exec
                                       runner.sh <stage> → collect /results
                                       → workload down (DB stays warm)
  exec     <project> -- <cmd>...       up if not running → exec arbitrary cmd
  destroy  <project>                   tear down workload AND DB
  ps       <project>                   compose ps (both stacks)
  logs     <project> [svc]             compose logs (both stacks)

Per-run isolation: `RUN_ID` is generated per run and exported into the
compose env so each run gets a unique `./out/<project>/<run-id>/` results
dir. The DB persists across runs; the workload container is recreated each
time, providing per-iteration isolation for the application layer.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

# ─────────────────────── project registry ───────────────────────

PROJECTS: dict[str, dict] = {
    "eshop": {
        "compose":     "compose/eshop.yml",                  # workload stack
        "db_compose":  "compose/databases/eshop.yml",        # database stack
        "service":     "eshop",                               # workload service name
        "db_services": ["sqlserver"],                         # DB services (for warm tagging)
        "compose_project":    "ai-harness-eshop",
        "db_compose_project": "ai-harness-eshop-db",
        "image":       "ai-harness/eshop:latest",
        "warm_image":  "ai-harness/eshop:warm",
        "build_ctx":   "images/eshop",
        # Host checkout used as named build context (eshop-src). Cloned by
        # cmd_build if missing. The Dockerfile expects this via
        # `--build-context eshop-src=<this path>`.
        "src_repo":    "https://github.com/NimblePros/eShopOnWeb.git",
        "src_branch":  "main",
        "src_dir":     "checkouts/eshop",
        "src_context": "eshop-src",
        "secrets":     ["MSSQL_SA_PASSWORD"],
        "stages":      ["build", "test", "all"],
    },
    "medplum": {
        "compose":     "compose/medplum.yml",
        "db_compose":  "compose/databases/medplum.yml",
        "service":     "medplum",
        "db_services": ["postgres", "redis"],
        "compose_project":    "ai-harness-medplum",
        "db_compose_project": "ai-harness-medplum-db",
        "image":       "ai-harness/medplum:latest",
        "warm_image":  "ai-harness/medplum:warm",
        "build_ctx":   "images/medplum",
        "src_repo":    "https://github.com/medplum/medplum.git",
        "src_branch":  "main",
        "src_dir":     "checkouts/medplum",
        "src_context": "medplum-src",
        "secrets":     ["POSTGRES_PASSWORD"],
        "stages":      ["build", "test", "all"],
    },
}


# ─────────────────────── helpers ───────────────────────


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _check(*cmd: str, env: dict | None = None, capture: bool = False, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    """Run a command; return CompletedProcess. Caller decides how to interpret exit code."""
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    return subprocess.run(
        list(cmd),
        env=     proc_env,
        cwd=     cwd or REPO_ROOT,
        check=   False,
        capture_output= capture,
        text=    True,
    )


def _must(*cmd: str, env: dict | None = None) -> None:
    """Run; raise SystemExit on non-zero."""
    rc = _check(*cmd, env=env).returncode
    if rc != 0:
        sys.exit(rc)


def _project(name: str) -> dict:
    if name not in PROJECTS:
        sys.exit(f"unknown project: {name} (have: {', '.join(PROJECTS)})")
    return PROJECTS[name]


def _require_env(project: dict) -> dict:
    """Pull project secrets from the host env (or .env)."""
    missing = [s for s in project["secrets"] if not os.environ.get(s)]
    if missing:
        sys.exit(f"missing env vars: {', '.join(missing)} (export them or set in .env)")
    return {s: os.environ[s] for s in project["secrets"]}


def _compose_base_env(project: dict, rid: str | None = None) -> dict:
    """Build the env passed to `docker compose` invocations.

    All compose files (workload AND db) share these required vars.
    """
    base = _require_env(project)
    rid = rid or os.environ.get("RUN_ID", "default")
    base["RUN_ID"] = rid

    # RESULTS_DIR / SOURCE_DIR / MEMORY_DIR are required by the workload
    # compose's volume binds (`${VAR:?...}` syntax). For ops that don't
    # actually start the workload (db-up, ps, logs) the paths still have
    # to resolve, so we point them at placeholders. `cmd_run` overrides
    # them with real paths.
    #
    # CRITICAL: SOURCE_DIR and RESULTS_DIR must point at SEPARATE
    # placeholder dirs. SOURCE_DIR is rsync'd onto /workspace/repo by
    # runner.sh (with --delete), so it must always be empty when there's
    # no real source. RESULTS_DIR gets written to (logs, result.json).
    # If both pointed at the same dir, a previous run's logs would appear
    # as the next run's "source", and runner.sh would rsync them onto
    # /workspace/repo, deleting most of the repo. Use separate dirs.
    #
    # Why per-run paths? Docker Desktop on WSL2 stages each unique host
    # bind-source under /run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/
    # Ubuntu/<sha> where <sha> = SHA256(host_path). Reusing the same host
    # path across runs hits that cached staging entry, which gets corrupted
    # if a prior run mounted the same dst with a different source.
    empty_dir = REPO_ROOT / "out" / "_placeholder" / "empty"
    empty_dir.mkdir(parents=True, exist_ok=True)
    results_default = REPO_ROOT / "out" / "_placeholder" / "results" / rid
    results_default.mkdir(parents=True, exist_ok=True)
    base.setdefault("RESULTS_DIR", str(results_default))
    base.setdefault("SOURCE_DIR",  str(empty_dir))
    base.setdefault("MEMORY_DIR",  str(empty_dir))
    base.setdefault("TASK_ID",     "")
    base.setdefault("TEST_FILTER", "")
    return base


def _compose(project: dict, *args: str, env: dict | None = None,
             capture: bool = False, db: bool = False) -> subprocess.CompletedProcess:
    """Run docker compose against the workload OR db stack.

    `db=True` targets compose/databases/<project>.yml under its own
    compose project name. Default targets the workload compose.
    """
    base = _compose_base_env(project, rid=(env or {}).get("RUN_ID"))
    if env:
        base.update(env)
    compose_file = project["db_compose"] if db else project["compose"]
    project_name = project["db_compose_project"] if db else project["compose_project"]
    return _check("docker", "compose",
                  "-p", project_name,
                  "-f", compose_file,
                  *args,
                  env=base, capture=capture)


def _db_is_up(project: dict) -> bool:
    """True if the DB stack has at least one running container."""
    r = _compose(project, "ps", "-q", capture=True, db=True)
    return bool(r.stdout.strip())


def _image_exists(tag: str) -> bool:
    r = _check("docker", "image", "inspect", tag, capture=True)
    return r.returncode == 0


def _resolve_warm_images(project: dict, args_snapshot: str, env: dict) -> None:
    """If --snapshot warm and warm tags exist, inject them into `env`."""
    if args_snapshot != "warm":
        return
    warm_app = f"ai-harness/{_project_name_from_dict(project)}-{project['service']}:warm"
    if _image_exists(warm_app):
        env_var = f"{_project_name_from_dict(project).upper()}_IMAGE"
        env[env_var] = warm_app
        print(f"⟳ using warm baseline: {warm_app}", file=sys.stderr)
    else:
        print(f"⚠ requested --snapshot warm but {warm_app} not found; using :latest", file=sys.stderr)
    for svc in project["db_services"]:
        warm_db = f"ai-harness/{_project_name_from_dict(project)}-{svc}:warm"
        if _image_exists(warm_db):
            env[f"{svc.upper()}_IMAGE"] = warm_db
            print(f"⟳ using warm baseline: {warm_db}", file=sys.stderr)


def _project_name_from_dict(project: dict) -> str:
    """Reverse-lookup the project's short name (eshop / medplum) from its dict."""
    for k, v in PROJECTS.items():
        if v is project:
            return k
    raise RuntimeError("project not in registry")


def _now_ts() -> int:
    return int(time.time())


def _now_rid_stamp() -> str:
    """Sortable + human-readable timestamp for use in run_id directory names.

    Format: YYYY-MM-DDTHH-MM-SS  (e.g. 2026-05-17T04-30-15)
    - Lexicographic sort = chronological sort
    - Filesystem-safe (no colons; Windows / Docker Desktop friendly)
    - Locally legible without epoch math
    Local time, not UTC — these dirs are for humans inspecting them locally.
    """
    return time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime())


# ─────────────────────── subcommands ───────────────────────


def cmd_fetch_source(args: argparse.Namespace) -> int:
    """Clone the project's upstream repo to checkouts/<project> if missing.

    Idempotent. If the checkout already exists, this is a no-op (use
    `--refresh` to pull). The Dockerfile takes its source from here via
    BuildKit's named build context (`--build-context <name>=<dir>`).
    """
    p = _project(args.project)
    dst = REPO_ROOT / p["src_dir"]
    if dst.exists():
        if getattr(args, "refresh", False):
            print(f"⟳ refreshing {dst} (git fetch + reset)", file=sys.stderr)
            rc = _check("git", "-C", str(dst), "fetch", "--depth=1", "origin", p["src_branch"]).returncode
            if rc != 0:
                return rc
            return _check("git", "-C", str(dst), "reset", "--hard", "FETCH_HEAD").returncode
        print(f"⟳ source already at {dst.relative_to(REPO_ROOT)} (use --refresh to update)", file=sys.stderr)
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"⟳ cloning {p['src_repo']} -> {dst.relative_to(REPO_ROOT)}", file=sys.stderr)
    return _check("git", "clone", "--depth=1", "--branch", p["src_branch"],
                  p["src_repo"], str(dst)).returncode


def cmd_build(args: argparse.Namespace) -> int:
    """docker buildx build with the host checkout as a named build context."""
    p = _project(args.project)

    # Make sure the host checkout exists. Cold first-time build clones it.
    src_dir = REPO_ROOT / p["src_dir"]
    if not src_dir.exists():
        print(f"⟳ host checkout missing; auto fetch-source", file=sys.stderr)
        rc = _check("git", "clone", "--depth=1", "--branch", p["src_branch"],
                    p["src_repo"], str(src_dir)).returncode
        if rc != 0:
            return rc

    # Deposit .dockerignore at the build-context root so BuildKit excludes
    # bin/obj/node_modules from the transferred files. This is the second
    # half of the layer-ordering / cache-mount story: a small context
    # transfers fast AND yields stable cache keys when source files outside
    # the ignore list haven't changed.
    template = REPO_ROOT / p["build_ctx"] / "dockerignore.template"
    if template.exists():
        dst = src_dir / ".dockerignore"
        dst.write_text(template.read_text())

    print(f"⟳ docker buildx build {p['image']}", file=sys.stderr)
    print(f"   build context: {p['build_ctx']}", file=sys.stderr)
    print(f"   {p['src_context']}: {src_dir.relative_to(REPO_ROOT)}", file=sys.stderr)
    return _check(
        "docker", "buildx", "build",
        "--load",                                          # write to local image store
        "--build-context", f"{p['src_context']}={src_dir}",
        "-t", p["image"],
        "-f", f"{p['build_ctx']}/Dockerfile",
        p["build_ctx"],
    ).returncode


def cmd_db_up(args: argparse.Namespace) -> int:
    """Bring up the DB stack. No-op if it's already running."""
    p = _project(args.project)
    if _db_is_up(p):
        print(f"⟳ {args.project} DB stack already up", file=sys.stderr)
        return 0
    env: dict = {}
    _resolve_warm_images(p, args.snapshot, env)
    print(f"⟳ db-up {args.project} (project: {p['db_compose_project']})", file=sys.stderr)
    rc = _compose(p, "up", "-d", "--wait", env=env, db=True).returncode
    if rc != 0:
        _compose(p, "logs", "--tail=30", env=env, db=True)
    return rc


def cmd_db_down(args: argparse.Namespace) -> int:
    """Tear down the DB stack (volumes wiped)."""
    p = _project(args.project)
    print(f"⟳ db-down {args.project}", file=sys.stderr)
    return _compose(p, "down", "-v", db=True).returncode


def cmd_warmup(args: argparse.Namespace) -> int:
    """Bring up cold, run prep (restore + build + migrate), commit warm baselines.

    Two stacks come up: the DB stack (which creates the network) and the
    workload stack (which joins it). We commit ALL containers across BOTH
    stacks so the next `db-up` + `run` can use :warm tags.
    """
    p = _project(args.project)
    # Give warmup its own results dir so logs are captured (and don't pollute
    # the shared placeholder, which would then look like a non-empty source
    # to a future run's rsync overlay).
    warmup_rid = f"{_now_rid_stamp()}-warmup"
    warmup_results = REPO_ROOT / "out" / args.project / warmup_rid
    warmup_results.mkdir(parents=True, exist_ok=True)
    warmup_results.chmod(0o777)
    env: dict = {
        "RUN_ID":      warmup_rid,
        "RESULTS_DIR": str(warmup_results.resolve()),
    }

    # Cold-up the DB stack first (it creates the network).
    print(f"⟳ warmup: cold-up DB stack", file=sys.stderr)
    if _compose(p, "up", "-d", "--wait", env=env, db=True).returncode != 0:
        _compose(p, "logs", "--tail=30", env=env, db=True)
        return 1

    # Cold-up the workload stack.
    print(f"⟳ warmup: cold-up workload stack", file=sys.stderr)
    if _compose(p, "up", "-d", "--wait", env=env).returncode != 0:
        _compose(p, "logs", "--tail=30", env=env)
        _compose(p, "down", "-v", env=env)
        _compose(p, "down", "-v", env=env, db=True)
        return 1

    print(f"⟳ warmup: running prep stages (restore + build + migrate)", file=sys.stderr)
    rc = _compose(p, "exec", "-T", p["service"],
                  "/usr/local/bin/runner.sh", "warmup", env=env).returncode
    if rc != 0:
        print(f"✗ warmup stages failed (exit {rc}); leaving containers up for inspection", file=sys.stderr)
        return rc

    # Snapshot containers in BOTH stacks as :warm.
    #
    # Gotcha: SQL Server tolerates being committed mid-flight (its writable
    # layer is consistent enough that startup recovery is fast), but Postgres
    # does NOT. A live `docker commit` on Postgres captures dirty shared
    # buffers + half-written WAL state, and the next start spends 60-120s
    # in "syncing data directory (fsync)" doing crash recovery — defeating
    # the entire :warm-startup-time optimization.
    #
    # Solution: gracefully stop DB containers before committing them.
    # `docker stop` sends SIGTERM, which Postgres handles by checkpointing,
    # flushing WAL, then exiting cleanly within ~1-2s. Once the container
    # is exited, its writable layer is in a quiescent state and the
    # resulting `:warm` image starts in seconds without recovery.
    #
    # Workload containers don't have this issue (no DB engine inside them),
    # so we commit them live.
    print(f"⟳ committing warm baselines", file=sys.stderr)
    db_service_names = set(p.get("db_services", []))
    for db_flag, label in [(True, "db"), (False, "workload")]:
        ps = _compose(p, "ps", "--format", "json", capture=True, db=db_flag)
        if ps.returncode != 0:
            continue
        services = [json.loads(line) for line in ps.stdout.strip().split("\n") if line.strip()]
        for svc in services:
            name      = svc["Name"]
            svc_name  = svc.get("Service", "")
            warm_tag  = f"ai-harness/{args.project}-{svc_name}:warm"
            # If this is a DB service that maintains on-disk state, stop it
            # gracefully so its writable layer is consistent at commit time.
            if svc_name in db_service_names:
                print(f"⟳ stopping {name} gracefully before commit (SIGTERM, 30s timeout)", file=sys.stderr)
                _must("docker", "stop", "-t", "30", name)
            print(f"⟳ committing {name} ({label}) → {warm_tag}", file=sys.stderr)
            _must("docker", "commit", name, warm_tag)

    # Tear both stacks down.
    print(f"⟳ warmup: tearing down workload + DB", file=sys.stderr)
    _compose(p, "down", "-v", env=env)
    _compose(p, "down", "-v", env=env, db=True)
    print(f"✓ warmup complete. Future `db-up` + `run` use :warm baselines.", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run a stage end-to-end against the (possibly already-up) DB stack.

    Lifecycle:
      1. Ensure DB stack is up (auto db-up if needed; from :warm if present)
      2. Bring up workload (from :warm if present)
      3. Exec runner.sh <stage>
      4. Collect /results
      5. Tear down WORKLOAD ONLY — the DB stack stays running for the next
         iteration. Caller invokes `sandbox db-down` (or `sandbox destroy`)
         when they want to recycle the DB too.

    Returns the runner.sh exit code so callers can route on it.
    """
    p   = _project(args.project)
    # Run-id format: <local-iso-timestamp>-<short-stage>-<6hex>
    # Sortable + readable directory names, e.g. 2026-05-17T04-30-15-test-a1b2c3
    rid = args.run_id or f"{_now_rid_stamp()}-{args.stage}-{uuid.uuid4().hex[:6]}"
    env: dict = {"RUN_ID": rid}

    _resolve_warm_images(p, args.snapshot, env)

    # Empty-dir placeholder for SOURCE_DIR / MEMORY_DIR defaults. Always
    # the same dir (it stays empty). See _compose_base_env() for why.
    empty_dir = REPO_ROOT / "out" / "_placeholder" / "empty"
    empty_dir.mkdir(parents=True, exist_ok=True)

    # Task-scoped vs ephemeral output paths.
    if args.task:
        task_dir = REPO_ROOT / "out" / args.project / args.task
        task_dir.mkdir(parents=True, exist_ok=True)
        memory_dir = task_dir / "memory"
        memory_dir.mkdir(exist_ok=True)
        memory_dir.chmod(0o777)
        out_dir = task_dir / rid
        env["TASK_ID"]    = args.task
        env["MEMORY_DIR"] = str(memory_dir.resolve())
        print(f"⟳ task={args.task}  memory={memory_dir.relative_to(REPO_ROOT)}", file=sys.stderr)
    else:
        task_dir   = None
        out_dir    = REPO_ROOT / "out" / args.project / rid
        env["MEMORY_DIR"] = str(empty_dir.resolve())

    env.setdefault("SOURCE_DIR", str(empty_dir.resolve()))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o777)
    env["RESULTS_DIR"] = str(out_dir.resolve())

    if args.source:
        src = pathlib.Path(args.source).resolve()
        if not src.is_dir():
            sys.exit(f"--source must be a directory: {src}")
        env["SOURCE_DIR"] = str(src)
        print(f"⟳ source={src}  (rsync overlay onto warm /workspace/repo)", file=sys.stderr)

    if getattr(args, "test_filter", None):
        env["TEST_FILTER"] = args.test_filter
        print(f"⟳ filter={args.test_filter}", file=sys.stderr)

    print(f"⟳ run_id={rid}  out={out_dir.relative_to(REPO_ROOT)}", file=sys.stderr)
    started_at = _now_ts()

    # 1. Ensure DB stack is up.
    db_was_up = _db_is_up(p)
    if not db_was_up:
        print(f"⟳ DB stack not running; auto db-up", file=sys.stderr)
        if _compose(p, "up", "-d", "--wait", env=env, db=True).returncode != 0:
            _compose(p, "logs", "--tail=30", env=env, db=True)
            return 1
    else:
        print(f"⟳ DB stack already up (skipping startup cost)", file=sys.stderr)

    # 2. Bring up workload.
    print(f"⟳ compose up (workload)", file=sys.stderr)
    if _compose(p, "up", "-d", "--wait", env=env).returncode != 0:
        _compose(p, "logs", "--tail=40", env=env)
        _compose(p, "down", "-v", env=env)
        return 1

    # 3. Exec the requested stage.
    print(f"⟳ exec runner.sh {args.stage}", file=sys.stderr)
    exec_rc = _compose(p, "exec", "-T", p["service"],
                       "/usr/local/bin/runner.sh", args.stage, env=env).returncode

    # 4. Tear down workload only — DB stays up for the next iteration.
    print(f"⟳ compose down -v (workload only; DB stays up)", file=sys.stderr)
    _compose(p, "down", "-v", env=env)

    finished_at = _now_ts()

    # Append to the task's iterations log.
    if task_dir is not None:
        iteration_record = {
            "run_id":       rid,
            "task_id":      args.task,
            "stage":        args.stage,
            "snapshot":     args.snapshot,
            "source_dir":   str(pathlib.Path(args.source).resolve()) if args.source else None,
            "db_was_up":    db_was_up,
            "started_at":   started_at,
            "finished_at":  finished_at,
            "duration_s":   finished_at - started_at,
            "exit_code":    exec_rc,
            "result_path":  str(out_dir.relative_to(REPO_ROOT)),
        }
        with open(task_dir / "iterations.jsonl", "a") as f:
            f.write(json.dumps(iteration_record) + "\n")

    # Surface the result.json on stdout.
    result_json = out_dir / "result.json"
    if result_json.exists():
        print(result_json.read_text())
    else:
        print(json.dumps({"error": "no result.json produced",
                          "exit_code": exec_rc,
                          "run_id":    rid,
                          "out":       str(out_dir.relative_to(REPO_ROOT))}))

    return exec_rc


def cmd_exec(args: argparse.Namespace) -> int:
    """Open-ended exec into a running (or freshly-up'd) sandbox."""
    p = _project(args.project)
    # Ensure DB is up (workload's external network depends on it).
    if not _db_is_up(p):
        if _compose(p, "up", "-d", "--wait", db=True).returncode != 0:
            return 1
    # Ensure workload is up.
    if _compose(p, "ps", "-q", p["service"], capture=True).stdout.strip() == "":
        if _compose(p, "up", "-d", "--wait").returncode != 0:
            return 1
    cmd = args.cmd or ["bash"]
    return _compose(p, "exec", p["service"], *cmd).returncode


def cmd_destroy(args: argparse.Namespace) -> int:
    """Tear down both stacks (workload AND DB)."""
    p = _project(args.project)
    rc1 = _compose(p, "down", "-v").returncode
    rc2 = _compose(p, "down", "-v", db=True).returncode
    return rc1 or rc2


def cmd_ps(args: argparse.Namespace) -> int:
    p = _project(args.project)
    print("=== DB stack ===", file=sys.stderr)
    _compose(p, "ps", db=True)
    print("=== Workload stack ===", file=sys.stderr)
    return _compose(p, "ps").returncode


def cmd_logs(args: argparse.Namespace) -> int:
    p = _project(args.project)
    extra = [args.service] if args.service else []
    # `--all` makes compose include both running and stopped containers.
    rc1 = _compose(p, "logs", "--tail=200", *extra, db=True).returncode
    rc2 = _compose(p, "logs", "--tail=200", *extra).returncode
    return rc1 or rc2


# ─────────────────────── task memory ───────────────────────


def cmd_tasks_ls(args: argparse.Namespace) -> int:
    """List task_ids that have any iteration history."""
    projects = [args.project] if args.project else list(PROJECTS)
    rows = []
    for proj in projects:
        proj_dir = REPO_ROOT / "out" / proj
        if not proj_dir.is_dir():
            continue
        for task_dir in sorted(proj_dir.iterdir()):
            jsonl = task_dir / "iterations.jsonl"
            if not jsonl.is_file():
                continue
            lines = jsonl.read_text().splitlines()
            if not lines:
                continue
            last = json.loads(lines[-1])
            rows.append({
                "project":     proj,
                "task_id":     task_dir.name,
                "iterations":  len(lines),
                "last_run":    last.get("run_id", "?"),
                "last_status": "ok" if last.get("exit_code") == 0 else "fail",
                "last_at":     last.get("finished_at"),
            })

    if not rows:
        print("(no tasks with iteration history)")
        return 0

    if args.format == "json":
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'PROJECT':<10} {'TASK_ID':<28} {'ITERS':>6} {'LAST':<6}  {'LAST_RUN'}")
        for r in rows:
            print(f"{r['project']:<10} {r['task_id']:<28} "
                  f"{r['iterations']:>6} {r['last_status']:<6}  {r['last_run']}")
    return 0


def cmd_tasks_show(args: argparse.Namespace) -> int:
    """Show one task's iteration history (iterations.jsonl pretty-printed)."""
    task_dir = REPO_ROOT / "out" / args.project / args.task_id
    jsonl = task_dir / "iterations.jsonl"
    if not jsonl.is_file():
        sys.exit(f"no task: out/{args.project}/{args.task_id}/iterations.jsonl")

    iterations = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]

    if args.format == "json":
        print(json.dumps(iterations, indent=2))
        return 0

    print(f"task: {args.task_id}   project: {args.project}   iterations: {len(iterations)}")
    memory_dir = task_dir / "memory"
    if memory_dir.is_dir() and any(memory_dir.iterdir()):
        files = [str(p.relative_to(memory_dir)) for p in memory_dir.rglob("*") if p.is_file()]
        print(f"memory: {memory_dir.relative_to(REPO_ROOT)} ({len(files)} files: {', '.join(files[:5])}{'...' if len(files) > 5 else ''})")
    print()
    print(f"{'#':>3}  {'RUN_ID':<26} {'STAGE':<7} {'EXIT':>4}  {'DUR':>4}s  {'DB_UP':<5}  {'SOURCE'}")
    for i, it in enumerate(iterations, 1):
        src = it.get("source_dir") or "-"
        if src != "-":
            src = ".../" + pathlib.Path(src).name
        db_up = "yes" if it.get("db_was_up") else "no"
        print(f"{i:>3}  {it['run_id']:<26} {it.get('stage','?'):<7} "
              f"{it.get('exit_code','?'):>4}  {it.get('duration_s','?'):>4}s  {db_up:<5}  {src}")
    return 0


def cmd_tasks_memory(args: argparse.Namespace) -> int:
    """Print the path of a task's memory dir."""
    memory_dir = REPO_ROOT / "out" / args.project / args.task_id / "memory"
    if not memory_dir.is_dir():
        sys.exit(f"no memory dir: out/{args.project}/{args.task_id}/memory/")
    print(memory_dir)
    return 0


# ─────────────────────── parser ───────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sandbox", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_project_arg(sp):
        sp.add_argument("project", choices=list(PROJECTS))

    fs = sub.add_parser("fetch-source",
                        help="clone the project's upstream repo to checkouts/<project>/")
    add_project_arg(fs)
    fs.add_argument("--refresh", action="store_true",
                    help="if checkout exists, git fetch + reset --hard to remote HEAD")
    fs.set_defaults(func=cmd_fetch_source)

    b = sub.add_parser("build",   help="docker buildx build (auto-clones source if missing)")
    add_project_arg(b); b.set_defaults(func=cmd_build)

    w = sub.add_parser("warmup",  help="cold-start once, then docker commit warm baselines (DB + workload)")
    add_project_arg(w); w.set_defaults(func=cmd_warmup)

    du = sub.add_parser("db-up",  help="bring up the DB stack (no-op if running)")
    add_project_arg(du)
    du.add_argument("--snapshot", choices=["warm", "cold"], default="warm",
                    help="`warm` uses :warm DB tag if present; `cold` always cold-starts the DB")
    du.set_defaults(func=cmd_db_up)

    dd = sub.add_parser("db-down", help="tear down the DB stack")
    add_project_arg(dd); dd.set_defaults(func=cmd_db_down)

    r = sub.add_parser("run",     help="ensure DB up -> workload up -> exec runner.sh <stage> -> workload down")
    add_project_arg(r)
    r.add_argument("stage",       choices=["build", "test", "all"], help="runner.sh subcommand")
    r.add_argument("--snapshot",  choices=["warm", "cold"], default="warm",
                   help="`warm` uses :warm-tagged baseline if present; `cold` always rebuilds from :latest")
    r.add_argument("--run-id",    help="override the generated run id")
    r.add_argument("--task",      metavar="ID",
                   help="task identifier — enables substrate memory (persistent iteration log + writable /memory mount). "
                        "Without it, this run is ephemeral.")
    r.add_argument("--source",    metavar="DIR",
                   help="host directory rsync'd onto /workspace/repo before stages. "
                        "Preserves bin/obj/.git so the incremental build path is reused.")
    r.add_argument("--filter",    metavar="EXPR", dest="test_filter",
                   help="dotnet test --filter expression for the test stage. "
                        "E.g. 'ClassName=BasketServiceTests' or 'FullyQualifiedName~ApiAuth'. "
                        "Narrows the test scope from the full 113-test suite to whatever the "
                        "agent actually wants to check. Empty = run everything (default).")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("exec",    help="exec into the running sandbox (ad-hoc)")
    add_project_arg(e)
    e.add_argument("cmd",         nargs=argparse.REMAINDER, help="command + args after --")
    e.set_defaults(func=cmd_exec)

    d = sub.add_parser("destroy", help="tear down workload AND DB stacks");   add_project_arg(d); d.set_defaults(func=cmd_destroy)
    ps = sub.add_parser("ps",     help="compose ps for both stacks");          add_project_arg(ps); ps.set_defaults(func=cmd_ps)
    lg = sub.add_parser("logs",   help="compose logs for both stacks");        add_project_arg(lg); lg.add_argument("service", nargs="?"); lg.set_defaults(func=cmd_logs)

    # --- task memory subcommand group ---
    t  = sub.add_parser("tasks",  help="inspect substrate task memory (iteration history + memory dir)")
    tsub = t.add_subparsers(dest="task_cmd", required=True)

    tls = tsub.add_parser("ls",   help="list task_ids across projects (or scoped to one)")
    tls.add_argument("project",   nargs="?", choices=list(PROJECTS),
                     help="optional: only show tasks for this project")
    tls.add_argument("--format",  choices=["table", "json"], default="table")
    tls.set_defaults(func=cmd_tasks_ls)

    tshow = tsub.add_parser("show", help="show iterations.jsonl for one task")
    add_project_arg(tshow)
    tshow.add_argument("task_id")
    tshow.add_argument("--format", choices=["table", "json"], default="table")
    tshow.set_defaults(func=cmd_tasks_show)

    tmem = tsub.add_parser("memory", help="print path to a task's memory dir")
    add_project_arg(tmem)
    tmem.add_argument("task_id")
    tmem.set_defaults(func=cmd_tasks_memory)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
