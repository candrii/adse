# Temporal worker for the ai-harness orchestration layer.
#
# This image is the "headless agent runtime" — N replicas of it run inside
# compose, each polling Temporal for activities on the `agent-runtime` task
# queue and dispatching to OpenSandbox.
#
# It deliberately doesn't carry workload toolchains (no .NET, no Node) —
# those live in the project sandbox images (images/eshop, images/medplum).
# The worker is pure orchestration glue.

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends tini && \
    rm -rf /var/lib/apt/lists/*

# Only the worker deps — no mcp/SDKs. Keeps the image small.
RUN pip install --no-cache-dir \
        "requests>=2.32,<3" \
        "temporalio>=1.7,<2"

COPY harness/__init__.py             /app/harness/__init__.py
COPY harness/sandbox.py              /app/harness/sandbox.py
COPY harness/temporal_activities.py  /app/harness/temporal_activities.py
COPY harness/temporal_workflows.py   /app/harness/temporal_workflows.py
COPY harness/temporal_worker.py      /app/harness/temporal_worker.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    AI_HARNESS_INSIDE_COMPOSE=1

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "harness.temporal_worker"]
