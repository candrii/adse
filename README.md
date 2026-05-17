# Headless Agent Sandbox

Docker-based sandbox runtime for AI coding agents. Per-project Compose
profiles, snapshot-based reset, headless config, structured output, gVisor
opt-in. This is the **substrate** — not the harness, not the orchestrator.

> "we're not asking you to build an AI harness. We're asking you to build
> the infrastructure that an AI harness runs inside."
> — task brief

## What's here

```
ai-harness/
├── compose/
│   ├── eshop.yml          # SQL Server + eShop sandbox, isolated network
│   └── medplum.yml        # Postgres + Redis + Medplum sandbox, isolated network
├── images/
│   ├── eshop/             # .NET 10 SDK + warm NuGet + runner.sh
│   └── medplum/           # Node 20 + pnpm + runner.sh (scaffold)
├── harness/
│   └── sandbox.py         # thin CLI over `docker compose` + `docker commit`
├── egress-proxy/          # tinyproxy + FQDN allowlist (network boundary)
├── scripts/
│   ├── install-gvisor.sh  # host-side gVisor install (kernel isolation)
│   └── verify-gvisor.sh
├── Makefile               # ergonomic wrappers
└── docs/
    ├── *.pdf, TASK_TEXT.md    # the brief
    └── out-of-scope/          # orchestration explorations pulled back per the brief
```

There is no top-level `docker-compose.yml`. Per-project profiles are the
unit of isolation.

## Quickstart

```bash
make bootstrap                      # write .env (DB passwords)
make build PROJECT=eshop            # docker build the sandbox image
make warmup PROJECT=eshop           # cold-start once, commit :warm baselines
make run-test PROJECT=eshop         # restore from :warm, run tests, write result.json
cat out/eshop/<run-id>/result.json
```

Output lands in `./out/<project>/<run-id>/` — a JSON result, per-stage
logs, and any test artifacts the workload emitted.

## The agent loop — task-scoped iteration

A headless agent rarely runs once. It tries a change, reads results,
reasons, tries another change, and repeats until the task is done. The
substrate provides this loop:

```bash
# Agent picks a task identifier and (optionally) a host source directory
# to mount. Edits source on the host between runs — no image rebuild.

make run-task PROJECT=eshop TASK=fix-health-endpoint SOURCE=~/work/eshop
# ↑ first iteration: copies host source into sandbox, builds, tests,
#   appends to out/eshop/fix-health-endpoint/iterations.jsonl

# Agent reads results, reasons, makes a fix:
vim ~/work/eshop/src/Web/Program.cs
echo "iter-1: removed early registration of MapHealthChecks" \
  >> $(make tasks-memory TASK=fix-health-endpoint)/notes.md

# Next iteration — same task, persistent memory:
make run-task PROJECT=eshop TASK=fix-health-endpoint SOURCE=~/work/eshop

# Inspect:
make tasks-ls                                    # all tasks across projects
make tasks-show TASK=fix-health-endpoint         # iteration history
```

What's provided by the substrate (not the agent):

| Mechanism | Purpose | Lifecycle |
|---|---|---|
| `--task <id>` flag | Names a task; scopes results | per agent reasoning loop |
| `out/<project>/<task>/iterations.jsonl` | Append-only iteration log | persists across `down -v` |
| `out/<project>/<task>/<run_id>/` | Full per-iteration artifacts (result.json, logs, .trx) | persists |
| `out/<project>/<task>/memory/` | Agent-writable scratchpad, mounted as `/memory` inside sandbox | persists |
| `--source <dir>` flag | Host source override; bind-mounted read-only at `/workspace/src` | per iteration |

What's **not** provided:

- The agent's LLM context, prompts, reasoning. That's the harness's job
  — Claude memory tool, LangGraph checkpointer, whatever it uses.
- Cross-task memory. Each task is a namespace; tasks are isolated.

### Substrate result contract

Each iteration produces a result.json that surfaces the task identifier
and memory mount, so the agent can introspect from inside the sandbox:

```json
{
  "task_id":         "fix-health-endpoint",
  "run_id":          "r1778972422-d49319",
  "project":         "eshop",
  "stage":           "test",
  "status":          "pass",
  "exit_code":       0,
  "memory_dir":      "/memory",
  "source_override": true,
  "duration_s":      54,
  "stages":          { ... },
  "artifacts":       [ ... ]
}
```

