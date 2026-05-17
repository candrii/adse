"""Temporal worker entry point.

Run N replicas of this in compose (`docker compose up --scale agent-worker=N`).
Each replica is a long-lived process that polls Temporal for activity tasks
on the `agent-runtime` task queue, and for workflows it knows how to execute.

Concurrency knobs (via env):
  MAX_CONCURRENT_ACTIVITIES — max activities one worker handles at once.
  MAX_CONCURRENT_WORKFLOWS  — max workflow runs one worker drives at once.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from .temporal_activities import run_in_sandbox
from .temporal_workflows  import SingleTask, BuildTestPipeline, FanOut


TASK_QUEUE = "agent-runtime"


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger("worker")

    address = os.environ.get("TEMPORAL_ADDRESS", "temporal:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    identity = f"{socket.gethostname()}-{os.getpid()}"

    log.info("connecting to temporal address=%s namespace=%s", address, namespace)
    client = await Client.connect(address, namespace=namespace, identity=identity)

    worker = Worker(
        client,
        task_queue=                  TASK_QUEUE,
        workflows=                   [SingleTask, BuildTestPipeline, FanOut],
        activities=                  [run_in_sandbox],
        max_concurrent_activities=   int(os.environ.get("MAX_CONCURRENT_ACTIVITIES", "4")),
        max_concurrent_workflow_tasks=int(os.environ.get("MAX_CONCURRENT_WORKFLOWS", "4")),
    )

    log.info("worker ready identity=%s task_queue=%s", identity, TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
