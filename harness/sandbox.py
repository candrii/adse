#!/usr/bin/env python3
"""Thin CLI over `docker compose` for per-project sandboxes.

This is the *only* layer the harness offers above raw Docker. It doesn't
provide a stateful service or REST API — it's a script. The brief is about
the sandbox substrate, not orchestration; this CLI is what a CI runner,
an AI harness, or a developer would call to drive a sandbox.

Subcommands:
  build    <project>                   docker build the sandbox image
  warmup   <project>                   cold start once, run prep stages,
                                       then `docker commit` the running
                                       containers as `:warm` baselines
                                       (~sub-2s restore on subsequent runs)
  run      <project> <stage>           up (from :warm if present, else cold) →
                                       exec runner.sh <stage> →
                                       collect /results → down -v
  exec     <project> -- <cmd>...       up if not running → exec arbitrary cmd
  destroy  <project>                   compose down -v
  ps       <project>                   compose ps
  logs     <project> [svc]             compose logs

Per-run isolation: `RUN_ID` is generated per run and exported into the
compose env so each run gets a unique `./out/<project>/<run-id>/` results
dir. Concurrent runs against the same project would still collide on
container_name though — see README for how to run in parallel (separate
project names via `-p`).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
import uuid

# ─────────────────────── project registry ───────────────────────

PROJECTS: dict[str, dict] = {
    "eshop": {
        "compose":     "compose/eshop.yml",
        "service":     "eshop",         # the workload service name (vs. sqlserver / postgres / redis)
        "image":       "ai-harness/eshop:latest",
        "warm_image":  "ai-harness/eshop:warm",
        "build_ctx":   "images/eshop",
        "secrets":     ["MSSQL_SA_PASSWORD"],
        "stages":      ["build", "test", "all"],
    },
    "medplum": {
        "compose":     "compose/medplum.yml",
        "service":     "medplum",
        "image":       "ai-harness/medplum:latest",
        "warm_image":  "ai-harness/medplum:warm",
        "build_ctx":   "images/medplum",
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


def _compose(project: dict, *args: str, env: dict | None = None,
             capture: bool = False) -> subprocess.CompletedProcess:
    """Run `docker compose -f compose/<project>.yml <args>` with proper env."""
    base = _require_env(project)
    base.setdefault("RUN_ID", os.environ.get("RUN_ID", "default"))
    # RESULTS_DIR is required by compose's volume binds. For ops that don't
    # actually start the workload container (down, ps, logs) the path still
    # has to be set so compose can resolve the variable — a dummy default
    # keeps those commands from failing the interpolation check. `cmd_run`
    # overrides this with the real per-run path.
    base.setdefault("RESULTS_DIR", str(REPO_ROOT / "out" / "_unused"))
    # SOURCE_DIR / MEMORY_DIR default to /dev/null so the optional mounts
    # silently no-op when the caller doesn't request them. runner.sh
    # tests `[ -d ... ]` before acting on either.
    base.setdefault("SOURCE_DIR", "/dev/null")
    base.setdefault("MEMORY_DIR", "/dev/null")
    base.setdefault("TASK_ID",    "")
    if env:
        base.update(env)
    return _check("docker", "compose", "-f", project["compose"], *args,
                  env=base, capture=capture)


def _image_exists(tag: str) -> bool:
    r = _check("docker", "image", "inspect", tag, capture=True)
    return r.returncode == 0


def _now_ts() -> int:
    return int(time.time())


# ─────────────────────── subcommands ───────────────────────


def cmd_build(args: argparse.Namespace) -> int:
    p = _project(args.project)
    print(f"⟳ docker build {p['image']} (context: {p['build_ctx']})", file=sys.stderr)
    return _check("docker", "build", "-t", p["image"], "-f",
                  f"{p['build_ctx']}/Dockerfile", p["build_ctx"]).returncode


def cmd_warmup(args: argparse.Namespace) -> int:
    """Bring up cold, run prep (restore + build + migrate), commit warm baselines.

    Each container in the compose file that we want to snapshot gets its
    own committed tag. The eshop profile has sqlserver + eshop; we commit
    both so the next `run` doesn't have to re-migrate or re-restore.

    Subsequent `run` calls auto-pick `:warm` if it exists.
    """
    p = _project(args.project)
    print(f"⟳ warmup: cold-up {args.project}", file=sys.stderr)
    rc = _compose(p, "up", "-d", "--wait").returncode
    if rc != 0:
        _compose(p, "logs", "--tail=30")
        return rc

    print(f"⟳ warmup: running prep stages (restore + build + migrate)", file=sys.stderr)
    rc = _compose(p, "exec", "-T", p["service"],
                  "/usr/local/bin/runner.sh", "warmup").returncode
    if rc != 0:
        print(f"✗ warmup stages failed (exit {rc}); leaving containers up for inspection", file=sys.stderr)
        return rc

    # Snapshot each running container as :warm
    ps = _compose(p, "ps", "--format", "json", capture=True)
    if ps.returncode != 0:
        sys.exit(f"compose ps failed: {ps.stderr}")
    services = [json.loads(line) for line in ps.stdout.strip().split("\n") if line.strip()]
    for svc in services:
        name      = svc["Name"]                                    # container name
        svc_name  = svc.get("Service", "")                         # service in compose file
        warm_tag  = f"ai-harness/{args.project}-{svc_name}:warm"
        print(f"⟳ committing {name} → {warm_tag}", file=sys.stderr)
        _must("docker", "commit", name, warm_tag)

    print(f"⟳ warmup: down -v", file=sys.stderr)
    _compose(p, "down", "-v")
    print(f"✓ warmup complete. Future `run` picks up :warm baselines.", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """End-to-end: up → exec runner.sh <stage> → collect /results → down -v.

    Returns the runner.sh exit code so callers can route on it. Result
    artifacts land in ./out/<project>/<run-id>/.
    """
    p   = _project(args.project)
    rid = args.run_id or f"r{_now_ts()}-{uuid.uuid4().hex[:6]}"
    env: dict = {"RUN_ID": rid}

    # If `--snapshot warm` is requested and the warm tags exist, route the
    # compose to use them via the image-env-var indirection in compose/*.yml.
    if args.snapshot == "warm":
        warm_app = f"ai-harness/{args.project}-{p['service']}:warm"
        if _image_exists(warm_app):
            env_var = f"{args.project.upper()}_IMAGE"
            env[env_var] = warm_app
            print(f"⟳ using warm baseline: {warm_app}", file=sys.stderr)
            # SQL Server / Postgres warm tag if it exists
            for svc in ("sqlserver", "postgres"):
                warm_db = f"ai-harness/{args.project}-{svc}:warm"
                if _image_exists(warm_db):
                    env[f"{svc.upper()}_IMAGE"] = warm_db
                    print(f"⟳ using warm baseline: {warm_db}", file=sys.stderr)
        else:
            print(f"⚠ requested --snapshot warm but {warm_app} not found; falling back to cold", file=sys.stderr)

    # Task-scoped vs ephemeral output paths.
    # With --task: out/<project>/<task_id>/<run_id>/ + persistent memory dir.
    # Without:     out/<project>/<run_id>/ — single-shot, no memory.
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

    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o777)
    env["RESULTS_DIR"] = str(out_dir.resolve())

    # Optional source override — bind-mounted read-only into the sandbox.
    # runner.sh copies it onto /workspace/repo before stages.
    if args.source:
        src = pathlib.Path(args.source).resolve()
        if not src.is_dir():
            sys.exit(f"--source must be a directory: {src}")
        env["SOURCE_DIR"] = str(src)
        print(f"⟳ source={src}  (replaces baked /workspace/repo)", file=sys.stderr)

    print(f"⟳ run_id={rid}  out={out_dir.relative_to(REPO_ROOT)}", file=sys.stderr)
    started_at = _now_ts()

    # Bring up + wait healthy
    print(f"⟳ compose up", file=sys.stderr)
    if _compose(p, "up", "-d", "--wait", env=env).returncode != 0:
        _compose(p, "logs", "--tail=40", env=env)
        _compose(p, "down", "-v", env=env)
        return 1

    # Exec the requested stage
    print(f"⟳ exec runner.sh {args.stage}", file=sys.stderr)
    exec_rc = _compose(p, "exec", "-T", p["service"],
                       "/usr/local/bin/runner.sh", args.stage, env=env).returncode

    # Tear down regardless (clean state between runs is mandatory).
    print(f"⟳ compose down -v", file=sys.stderr)
    _compose(p, "down", "-v", env=env)

    finished_at = _now_ts()

    # Append to the task's iterations log (substrate memory layer).
    if task_dir is not None:
        iteration_record = {
            "run_id":       rid,
            "task_id":      args.task,
            "stage":        args.stage,
            "snapshot":     args.snapshot,
            "source_dir":   str(pathlib.Path(args.source).resolve()) if args.source else None,
            "started_at":   started_at,
            "finished_at":  finished_at,
            "duration_s":   finished_at - started_at,
            "exit_code":    exec_rc,
            "result_path":  str(out_dir.relative_to(REPO_ROOT)),
        }
        with open(task_dir / "iterations.jsonl", "a") as f:
            f.write(json.dumps(iteration_record) + "\n")

    # Surface the result.json on stdout for downstream consumers.
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
    # Ensure the stack is up
    if _compose(p, "ps", "-q", p["service"], capture=True).stdout.strip() == "":
        if _compose(p, "up", "-d", "--wait").returncode != 0:
            return 1
    cmd = args.cmd or ["bash"]
    return _compose(p, "exec", p["service"], *cmd).returncode


def cmd_destroy(args: argparse.Namespace) -> int:
    p = _project(args.project)
    return _compose(p, "down", "-v").returncode


def cmd_ps(args: argparse.Namespace) -> int:
    return _compose(_project(args.project), "ps").returncode


def cmd_logs(args: argparse.Namespace) -> int:
    p = _project(args.project)
    extra = [args.service] if args.service else []
    return _compose(p, "logs", "--tail=200", *extra).returncode


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
                # Skip ephemeral run dirs (those have a result.json but no iterations.jsonl).
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
        print(f"memory: {memory_dir.relative_to(REPO_ROOT)} ({len(files)} files: {', '.join(files[:5])}{'…' if len(files) > 5 else ''})")
    print()
    print(f"{'#':>3}  {'RUN_ID':<26} {'STAGE':<7} {'EXIT':>4}  {'DUR':>4}s  {'SOURCE'}")
    for i, it in enumerate(iterations, 1):
        src = it.get("source_dir") or "—"
        if src != "—":
            src = "…/" + pathlib.Path(src).name
        print(f"{i:>3}  {it['run_id']:<26} {it.get('stage','?'):<7} "
              f"{it.get('exit_code','?'):>4}  {it.get('duration_s','?'):>4}s  {src}")
    return 0


def cmd_tasks_memory(args: argparse.Namespace) -> int:
    """Print the path of a task's memory dir (or open a file inside it)."""
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

    b = sub.add_parser("build",   help="docker build the sandbox image");   add_project_arg(b); b.set_defaults(func=cmd_build)
    w = sub.add_parser("warmup",  help="cold-start once, then docker commit warm baselines"); add_project_arg(w); w.set_defaults(func=cmd_warmup)

    r = sub.add_parser("run",     help="full lifecycle: up → exec runner.sh <stage> → results → down")
    add_project_arg(r)
    r.add_argument("stage",       choices=["build", "test", "all"], help="runner.sh subcommand")
    r.add_argument("--snapshot",  choices=["warm", "cold"], default="warm",
                   help="`warm` uses :warm-tagged baseline if present; `cold` always rebuilds from :latest")
    r.add_argument("--run-id",    help="override the generated run id")
    r.add_argument("--task",      metavar="ID",
                   help="task identifier — enables substrate memory (persistent iteration log + writable /memory mount). "
                        "Without it, this run is ephemeral.")
    r.add_argument("--source",    metavar="DIR",
                   help="host directory to mount read-only as the source. "
                        "runner.sh copies it onto /workspace/repo before stages — lets the agent edit on the host "
                        "between iterations without rebuilding the image.")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("exec",    help="exec into the running sandbox (ad-hoc)")
    add_project_arg(e)
    e.add_argument("cmd",         nargs=argparse.REMAINDER, help="command + args after --")
    e.set_defaults(func=cmd_exec)

    d = sub.add_parser("destroy", help="compose down -v");            add_project_arg(d); d.set_defaults(func=cmd_destroy)
    ps = sub.add_parser("ps",     help="compose ps");                 add_project_arg(ps); ps.set_defaults(func=cmd_ps)
    lg = sub.add_parser("logs",   help="compose logs");               add_project_arg(lg); lg.add_argument("service", nargs="?"); lg.set_defaults(func=cmd_logs)

    # ─── task memory subcommand group ───
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
