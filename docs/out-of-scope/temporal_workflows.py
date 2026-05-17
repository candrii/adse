"""Temporal workflows — durable, retryable agent task orchestration.

Workflow code is sandboxed by Temporal's runtime: no I/O, no random, no clock
reads. All side effects go through activities. The `unsafe.imports_passed_through`
context lets us import dataclasses without tripping that sandbox.

Three workflows here, each demonstrating a different agent pattern:

  - SingleTask     : one-shot exec in a sandbox (smallest unit)
  - BuildTestPipeline: classic CI shape (build → test → report)
  - FanOut         : run the same task across N inputs in parallel — the
                     "agent does 50 things at once" pattern
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .temporal_activities import run_in_sandbox, SandboxTask, SandboxResult


# ─────────────────────────── workflows ───────────────────────────


@workflow.defn(name="SingleTask")
class SingleTask:
    """One sandbox run with retry. The minimum useful workflow."""

    @workflow.run
    async def run(self, task: SandboxTask) -> SandboxResult:
        return await workflow.execute_activity(
            run_in_sandbox,
            task,
            start_to_close_timeout= timedelta(seconds=task.timeout_s + 60),
            heartbeat_timeout=      timedelta(seconds=120),
            retry_policy= RetryPolicy(
                initial_interval=  timedelta(seconds=2),
                maximum_interval=  timedelta(minutes=1),
                maximum_attempts=  2,   # one retry, then give up
            ),
        )


@workflow.defn(name="BuildTestPipeline")
class BuildTestPipeline:
    """CI shape: build → test → (report). Each stage runs in a fresh sandbox.

    If build fails: don't run tests, return immediately with the build result.
    This is what an agent harness wants when wrapping the eshop/medplum
    runner.sh — the orchestrator decides which stages to skip based on prior
    outcomes, not the sandbox itself.
    """

    @workflow.run
    async def run(self, project_image: str, env: dict[str, str]) -> dict:
        build = await workflow.execute_activity(
            run_in_sandbox,
            SandboxTask(
                image=     project_image,
                command=   "/usr/local/bin/runner.sh build",
                timeout_s= 900,
                env=       env,
                resource_limits={"cpu": "2", "memory": "4Gi"},
                metadata=  {"stage": "build"},
            ),
            start_to_close_timeout= timedelta(minutes=20),
            heartbeat_timeout=      timedelta(seconds=120),
            retry_policy=           RetryPolicy(
                initial_interval= timedelta(seconds=2),
                backoff_coefficient=2.0,
                maximum_interval= timedelta(seconds=20),
                maximum_attempts= 4,   # WSL2 port-forwarder needs slack
            ),
        )
        if build.exit_code != 0:
            return {"status": "build_failed", "build": build.__dict__}

        test = await workflow.execute_activity(
            run_in_sandbox,
            SandboxTask(
                image=     project_image,
                command=   "/usr/local/bin/runner.sh test",
                timeout_s= 1200,
                env=       env,
                resource_limits={"cpu": "2", "memory": "4Gi"},
                metadata=  {"stage": "test"},
            ),
            start_to_close_timeout= timedelta(minutes=25),
            heartbeat_timeout=      timedelta(seconds=120),
            retry_policy=           RetryPolicy(
                initial_interval= timedelta(seconds=2),
                backoff_coefficient=2.0,
                maximum_interval= timedelta(seconds=20),
                maximum_attempts= 4,   # WSL2 port-forwarder needs slack
            ),
        )
        return {
            "status": "ok" if test.exit_code == 0 else "test_failed",
            "build":  build.__dict__,
            "test":   test.__dict__,
        }


@workflow.defn(name="FanOut")
class FanOut:
    """Run the same template task across N inputs concurrently.

    The 'scalable parallel agents' demo: kick this off with 20 commands and
    each lands on a worker, gets its own sandbox, runs in parallel. The
    workflow waits for all of them and returns the aggregate.
    """

    @workflow.run
    async def run(self, tasks: list[SandboxTask]) -> list[SandboxResult]:
        # asyncio.gather() truly parallelizes: each branch becomes its own
        # activity task, the Temporal scheduler hands them to workers in
        # parallel. Sequential `await` would have serialized them.
        return list(await asyncio.gather(*[
            workflow.execute_activity(
                run_in_sandbox, t,
                start_to_close_timeout= timedelta(seconds=t.timeout_s + 60),
                heartbeat_timeout=      timedelta(seconds=120),
                retry_policy=           RetryPolicy(
                initial_interval= timedelta(seconds=2),
                backoff_coefficient=2.0,
                maximum_interval= timedelta(seconds=20),
                maximum_attempts= 4,   # WSL2 port-forwarder needs slack
            ),
            )
            for t in tasks
        ]))


# Agent-loop workflows (AgenticTicket, LangGraphTicket) live in
# docs/out-of-scope/ — they are *consumers* of this runtime, not part of it.
# The brief explicitly excludes "build the AI harness" from scope.