## How it answers the brief

| Brief topic | This repo's answer |
|---|---|
| **DB conflict (SQL Server vs Postgres + Redis)** | Per-project Compose profiles. `eshop.yml` brings only SQL Server; `medplum.yml` brings Postgres + Redis. Unique network namespace per project (`ai-harness-eshop-net` / `ai-harness-medplum-net`) — zero cross-talk. |
| **Clean state strategy** | `docker commit` of warm baselines after one cold prep run. `sandbox warmup eshop` does the slow stuff (NuGet restore, dotnet build, EF migrate) once, then commits sqlserver + eshop as `:warm`. Subsequent `sandbox run` restores from those — sub-2s. `down -v` between runs wipes any in-run state (tmpfs data volumes). |
| **Headless execution** | `ACCEPT_EULA=Y` for SQL Server in compose env; `DOTNET_NOLOGO`, `DOTNET_CLI_TELEMETRY_OPTOUT` in image. No CLI prompts; everything passes through env. |
| **Output capture as structured data** | `runner.sh` writes `/results/result.json` with stable shape: `{run_id, project, stage, status, exit_code, duration_s, stages: {name: {ok, duration_s, log}}, artifacts: [...]}`. Bind-mounted to `./out/<project>/<run-id>/` on host. Stable exit codes: 0=pass, 10=build_fail, 20=test_fail, 30=migrate_fail, 40=health_fail, 70=patch_fail, 80=checkout_fail. |
| **Isolation model** | (1) `cap_drop` of the dangerous Linux caps (NET_ADMIN, NET_RAW, SYS_ADMIN, SYS_MODULE, SYS_PTRACE, SYS_TIME, MKNOD, AUDIT_WRITE, SYS_TTY_CONFIG) + `no-new-privileges:true` on the workload service. (2) Per-project network namespace — `ai-harness-eshop-net` and `ai-harness-medplum-net` are unrelated. (3) Egress allowlist: `egress-proxy` runs on each project's network with an FQDN whitelist (github / nuget / npm / mcr / docker hub / cdnjs). **Caveat:** this is an advisory boundary — code that respects `HTTP_PROXY` env vars routes through it, but the container netns has direct outbound. For enforced egress we'd need netns iptables or k8s NetworkPolicy; see "Egress allowlist (caveat)" below. (4) gVisor opt-in path: `runtime: runsc` is a one-line config change once `make install-gvisor` is run on the host. |
| **Secrets handling** | DB passwords in `.env` (.gitignored). Compose substitutes at startup; runtime containers see them as env vars. **Never** passed on command line, never written to images, never logged. Production path: swap `.env` for a secret-mount via Vault / 1Password / AWS Secrets Manager — `compose/*.yml` already references env-style variables. |
| **Resource limits & multi-tenancy** | Per-service `deploy.resources.limits` in each compose profile (eshop sqlserver: 4G/2cpu; eshop app: 4G/2cpu; medplum: 4G/2cpu). For parallel runs, pass `-p <unique-name>` to compose via the CLI's `RUN_ID` mechanism — the network is created per project name. |
| **Build cache** | NuGet warm cache baked into the eshop image at build time (one `dotnet restore` during image build). Same pattern for medplum (`pnpm install --frozen-lockfile` at image build). On warm baselines: `docker commit` after migrate gives a hot DB schema too. BuildKit cache mounts are the next step for the image-rebuild path. |

## CLI surface

```
sandbox build    <project>                  docker build the image
sandbox warmup   <project>                  cold-up → prep stages → docker commit :warm tags
sandbox run      <project> <stage>          full lifecycle: up → exec runner.sh <stage> → down
                                            <stage> ∈ build | test | all
                                            --snapshot warm|cold (default: warm)
sandbox exec     <project> -- <cmd>...      ad-hoc shell into the running sandbox
sandbox destroy  <project>                  compose down -v
sandbox ps       <project>                  compose ps
sandbox logs     <project> [svc]            compose logs
```

This is what a CI runner, an AI harness, or a developer would call. It
deliberately doesn't expose a stateful service — `docker compose` is the
state. If you need a long-lived queue/orchestrator above this, build it as
your harness; treat this as the substrate.

## Lifecycle in one diagram

