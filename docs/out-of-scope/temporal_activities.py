"""Temporal activities — the bridge from workflow code to OpenSandbox.

An activity is the unit of work Temporal retries, times out, and heartbeats
against. It MUST be deterministic-replayable from the workflow's perspective,
which is why I/O lives here, not in the workflow file.

Activities are registered with the Temporal worker (`temporal_worker.py`) and
referenced by name from workflows (`temporal_workflows.py`).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from temporalio import activity

from .sandbox import SandboxClient


# ─────────────────────────── data contracts ───────────────────────────


@dataclass
class SandboxTask:
    """What a workflow asks an activity to run."""
    image:           str
    command:         str
    timeout_s:       int = 600
    env:             dict[str, str] = field(default_factory=dict)
    resource_limits: dict[str, str] = field(default_factory=lambda: {"cpu": "500m", "memory": "512Mi"})
    metadata:        dict[str, str] = field(default_factory=dict)


@dataclass
class SandboxResult:
    """What the activity returns. Kept compact — Temporal serializes this
    into the workflow's event history, so multi-MB stdout would bloat the DB."""
    sandbox_id: str
    exit_code:  int
    status:     str          # "ok" | "fail" | "error"
    stdout:     str          # truncated to ~32 KB
    stderr:     str          # truncated to ~8 KB
    duration_s: float


# ─────────────────────────── activity impls ───────────────────────────


def _client() -> SandboxClient:
    return SandboxClient(
        os.environ.get("OPENSANDBOX_API", "http://opensandbox:8080"),
        os.environ.get("OPEN_SANDBOX_API_KEY"),
    )


@activity.defn(name="run_in_sandbox")
async def run_in_sandbox(task: SandboxTask) -> SandboxResult:
    """Create a sandbox, run one command, return result, tear down.

    Why a single activity does the whole lifecycle (rather than 3 separate
    activities for create/exec/delete): Temporal's retry semantics work
    cleanest when the unit-of-work owns its setup + teardown. If the worker
    crashes mid-exec, Temporal retries the *whole* activity on another
    worker, and the original sandbox eventually times out and is garbage-
    collected by OpenSandbox's TTL. No orphans.
    """
    client = _client()
    started = time.time()
    activity.logger.info("creating sandbox image=%s", task.image)

    sbx = client.create(
        image=           task.image,
        env=             task.env,
        timeout_s=       task.timeout_s,
        resource_limits= task.resource_limits,
        metadata=        {"temporal_workflow_id": activity.info().workflow_id,
                          "temporal_activity_id": activity.info().activity_id,
                          **task.metadata},
    )
    activity.heartbeat({"stage": "sandbox_created", "sandbox_id": sbx["id"]})

    try:
        activity.logger.info("exec'ing in sandbox=%s command=%s", sbx["id"], task.command[:80])
        result = client.exec_capture(sbx, task.command, timeout_s=task.timeout_s)
        return SandboxResult(
            sandbox_id= sbx["id"],
            exit_code=  result["exit_code"],
            status=     "ok" if result["exit_code"] == 0 else "fail",
            stdout=     result["stdout"][-32_000:],
            stderr=     result["stderr"][-8_000:],
            duration_s= time.time() - started,
        )
    except Exception as exc:
        return SandboxResult(
            sandbox_id= sbx["id"],
            exit_code=  -1,
            status=     "error",
            stdout=     "",
            stderr=     f"{type(exc).__name__}: {exc}",
            duration_s= time.time() - started,
        )
    finally:
        # Best-effort teardown. OpenSandbox's own TTL is the safety net.
        try:
            client.delete(sbx["id"])
        except Exception as exc:
            activity.logger.warning("teardown failed for %s: %s", sbx["id"], exc)
