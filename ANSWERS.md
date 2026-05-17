# Answers

Plain text answers to the brief. Where a claim maps to code in this repo, the
file path is cited; where an item is proposed but not yet wired up, it is
explicitly marked "Not implemented".


## 1. Sandbox topology

One sandbox per application. Each application is split across TWO compose
files with independent lifecycles:

```
compose/databases/<project>.yml   long-lived DB stack. Owns the network.
                                  Started once per agent session.
compose/<project>.yml             ephemeral workload stack. Joins the
                                  DB's network as `external: true`.
                                  Created + destroyed per iteration.
```

Per-project network namespace; zero cross-talk between an eshop run and a
medplum run on the same host. Same shape (compose pair + image +
runner.sh + result.json) generalises to any new app.

```
compose/databases/eshop.yml     SQL Server                          (network: ai-harness-eshop-net)
compose/eshop.yml               eshop workload + egress-proxy       (joins ai-harness-eshop-net)
compose/databases/medplum.yml   Postgres + Redis                    (network: ai-harness-medplum-net)
compose/medplum.yml             medplum workload + egress-proxy     (joins ai-harness-medplum-net)
```

Why split? The agent loop pays the database startup cost (SQL Server boot
+ healthcheck wait ~5-8s) ONCE per session, not once per iteration. See Q5.


## 2. Databases

Three engines across the two projects. No consolidation - keeps per-app
plugin constraints, schema lifecycles, and freedom to experiment independent.

- SQL Server   - compose/databases/eshop.yml   (mcr.microsoft.com/mssql/server:2022-latest)
- Postgres     - compose/databases/medplum.yml (postgres:16-alpine)
- Redis        - compose/databases/medplum.yml (redis:7-alpine)

Bootstrap-once-per-session pattern:

1. `make warmup` (one-time, after image build): brings the DB stack up cold,
   runs restore + build + migrate via runner.sh inside the workload, then
   `docker commit`s each running container as `ai-harness/<project>-<svc>:warm`.
   Both the DB and workload containers get tagged so the migrated schema
   travels with the DB's `:warm` tag. Implemented in harness/sandbox.py
   cmd_warmup.
2. `make db-up` (once per session): starts the DB stack from its `:warm`
   tag. SQL Server boots with `eShopOnWeb_Catalog` + `eShopOnWeb_Identity`
   already migrated.