```
                                  ┌──────────────────────────────┐
                                  │  Caller (CI, AI harness,     │
                                  │   developer at terminal)     │
                                  └──────────────┬───────────────┘
                                                 │  sandbox <cmd>
                                                 ▼
                                  ┌──────────────────────────────┐
                                  │  harness/sandbox.py          │
                                  │  thin CLI: ~250 lines        │
                                  └──────────────┬───────────────┘
                                                 │  docker compose -f
                                                 ▼
   ╭─────────────────────────────────────────────────────────────────────╮
   │  compose/eshop.yml          OR        compose/medplum.yml           │
   │  ┌──────────────┐                     ┌──────────────┐ ┌──────────┐ │
   │  │  sqlserver   │                     │   postgres   │ │  redis   │ │
   │  └──────────────┘                     └──────────────┘ └──────────┘ │
   │  ┌──────────────┐                     ┌──────────────┐              │
   │  │    eshop     │                     │   medplum    │              │
   │  │  (workload)  │                     │  (workload)  │              │
   │  └──────────────┘                     └──────────────┘              │
   │  ai-harness-eshop-net                  ai-harness-medplum-net       │
   ╰─────────────────────────────────────────────────────────────────────╯
                       │                                  │
                       └──────────────┬───────────────────┘
                                      │  egress: HTTP_PROXY
                                      ▼
                          ┌────────────────────────┐
                          │  egress-proxy          │
                          │  tinyproxy + allowlist │
                          └─────────┬──────────────┘
                                    ▼
                              github / nuget / npm / mcr
```

Reset between runs is `docker compose down -v` (volumes wiped — data was on
tmpfs, so this is sub-second). Restart from warm baseline is `docker compose
up` with the `:warm` image tag substituted in via env var — typically
under 5 seconds for the whole stack to be healthy.

## Demo script

```bash
# 1. bootstrap
make bootstrap                       # ~1s; writes .env

# 2. build the eshop sandbox image (slow, one-time: pulls .NET SDK + restores NuGet)
make build PROJECT=eshop             # ~3–5 min cold; subsequent runs cached

# 3. warm baseline (slow, one-time: cold-up + migrate + commit)
make warmup PROJECT=eshop            # ~60–90s; the only run that pays full startup cost

# 4. test runs against the warm baseline — this is what an agent harness would do
time make run-test PROJECT=eshop     # target: ~25–30s total
cat out/eshop/*/result.json          # structured result, per-stage timing

# 5. another test run, no cleanup needed (the previous one already torn itself down)
time make run-test PROJECT=eshop     # demonstrates repeatability + reset
```

## What's deliberately out of scope

- **Orchestration above the sandbox** (queue, workflow engine, MCP server,
  Temporal). Earlier explorations parked under `docs/out-of-scope/` —
  with notes on why we pulled them back.
- **The agent itself** (LLM loop, tool dispatching, prompt engineering).
  That's the consumer of this runtime.
- **Production k8s migration**. Same images and runner.sh work as-is; need
  manifests + NetworkPolicy + RuntimeClass. Notes in the gVisor scripts.

## Egress allowlist (caveat)

The `egress-proxy/` directory ships a tinyproxy + FQDN allowlist
(`allowlist.txt`). `compose/eshop.yml` and `compose/medplum.yml` declare
it as a service on each project's network. **But:**

- The proxy is *advisory*. Code that honors the `HTTP_PROXY` /
  `HTTPS_PROXY` env vars routes through it; code that opens raw sockets
  to an IP doesn't. The container's network namespace has direct
  outbound on this host.
- We tried setting `HTTP_PROXY` on the workload service and discovered
  it broke .NET's LibMan HTTPS-via-proxy negotiation (LIB002 even with
  cdnjs allowlisted and `curl -x` working manually). Rather than ship
  a half-working setup, we removed the env-var injection and documented
  the gap honestly.

What this means in practice:
- The proxy is useful as a **build-time** dependency mirror (a deliberate
  caller can route through it).
- It is **not** a runtime sandbox-escape boundary.

To make egress actually enforced, the production path is:
- **k8s**: a `CiliumNetworkPolicy` with `toFQDNs` allowlist on the
  workload pod. Same allowlist; kernel-enforced.
- **Same-host Docker**: `iptables` in the container's netns + a custom
  network plugin, or run the workload behind a `NetworkPolicy`-style
  service mesh (Istio etc.).
