# Out of scope — kept for reference

These files were built while exploring the broader architecture but pulled
back from the active deliverable. The brief explicitly says:

> "we're not asking you to build an AI harness. We're asking you to build
> the infrastructure that an AI harness runs inside."

Everything here is the *harness / orchestration* side — a layer above the
sandbox runtime, which is consumer territory, not infrastructure
territory. Kept as reference for what an integration would look like; not
in the demo path, not in the compose stack, not in any image.

## What's here

### `manager/` — Runtime Manager service

FastAPI service that wrapped OpenSandbox with higher-level operations:
build / migrate / app_start / wait_healthy / tests / reset / warm-pool
lease / sidecar attach. Per-project handlers (`projects.py`) carried
project-specific knowledge.

**Why pulled back:** added an orchestration tier on top of OpenSandbox
without clearly articulating what it bought over `docker compose` directly.
Reviewer's critique: "reinventing what Docker/Podman/Kubernetes already
expose natively." Fair point.

### `temporal_*.py` — Temporal workflows + activities + worker + submit CLI

Coordination layer using Temporal for durable workflows:
- `temporal_workflows.py` — SingleTask / BuildTestPipeline / FanOut
- `temporal_activities.py` — `run_in_sandbox` bridge to OpenSandbox
- `temporal_worker.py` — long-running Temporal worker
- `temporal_submit.py` — submit CLI (`single` / `fanout` / `buildtest`)
- `temporal_activities_agentic.py` — Anthropic-SDK-direct LLM loop
- `temporal_activities_langgraph.py` — LangGraph variant of same

**Why pulled back:** Temporal is for durable, long-running workflows with
replay/retry/signals. Sandbox provisioning is short-lived request/response.
A queue or k8s Jobs handles the actual shape with a fraction of the
operational surface. Adding Temporal also adds a Postgres + a worker fleet
before the first container starts. Wrong tool for the job.

### `mcp_server.py` — Model Context Protocol adapter

Exposed sandbox operations to MCP-aware agents (Claude Code, Cursor) as
tools (`bash`, `read_file`, `write_file`, `apply_patch`, `run_pipeline`).

**Why pulled back:** also harness-side. The sandbox doesn't need to know
about MCP; an MCP-aware agent on the host can integrate however it wants.

### `images/opensandbox-server/`, `sandbox.toml`, `sandbox.gvisor.toml`

OpenSandbox (Alibaba) as the sandbox lifecycle API.

**Why pulled back:** the redesign uses the Docker SDK directly. Reviewer
flagged a supply-chain question for healthcare (OpenSandbox is an
Alibaba project with limited Western adoption + mostly Chinese
documentation). On top of that, OpenSandbox reinvents what Docker
already exposes natively. The cost — defending the choice to a healthcare
panel — outweighed the benefit of its REST surface for our use case.

### `images/temporal-worker/`, `images/runtime-manager/`

Container images that bundled the orchestration code. Drop with the code.

---

## How this maps to the active code

The active deliverable in the repo root now provides the same outcomes via
simpler primitives:

| What this layer did | Now done by |
|---|---|
| Submit a build/test task to a queue | `make eshop` / `make medplum` (CLI → `docker compose`) |
| Spawn a sandbox container | `docker compose -f compose/<project>.yml up` |
| Exec a command inside | `docker compose exec <svc> <cmd>` (wrapped by the CLI) |
| Capture results | volume mount → `./out/<run-id>/result.json` |
| Reset state between runs | `docker commit` of warm baseline + `docker compose down -v` |
| Multi-tenant runs | per-project compose profiles + unique network namespace per `run-id` |
| Gate egress | `egress-proxy/` (unchanged) |
| Kernel isolation | `runtime: runsc` flag in compose service (gVisor scripts unchanged) |

The active code is ~10× smaller and answers the brief's actual questions:
DB conflict (separate compose profiles), snapshot reset, headless config,
output capture, resource limits, isolation primitives.