3. `make run-test` (per iteration): brings up the workload, runs the
   requested stage, tears DOWN ONLY THE WORKLOAD. The DB stays running for
   the next iteration. `dotnet ef database update` becomes an EF no-op
   (~10s, dominated by `dotnet ef` tool startup; the actual "is the schema
   current?" check is sub-millisecond).
4. `make destroy` (end of session): tears down both stacks. Full reset.

Data location depends on the engine, and the choice is driven by whether
`docker commit` can capture it:

- **SQL Server**: writable layer (NOT tmpfs, NOT a named volume). The
  `mcr.microsoft.com/mssql/server:2022-latest` image declares no `VOLUME`
  on `/var/opt/mssql` (verified via `docker image inspect ... --format
  '{{.Config.Volumes}}'` -> `map[]`), so the engine's `.mdf`/`.ldf` files
  land in the container's writable layer. That layer IS what `docker
  commit` captures, so the migrated schema travels with the `:warm` tag.
  See compose/databases/eshop.yml for the rationale comment.
- **Postgres**: writable layer at `PGDATA=/var/postgres-data` (NOT under
  the image-declared `VOLUME ["/var/lib/postgresql/data"]`). The Postgres
  image DOES declare a VOLUME at `/var/lib/postgresql/data` — anything
  written there goes to an anonymous Docker volume that `docker commit`
  skips. By overriding `PGDATA` to a path outside the declared VOLUME,
  postgres writes go to the writable layer instead, which `docker commit`
  captures. After warmup seeds `medplum_test` (via medplum's
  `seed.test.ts`), the warm image carries the seeded schema. **Required**:
  postgres must be gracefully stopped (SIGTERM, ~1-2s clean shutdown)
  before `docker commit`, else the captured state is mid-flight and the
  next start spends 60-120s in `syncing data directory (fsync)` crash
  recovery. `harness/sandbox.py cmd_warmup` does this for any service
  in the project's `db_services` list.
- **Redis**: in-memory by default (`--save "" --appendonly no`); nothing
  to persist.

Reset semantics:

- Per iteration: workload `compose down -v` wipes the workload container's
  writable layer (in-run edits, /tmp). DB stays untouched.
- Per session: `make destroy` wipes both. SQL Server's schema state is
  preserved in the `:warm` image; next `db-up` rehydrates it.
- Per host: `make nuke` removes all images including `:warm`.


## 3. Clean state strategy

Clean by design between iterations:

- Workload `compose down -v` after every `run` (harness/sandbox.py cmd_run).
  Wipes the workload container's writable layer; per-iteration in-run
  edits to `/workspace/repo`, `/tmp`, and `bin/obj` deltas are discarded.
- Workload container recreated from `:warm` baseline on the next `run`;
  full image rebuild only happens when the Dockerfile or source changes.
- DB stays up between iterations by design (Q5 optimization). Schema is
  reset only at session boundaries via `make destroy` + `make db-up`,
  which restores from `:warm`.

Caching layers actually in the repo:

- SQL Server schema baked into the `:warm` DB image via `docker commit`.
  Possible because the image declares no `VOLUME` on `/var/opt/mssql`
  (see Q2). The migrated `__EFMigrationsHistory` table travels with the
  image; next `dotnet ef database update` finds the schema current and
  is a no-op.
- NuGet packages baked into the eshop image layer at build time.
  images/eshop/Dockerfile sets `NUGET_PACKAGES=/opt/nuget/packages` and runs
  `dotnet restore eShopOnWeb.sln` in a RUN step. Packages live in the layer,
  not in a BuildKit cache mount.
- `bin/obj` baked into the `:warm` workload image via `docker commit` after
  the warmup `dotnet build`. Next iteration's incremental build reuses
  these artifacts (build stage: ~5s warm vs ~18s cold).
- Postgres seeded schema baked into the `:warm` DB image via `docker
  commit`, mirroring the SQL Server pattern. Trick: postgres image
  declares `VOLUME ["/var/lib/postgresql/data"]`, so we override
  `PGDATA=/var/postgres-data` (a path NOT under the declared VOLUME).
  Postgres writes go to the container's writable layer, which `docker
  commit` captures. After warmup runs medplum's `seed.test.ts` to
  populate `medplum_test`, the warm postgres image starts on next
  `db-up` already-seeded (~4s startup instead of ~60-90s seed cost).
  Required: gracefully `docker stop` postgres before commit
  (`harness/sandbox.py cmd_warmup`) so the writable layer is in a
  consistent state — otherwise the next start spends 60-120s in crash
  recovery `syncing data directory (fsync)`.
- node_modules baked into the medplum image layer.
  images/medplum/Dockerfile runs `npm ci --include=dev` over the full
  30-package monorepo at build time. The runner skips re-install when
  node_modules is present and lockfile is unchanged (~80s saved per
  iteration).
- Default Docker layer cache for everything else.

What is NOT implemented (vs. earlier draft phrasing):
- BuildKit `RUN --mount=type=cache` for nuget / pnpm stores. Current
  implementation bakes packages into the layer at image-build time, which
  is good for warm-start but reorders cache invalidation differently.


## 4. Persistent context across iterations

Problem: a headless agent's reasoning loop spans many sandbox lifecycles.
Sandboxes are ephemeral - created and destroyed without record - so the
agent needs a way to carry state across iterations: the latest test
results, prior reasoning notes, the diff that was tried last.

Implementation: filesystem, anchored by a task identifier (`--task <id>`).

```
out/<project>/<task>/memory/          bind-mounted into the sandbox as
                                      /memory. Agent-writable scratchpad,
                                      survives `compose down -v`.
out/<project>/<task>/iterations.jsonl append-only iteration log
                                      (run_id, stage, exit_code,
                                      duration, source_dir, db_was_up).
out/<project>/<task>/<run_id>/        full per-iteration artifacts
                                      (result.json, .trx, app.log, stage
                                      logs). run_id format:
                                      2026-05-17T01-14-53-test-434b1e
                                      so dirs sort chronologically.
```

CLI for inspecting iteration history:

```
harness/sandbox.py tasks ls
harness/sandbox.py tasks show <project> <task-id>
harness/sandbox.py tasks memory <project> <task-id>
```

Wired up in `harness/sandbox.py cmd_run` and the `tasks` subcommand group.


## 5. intra

Measured wall times for the eshop project (Linux WSL2, Docker Desktop):

| Stage | Wall | In-container | Per-stage breakdown |
|---|---|---|---|
| Cold (no warm, DB cold) | 70s | 51s | build 15s, migrate 16s, test 16s |
| Warm (first iteration; DB cold-starts) | 51s | 35s | build 5s, migrate 10s, test 17s |
| Warm + DB up, full suite (113 tests) | **29s** | **22s** | build 4s, migrate **skipped (was 10s)**, app_start **removed (was 3s)**, test 17s |
| Warm + DB up, narrow filter (single class, 2 tests) | **16s** | **9s** | build 5s, migrate **skipped**, test 3s |
| Incremental (warm + DB up + --source overlay) | ~30s no-edit; ~38s with a 1-file edit | ~22-30s | rsync overlay preserves bin/obj |

Strategy: tiered. Pay the full price once at session start, then iterate
cheaply. The 10s target is reached for the *in-container work* on a
narrow-filter iteration; the remaining ~7s of wall time is compose
container create/destroy overhead, which would require D below to remove.
See README "Lifecycle stages" for the full table.

Currently implemented in this repo:

- `:warm` snapshot via `docker commit` of BOTH the DB container and the
  workload container. SQL Server's schema state (in the writable layer
  because the image declares no VOLUME) AND the workload's `bin/obj` tree
  travel with the snapshot.
- Independent DB lifecycle (`compose/databases/<project>.yml` + workload
  joins as `external` network). DB stays up across iterations; only
  workload restarts.
- NuGet / pnpm warm caches baked into image layers (images/*/Dockerfile).
- Host-source build context: source lives at `checkouts/<project>/` on
  the host. `docker buildx build --build-context <project>-src=...`
  injects it into the image. No `git clone` at image-build time. Update
  source via `make refresh-source` (git fetch + reset --hard on the
  host) without rebuilding.
- `.dockerignore` deposited at the build-context root before each build
  (from `images/<project>/dockerignore.template`). Keeps the context
  small and cache keys stable when bin/obj/node_modules churn between
  iterations.
- `COPY --link` on every COPY in the workload Dockerfiles, with numeric
  `--chown=1001:1001` (eshop) / `1000:1000` (medplum) so the link layer
  doesn't need the username resolved during the COPY (`--link` runs
  before the `agent` user is created).
- xUnit parallel-collection config baked into the image
  (`xunit.runner.json` in each test project dir). Opt-out via
  `PARALLEL_TESTS=0` env var on the workload (runner.sh removes the
  runner.json files at runtime). Marginal impact on the current test mix
  — a single 12s `PublicApiIntegrationTests.dll` assembly dominates, and
  xUnit's `parallelizeCollections` only parallelises within one assembly.
- Skip-migrate-when-current: runner.sh counts `*.cs` migration files in
  `src/Infrastructure/Data/Migrations/` and `.../Identity/Migrations/`,
  compares to row count in `__EFMigrationsHistory` on the live DB. If
  DB >= source, skips the `dotnet ef database update` call entirely
  (saves ~10s of pure `dotnet ef` tool startup per iteration). If source
  has new migrations or DB is empty, falls back to running `dotnet ef`.
- Drop app_start from test path: eShop tests use
  `WebApplicationFactory<Program>` (in-memory test server) — they don't
  need a separately-running `dotnet run`. Removed from runner.sh do_test
  (saves ~3s per iteration).
- Test scope via `--filter`: CLI flag passes through to
  `dotnet test --filter`. Agent specifies e.g.
  `FullyQualifiedName~BasketServiceTests` to narrow the run from 113
  tests / ~17s to a single class / ~3s.
- rsync overlay of host source onto the baked /workspace/repo, preserving
  `bin/` and `obj/` so dotnet build runs incrementally
  (images/eshop/runner.sh).
- Alpine variants for Postgres + Redis (medplum).
- Tight healthcheck intervals (3-5s) on Postgres / Redis / SQL Server.

Considerations table below splits all candidates into Implemented,
Not implemented (proposed), and Out of scope.

Sorted by impact within each scope band: Implemented (high to low) first,
then Not implemented / proposed (high to low), then Out of scope.

| Optimization | Mechanism | Why it helps | Status |
|---|---|---|---|
| Warm DB schema via `docker commit` of writable layer | SQL Server image declares no VOLUME on /var/opt/mssql, so the migrated schema lives in the container's writable layer. `docker commit` after warmup captures it into `:warm`. Stable DB names (`eShopOnWeb_Catalog`, `eShopOnWeb_Identity`) so the warm schema is the one the next run uses. | EF migrate becomes a no-op every iteration (~10s of pure `dotnet ef` tool startup, vs ~16s of real schema work cold). Saves ~6s per iteration. | Implemented (compose/databases/eshop.yml, images/eshop/runner.sh DB_CATALOG/DB_IDENTITY) |
| Independent DB lifecycle | DB compose owns the network; workload compose joins as `external: true`. `make db-up` once per session; `make run` only restarts the workload. | SQL Server boot + healthcheck wait (~5-8s) is paid once per session, not per iteration. Headline savings: warm path drops from 51s -> 42s per iteration. | Implemented (compose/databases/, harness/sandbox.py cmd_db_up + cmd_run) |
| Warm snapshot of workload container | `docker commit` after warmup captures bin/obj + NuGet cache + dotnet-ef tool into `:warm`. | Build stage drops from ~18s cold to ~5s warm. NuGet restore is sub-second on warm. | Implemented (harness/sandbox.py cmd_warmup) |
| rsync overlay of host source | runner.sh syncs `/workspace/src` -> `/workspace/repo`, excludes `bin/`, `obj/`, `.git/` | Lets dotnet build run incrementally between iterations (1-3s for a 1-file edit vs ~18s from scratch). Preserves `.git` so the baked repo's canonical history isn't stomped by the agent's local working tree. | Implemented (images/eshop/runner.sh) |
| Skip migrate when DB schema is current | runner.sh counts source migration `.cs` files vs `SELECT COUNT(*) FROM __EFMigrationsHistory`. If DB >= source, calls `stage_skip` (records `{ok: true, duration_s: 0, skipped: true, reason: ...}` in result.json) instead of invoking `dotnet ef`. | Saves ~10s every warm iteration. `dotnet ef database update` pays ~4-5s of SDK / EF tool startup even when there's nothing to do; this avoids paying it twice (one per DbContext). On migration churn the check fails open and runs `dotnet ef` normally. | Implemented (images/eshop/runner.sh migrate_or_skip) |
| Drop app_start from test path | runner.sh do_test no longer runs `dotnet run` + waits for `/home_page_health_check`. | Saves ~3s every iteration. eShop's tests use `WebApplicationFactory<Program>` (in-memory test server); they own the app lifecycle and don't talk to a separately-running `dotnet run` instance. Verified by inspecting `tests/*/Web*Fixture.cs` and `ProgramTest.cs`. | Implemented (images/eshop/runner.sh do_test) |
| Test scope via `--filter` | `sandbox run --filter <expr>` → `TEST_FILTER` env var → `dotnet test --filter`. Format: `FullyQualifiedName~BasketServiceTests`, `ClassName=...`, `Category=...`. | Iteration cost scales with what the agent actually touched, not the whole suite. Measured: full suite test stage = 17s; single-class filter = 3s. Saves up to ~14s per iteration when the agent has a focused change. | Implemented (harness/sandbox.py cmd_run --filter, compose/eshop.yml TEST_FILTER, images/eshop/runner.sh do_test) |
| Warm Postgres schema via `PGDATA` override + `docker commit` | Override `PGDATA=/var/postgres-data` (a path NOT under the image-declared `VOLUME ["/var/lib/postgresql/data"]`) so postgres writes land in the writable layer instead of an anonymous volume. `cmd_warmup` runs medplum's `seed.test.ts` to populate `medplum_test` (~90s), gracefully stops postgres (SIGTERM checkpoint), and `docker commit`s the container as `:warm`. Subsequent `db-up` from `:warm` starts already-seeded in ~4s. Same docker-commit pattern as SQL Server, plus the PGDATA workaround for the VOLUME declaration. | Removes the 60-90s seed cost from every db-up after the first warmup. The graceful-stop step is critical: a live commit of postgres captures dirty shared buffers + half-written WAL, and the next start spends 60-120s in `syncing data directory (fsync)` crash recovery. | Implemented (compose/databases/medplum.yml `PGDATA` env, harness/sandbox.py cmd_warmup `docker stop -t 30` before commit) |
| NuGet / pnpm cache baked into image | `dotnet restore` / `pnpm install` runs in the Dockerfile; packages persist as a layer | First iteration in a session pays the restore cost; subsequent iterations reuse the layer | Implemented (images/eshop/Dockerfile, images/medplum/Dockerfile) |
| Alpine sidecar images | `postgres:16-alpine`, `redis:7-alpine` | Smaller images (~80 MB vs ~400 MB), faster first pull, smaller resident set | Implemented (compose/medplum.yml). SQL Server has no Alpine variant. |
| Healthcheck interval tuning | 3s on Postgres/Redis, 5s on SQL Server | Compose cold-start is dominated by healthcheck poll cadence; tightening intervals shaves 20-40s | Implemented (compose/*.yml) |
| Combined `RUN` instructions | `apt-get update && install && rm -rf /var/lib/apt/lists/*` in one RUN | Fewer layers, smaller image, faster extraction | Implemented (images/eshop/Dockerfile:21-31) |
| Replace in-Dockerfile `git clone` with host-source injection | Source cloned to `checkouts/<project>/` on the host (one-time, `make fetch-source`). `docker buildx build --build-context <project>-src=./checkouts/<project>` injects it; Dockerfile uses `COPY --link --from=<project>-src`. `make refresh-source` updates via git fetch + reset --hard, no image rebuild needed. | Eliminates github.com as a build-time dependency. Source updates decouple from image rebuilds. Reuses the same rsync overlay pattern already in runner.sh, applied earlier in the lifecycle. Trade-off: image is no longer self-sufficient from a clean checkout — depends on host source state, which is fine for a workstation agent loop. | Implemented (images/eshop/Dockerfile, images/medplum/Dockerfile, harness/sandbox.py cmd_fetch_source + cmd_build) |
| `.dockerignore` at build-context root | `images/<project>/dockerignore.template` is deposited at `checkouts/<project>/.dockerignore` by `cmd_build` on every invocation. Excludes `bin/`, `obj/`, `node_modules/`, IDE dirs. | Cuts build context transfer; BuildKit hashes context on every build to compute cache keys. A bloated context (full of stale bin/obj from agent iterations) invalidates cache silently. | Implemented (images/<project>/dockerignore.template, harness/sandbox.py cmd_build) |
| `COPY --link` | All COPY instructions in the workload Dockerfiles use `--link` with numeric `--chown` (1001:1001 for eshop, 1000:1000 for medplum) — `--link` runs before the agent user is created, so the chown must use a UID that doesn't need name resolution. | Content-addressable independent layers. Reordering or rebuilding earlier instructions doesn't invalidate `--link` layers; lets BuildKit reuse more cache when the Dockerfile is edited. | Implemented (images/eshop/Dockerfile, images/medplum/Dockerfile) |
| Parallel test execution | `xunit.runner.json` (`parallelizeAssembly: true`, `parallelizeTestCollections: true`, `maxParallelThreads: 0`) baked into each tests/<proj>/ dir during image build. Opt-out via `PARALLEL_TESTS=0` env on workload — runner.sh deletes the runner.json files before `dotnet test`. | Multi-core utilisation on the test stage. Measured impact on the current eShop test mix: marginal (~1-2s, within noise) because a single 12s `PublicApiIntegrationTests.dll` assembly dominates and `parallelizeCollections` only parallelises within an assembly. Configuration is in place for when test count grows. | Implemented (images/eshop/Dockerfile, images/eshop/runner.sh do_test) |
| Dockerfile layer ordering | Copy `*.csproj` / `package.json` first -> restore -> only then `COPY . .` | Single highest-leverage Dockerfile pattern: source edits invalidate only late layers; the heavy restore layer stays cached | Partial — superseded for the current workflow by the host-source build context row above (the agent edits at runtime via `--source`, not by rebuilding the image, so source-edit-driven rebuild isn't the hot path). The two-stage manifest split would still help on `make refresh-source` + rebuild cycles. |
| Persistent workload container (no compose down between iterations) | Workload stays up like the DB does. Iteration becomes `docker compose exec` against the running container instead of `up → exec → down -v`. State reset is runner.sh's responsibility (re-rsync from /workspace/src, drop bin/obj on demand, clear test artifacts). | Saves the compose orchestration overhead — measured ~7s per iteration today, which is what stands between in-container 9s and wall 16s. With this in place, narrow-filter iterations would land at ~10-12s wall. | **Not implemented — deliberate tradeoff.** Considered for the 10s wall target; rejected because: (1) it weakens the per-iteration isolation guarantee currently provided by container destroy + recreate (cap_drop, no-new-privileges, baked rootfs all re-applied on fresh container; persistent container reuses the same OS state across runs); (2) state reset becomes runner.sh's responsibility and any miss leaks state (NuGet cache pollution, test result dirs, half-applied migrations) into the next iteration; (3) the 7s saved is one-time per iteration whereas a state-leak bug compounds across the agent's reasoning loop. The in-container work already hits the 10s target on a narrow filter (9s measured) — the remaining 7s is the price of the isolation guarantee the brief explicitly wants. Compose down/up is the simplest unambiguous reset. |
| EF migration bundle | `dotnet ef migrations bundle --context CatalogContext` produces a self-contained `efbundle` executable that starts in ~200ms vs `dotnet ef`'s ~4s. Bake both bundles at warmup; iteration runs `./efbundle-catalog --connection "..."` directly. | Replaces the "skip when current" pattern with a "always run, but cheap" pattern. ~1s per iteration with the bundle, vs ~10s for `dotnet ef`, vs 0s with skip-when-current. | **Not implemented — overlaps with "Skip migrate when DB schema is current" above.** Both target the same ~10s `dotnet ef` startup. Skip-when-current was chosen because: (1) ~0s vs ~1s, so it's strictly faster when the schema IS current (the agent-loop hot path); (2) no new build step or artifact to ship in the warm image; (3) simpler failure mode — if the check is wrong, we fall back to a normal `dotnet ef` invocation. The bundle is better when migrations actually need to be applied (the cold-first-iteration case), but for that path we still pay schema-creation cost which dominates the EF tool startup anyway. Worth revisiting only if migration churn becomes the hot path. |
| BuildKit cache mounts | `RUN --mount=type=cache,sharing=locked,target=/opt/nuget/packages` etc. | Persists package store across `docker build` invocations, concurrency-safe; complementary to the baked-in layer for the rebuild path | Not implemented (proposed). NOTE: conflicts with the current "bake NuGet into the image layer" pattern — cache mounts are not committed to the image, so the warm-start path would lose its hot NuGet cache. Worth it only if image rebuild becomes more frequent than warm starts (currently the reverse). |
| Local pull-through registry | Harbor or `registry:2` mirror mode for mcr.microsoft.com + docker.io | Eliminates Docker Hub throttling, dedupes layers across projects, speaks registry protocol | Not implemented (proposed) |
| Local NuGet / npm proxy (Squid or NuGet server) | HTTP cache between sandbox and nuget.org / npmjs.org | Avoids re-fetching dependencies across image rebuilds; pairs well with `git clone` replacement above | Not implemented (proposed) |
| `BUILDKIT_INLINE_CACHE=1` | `--build-arg BUILDKIT_INLINE_CACHE=1` + `--cache-from <local-image>` | Lets the warm-baseline image act as a cache source for the next rebuild, fully local; no external registry needed | Not implemented (proposed). Marginal in a single-host workflow where Docker's local layer cache already provides reuse; primarily helps when builds happen across hosts. |
| `COMPOSE_BAKE=true` / `buildx bake` | Compose delegates to Buildx Bake; services build in parallel with work dedup | Helps when multiple services need rebuild (eshop + egress-proxy, eshop + medplum side-by-side) | Not implemented (proposed) |
| Multi-stage SDK / runtime split | SDK in build stage only; slim ASP.NET runtime as final image | Smaller warm snapshot, faster save/restore. Caveat: current Dockerfile is deliberately single-stage because the agent runs `dotnet build` and `dotnet test` inside the running container, so SDK has to be present at runtime. Worth revisiting only if a separate test-only stage is introduced. | Not implemented (proposed; Dockerfile comment explains the trade-off) |
| Slim / chiseled / distroless runtime base | `mcr.microsoft.com/dotnet/aspnet:10.0-...-distroless-extra` for the runtime stage | Sub-100 MB runtime; combined with multi-stage split improves warm-snapshot save/restore | Not implemented; tied to the multi-stage split decision above |
| Bind mount source at build time | `RUN --mount=type=bind,source=.,target=/src` for build-only operations | Source read from build context without baking into a layer | Not implemented at build time; superseded by the named-build-context approach above. |
| BuildKit garbage-collection tuning | `/etc/docker/daemon.json`: raise `builder.gc.defaultKeepStorage` | Default GC can evict cache mounts between iterations on disk-constrained hosts, silently undoing the cache-mount win | Not implemented (host-level config, conditional on disk pressure) |
| `zstd` image compression | `docker buildx build --output type=image,compression=zstd` | ~60% faster decompression, multi-threaded, ~27% faster startup measured by AWS | Out of scope - warm baseline lives on the local host and is reused in place; no save/load or pull between iterations, so layer decompression isn't on the hot path |
| `--cache-from` against remote registry | `--cache-from=type=registry,ref=...` | Cross-host build-cache share for CI fleets | Out of scope - single-host agent loop caches locally after iteration 1; INLINE_CACHE row covers the local-only variant |
| Remote build accelerators (Depot / Docker Build Cloud / Blacksmith) | Offload `docker build` to a managed remote builder with shared org cache | Removes builder hardware as bottleneck | Out of scope - single-host agent loop adds latency without payoff |
| Cross-host registry mirror dedupe | Shared `registry:2` mirror across multiple agent hosts | Layers pulled once per fleet, not once per host | Out of scope - relevant only when scaling to multi-host agent fleets |


## 6. Output capture

What is implemented:
- /results/result.json with a stable shape: run_id, task_id, project,
  stage, status, exit_code, memory_dir, source_override, duration_s,
  stages: {name: {ok, duration_s, log}}, artifacts: [...]
  (images/eshop/runner.sh, "result.json scaffold" block)
- Stable exit-code taxonomy: 0=pass, 10=build_fail, 20=test_fail,
  30=migrate_fail, 40=health_fail, 50=timeout, 60=infra, 70=patch_fail,
  80=checkout_fail
- Per-stage logs (restore.log, build.log, migrate.log, app.log, test.log,
  run.log) bind-mounted to the host at `out/<project>/[<task>/]<run_id>/`
- dotnet test --logger trx - TRX (structured XML) parseable downstream
  (images/eshop/runner.sh `do_test` function)
- Per-iteration `db_was_up` field in iterations.jsonl so the agent can
  distinguish "first iteration this session" from "DB-warm iteration".

Not implemented:
- xUnit JUnitXml logger - would require adding `JUnitXml.TestLogger` NuGet
  to each test csproj, which means modifying eShop source. Skipped.
- A unified parser that normalises TRX (and future JUnit/pytest/jest) into
  a common schema. Result.json today carries TRX paths in `artifacts`; the
  consumer parses them.


## 7. Multi-tenant isolation

Docker Compose per project, with each project split across a DB stack and
a workload stack on the same per-project network namespace. Databases are
single-tenant within a project (no cross-app DB sharing). Straightforward,
minimal overhead.

- ai-harness-eshop-net    - created by compose/databases/eshop.yml,
                            joined as external by compose/eshop.yml
- ai-harness-medplum-net  - created by compose/databases/medplum.yml,
                            joined as external by compose/medplum.yml

Two compose project names per application:

- ai-harness-eshop-db     (DB stack, long-lived)
- ai-harness-eshop        (workload stack, ephemeral)

Concurrent runs against the same project would collide on container_name;
parallel needs separate compose `-p <name>` invocations. CLI exports
`RUN_ID` to compose, but the demo flow is one run at a time.


## 8. Security boundaries

Implemented:
- Network egress proxy: egress-proxy/ ships tinyproxy + FQDN allowlist,
  declared on each project's workload network. CAVEAT: advisory only.
  HTTP_PROXY is intentionally not set on the workload (compose/eshop.yml,
  see the comment near the eshop service `environment:` block) - setting
  it broke .NET LibMan HTTPS-via-proxy negotiation, and the container
  netns has unrestricted direct outbound regardless. Documented in README
  "Egress allowlist (caveat)".
- Linux capability drop on the workload: AUDIT_WRITE, MKNOD, NET_ADMIN,
  NET_RAW, SYS_ADMIN, SYS_MODULE, SYS_PTRACE, SYS_TIME, SYS_TTY_CONFIG.
  Plus `security_opt: no-new-privileges:true`.
  (compose/eshop.yml, compose/medplum.yml - `cap_drop:` block on each
  workload service)
- Per-project network namespace - zero reachability between an eshop run
  and a medplum run on the same host. The network is created by the
  per-project DB compose; the workload compose joins as external.
- gVisor opt-in path: scripts/install-gvisor.sh + scripts/verify-gvisor.sh
  install runsc on the host; `runtime: runsc` in compose is a one-line
  enable. Not enabled by default.
- Pre-created bind-mount destinations: `/memory` and `/workspace/src` are
  baked as directories in the image (images/eshop/Dockerfile,
  images/medplum/Dockerfile). Avoids a Docker Desktop WSL2 cache-poisoning
  issue where `docker commit` over a `/dev/null` bind-mount would freeze
  these as zero-byte files in the warm image, breaking subsequent
  directory binds. See README "Limitations" for the full story.

Not implemented:
- DinD (Docker-in-Docker) - listed as a future stronger-isolation option.
- Enforced egress at the kernel level (iptables in container netns, or k8s
  NetworkPolicy / Cilium toFQDNs). Real production fix.

Agent interface limitations:
- Injection is limited to env vars and bind mounts. Workload receives
  TASK_ID, SOURCE_DIR, MEMORY_DIR, RESULTS_DIR, DB credentials.
- Stage choice is fixed to {build, warmup, test, all}. The agent cannot
  pass per-run flags into the underlying dotnet / pnpm invocations without
  editing runner.sh.


## Boundaries of current solution

- Agent runtime out of scope. This is the substrate, not the harness.
- No external database for long-term memory or RAG. Filesystem only,
  scoped per task. Long-term self-learning capabilities are limited.
- Designed for one agent at a time per project. Concurrent runs need
  separate compose `-p <name>` invocations.
- No agent-to-agent communication.
- No MCP server or other rich interfaces. CLI (`harness/sandbox.py`) is
  the only entry point.
- No feedback hooks back to the agent runtime. Substrate emits result.json
  and exits; the harness decides what to do with the result.
- medplum project is scaffolded (Dockerfile + runner.sh + compose) but
  not validated end-to-end. eshop is the working path.