- **gVisor + Cilium** is the combination Anthropic Managed Agents and
  e2b's hosted runtime both use.

This is honest about an active limitation rather than a false claim of
filtered egress.

## How I used AI on this exercise

The brief asks. Honest write-up:

**Tools.** Claude Code as the primary pair; a fresh Claude Sonnet review
as a sanity check at one critical junction (see "course correction" below).

**What AI got right.**
- Boilerplate at the byte level — Dockerfiles, compose YAML, argparse
  CLI scaffolding, bash plumbing in `runner.sh`. Saved hours.
- Surfacing the eShopOnWeb specifics quickly: `gh api` queries to
  discover the `.NET 10` (not 9!) target, the two DbContexts
  (`CatalogContext` + `AppIdentityDbContext`), the actual health-check
  endpoint paths (`/home_page_health_check`, not `/health`).
- Recognizing when the WSL2 / Docker Desktop bind-mount permission
  quirk was eating us. The diagnostic loops were AI-driven.
- Debugging the silent exit-code bug: when `execd` reported
  `execution_complete` for success but `error` for failures, our parser
  defaulted to 0 on the wrong event type. AI noticed the discrepancy
  by probing the protocol directly.

**Where AI went wrong (and where I had to intervene).**
The single biggest course-correction: AI happily designed an entire
orchestration tier (Temporal + a Runtime Manager service + LangGraph +
OpenSandbox) before I asked an independent reviewer to read the brief.
The reviewer's critique:

> *"You've designed the top of the stack (workflow coordination) and
> skipped the middle (isolation, state reset, output capture, resource
> model, DB strategy) which is where the brief actually grades you. Cut
> Temporal, justify or drop OpenSandbox by name, and put the design
> weight on snapshotting, per-project compose profiles, and isolation
> primitives."*

That was correct. The brief literally says *"we're not asking you to
build an AI harness"* and AI built a harness anyway. I demolished
~600 LoC of orchestration code, parked it in `docs/out-of-scope/` with
a write-up of why, and rebuilt around per-project compose profiles +
`docker commit` snapshot reset + a thin CLI.

The pull-back was the most useful step in the project. It's a real
example of the failure mode the role description hints at: agents tend
to over-engineer the most-visible architectural layer rather than the
one that's actually graded. Catching that requires either a sharp
reviewer or a checklist; raw "more AI" doesn't help.

**Things I'd do differently next time.**
- Read the brief into the conversation *as a constraint document*
  before letting AI propose architecture. Forcing AI to map each design
  decision onto a brief criterion would have caught the over-scoping
  earlier.
- Validate the second project (Medplum) in lockstep with the first
  rather than at the end. We have one project working end-to-end
  (eShop) and one scaffolded (Medplum); a side-by-side build would
  have surfaced cross-cutting issues (e.g. uid mismatches on bind
  mounts) once, not twice.
- Bake a "demo evidence" target into the Makefile from day 1
  (screenshots, recorded asciicast). We have working CLI invocations
  but no curated evidence — the panel can run it but can't *see* it
  before they do.

This whole exercise — using an agent to build the substrate that
agents will run inside — was the most useful prompt for the role. The
specific things that went wrong are the things the substrate needs to
help future agents avoid.

## Limitations + things to know

- **Image size** (eshop: ~5.4 GB). Driven by the .NET 10 SDK + Blazor +
  test framework NuGet packages. Could shrink with multi-stage builds for
  pure runtime images, but the brief calls for an *agent* sandbox where
  the agent runs `dotnet test` etc. inside — so the SDK has to be in.
- **Single host**. The CLI calls `docker` locally. For multi-host fleets,
  point it at a remote Docker daemon (`DOCKER_HOST`) or move to k8s.
- **Sequential default** (one run per project at a time). Parallel runs
  require unique compose project names; the CLI has a hook for that
  (`RUN_ID` is exported to compose) but the demo flow is one-at-a-time.
- **medplum**: Dockerfile + runner.sh scaffolded but not validated
  end-to-end (eshop was the working path).
- **Container runs as root** in WSL2 because Docker Desktop doesn't
  propagate host bind-mount permissions into the container's user
  namespace. Mitigated by `cap_drop: [ALL]` + `no-new-privileges`, the
  per-project network namespace, and the egress allowlist. On native
  Linux you can drop back to uid 1001.
