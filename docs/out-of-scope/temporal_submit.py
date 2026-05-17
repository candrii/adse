"""Submit a task / workflow to the Temporal-backed agent runtime.

Examples:
  # one-shot exec in a fresh sandbox
  python3 -m harness.temporal_submit single \\
      --image python:3.12-slim \\
      --command 'python -c "print(2+2)"' \\
      --wait

  # fan-out: run 10 sandboxes in parallel
  python3 -m harness.temporal_submit fanout \\
      --image python:3.12-slim \\
      --command 'sleep $((RANDOM % 5)) && echo done' \\
      --count 10 \\
      --wait

  # eshop build/test pipeline (requires the prebake image to be built)
  python3 -m harness.temporal_submit buildtest --project eshop --wait
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

from temporalio.client import Client

from .temporal_activities import SandboxTask
from .temporal_workflows  import SingleTask, BuildTestPipeline, FanOut


TASK_QUEUE = "agent-runtime"


PROJECT_IMAGES = {
    "eshop":   "ai-harness/eshop:latest",
    "medplum": "ai-harness/medplum:latest",
}


async def _client(address: str) -> Client:
    return await Client.connect(address)


async def cmd_single(args: argparse.Namespace) -> int:
    client = await _client(args.temporal)
    task = SandboxTask(
        image=     args.image,
        command=   args.command,
        timeout_s= args.timeout,
        resource_limits={"cpu": args.cpu, "memory": args.memory},
    )
    wf_id = args.id or f"single-{uuid.uuid4().hex[:8]}"
    print(f"⟳ starting workflow {wf_id}", file=sys.stderr)
    handle = await client.start_workflow(
        SingleTask.run, task,
        id=         wf_id,
        task_queue= TASK_QUEUE,
    )
    print(json.dumps({"workflow_id": wf_id, "status": "started",
                      "ui": f"http://localhost:8233/namespaces/default/workflows/{wf_id}"}))
    if args.wait:
        print("⟳ waiting for result …", file=sys.stderr)
        result = await handle.result()
        print(json.dumps(result.__dict__, indent=2, default=str))
        return result.exit_code if result.status == "ok" else (1 if result.status == "fail" else 60)
    return 0


async def cmd_fanout(args: argparse.Namespace) -> int:
    client = await _client(args.temporal)
    tasks = [
        SandboxTask(image=args.image, command=args.command, timeout_s=args.timeout,
                    resource_limits={"cpu": args.cpu, "memory": args.memory},
                    metadata={"fanout_index": str(i)})
        for i in range(args.count)
    ]
    wf_id = args.id or f"fanout-{uuid.uuid4().hex[:8]}"
    print(f"⟳ starting fan-out workflow {wf_id} (count={args.count})", file=sys.stderr)
    handle = await client.start_workflow(
        FanOut.run, tasks,
        id=         wf_id,
        task_queue= TASK_QUEUE,
    )
    print(json.dumps({"workflow_id": wf_id, "count": args.count, "status": "started",
                      "ui": f"http://localhost:8233/namespaces/default/workflows/{wf_id}"}))
    if args.wait:
        print("⟳ waiting for all branches …", file=sys.stderr)
        results = await handle.result()
        summary = {
            "total": len(results),
            "ok":    sum(1 for r in results if r.status == "ok"),
            "fail":  sum(1 for r in results if r.status == "fail"),
            "error": sum(1 for r in results if r.status == "error"),
        }
        print(json.dumps(summary, indent=2))
    return 0


async def cmd_buildtest(args: argparse.Namespace) -> int:
    if args.project not in PROJECT_IMAGES:
        sys.exit(f"unknown project: {args.project}")
    client = await _client(args.temporal)
    env: dict[str, str] = {}
    if args.project == "eshop":
        env = {"MSSQL_SA_PASSWORD": os.environ.get("MSSQL_SA_PASSWORD", ""),
               "SQL_HOST":          "sqlserver"}
    else:
        env = {"POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
               "PG_HOST":           "postgres",
               "REDIS_HOST":        "redis"}
    wf_id = args.id or f"{args.project}-buildtest-{uuid.uuid4().hex[:8]}"
    print(f"⟳ starting buildtest workflow {wf_id}", file=sys.stderr)
    handle = await client.start_workflow(
        BuildTestPipeline.run, args=[PROJECT_IMAGES[args.project], env],
        id=         wf_id,
        task_queue= TASK_QUEUE,
    )
    print(json.dumps({"workflow_id": wf_id, "project": args.project, "status": "started",
                      "ui": f"http://localhost:8233/namespaces/default/workflows/{wf_id}"}))
    if args.wait:
        result = await handle.result()
        print(json.dumps(result, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--temporal", default=os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
                   help="Temporal frontend address (host:port)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("single", help="One sandbox, one command")
    s.add_argument("--image",   required=True)
    s.add_argument("--command", required=True)
    s.add_argument("--timeout", type=int, default=600)
    s.add_argument("--cpu",     default="500m")
    s.add_argument("--memory",  default="512Mi")
    s.add_argument("--id",      help="Override workflow id")
    s.add_argument("--wait",    action="store_true", help="Block until done; print result")
    s.set_defaults(func=cmd_single)

    f = sub.add_parser("fanout", help="N parallel sandboxes running the same command")
    f.add_argument("--image",   required=True)
    f.add_argument("--command", required=True)
    f.add_argument("--count",   type=int, default=5)
    f.add_argument("--timeout", type=int, default=300)
    f.add_argument("--cpu",     default="250m")
    f.add_argument("--memory",  default="256Mi")
    f.add_argument("--id",      help="Override workflow id")
    f.add_argument("--wait",    action="store_true")
    f.set_defaults(func=cmd_fanout)

    b = sub.add_parser("buildtest", help="eshop / medplum build → test pipeline")
    b.add_argument("--project", required=True, choices=list(PROJECT_IMAGES))
    b.add_argument("--id",      help="Override workflow id")
    b.add_argument("--wait",    action="store_true")
    b.set_defaults(func=cmd_buildtest)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
