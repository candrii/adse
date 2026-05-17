# syntax=docker/dockerfile:1.7-labs
#
# eShopOnWeb sandbox image.
#
# Strategy: the source comes from a host-managed checkout (`checkouts/eshop/`
# on the build machine) via BuildKit's named build context. The image does
# NOT `git clone` at build time — that:
#   - made every cold image build depend on github.com availability
#   - tied the `ESHOP_REF` to image-rebuild time (couldn't update without rebuild)
#   - duplicated network IO already paid for by `git pull` on the host
#
# Build invocation (handled by harness/sandbox.py cmd_build):
#
#   docker buildx build \
#       --build-context eshop-src=./checkouts/eshop \
#       -t ai-harness/eshop:latest \
#       images/eshop
#
# Multi-stage isn't needed: the final image must contain the SDK (the agent
# runs `dotnet build` / `dotnet test`) — we're shipping a build/test env,
# not a runtime.
#
# Note: eShopOnWeb's `main` branch targets .NET 10 (per global.json), so the
# base is .NET 10 SDK on Ubuntu Noble (24.04). MS doesn't ship a Debian
# variant for the SDK 10 line yet.

ARG DOTNET_SDK_TAG=10.0-noble
FROM mcr.microsoft.com/dotnet/sdk:${DOTNET_SDK_TAG}

# ───────────────────────── system deps ─────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git ca-certificates curl jq tini rsync \
        gnupg apt-transport-https && \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/ubuntu/24.04/prod noble main" \
      > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends mssql-tools18 unixodbc && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/mssql-tools18/bin:/home/agent/.dotnet/tools:${PATH}" \
    DOTNET_NOLOGO=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    NUGET_PACKAGES=/opt/nuget/packages

# ───────────────────────── non-root user ─────────────────────────
# /workspace/src and /memory are bind-mount destinations at runtime. They
# MUST exist as directories in the image, even when nothing is mounted
# there — otherwise `docker commit`-ing a container where these paths were
# bound to /dev/null (a char device) bakes them into the warm image as
# regular files, and the next run that bind-mounts a real directory to the
# same dst fails with "not a directory" at OCI init.
RUN groupadd -g 1001 agent && \
    useradd  -u 1001 -g 1001 -m -s /bin/bash agent && \
    mkdir -p /workspace /workspace/src /workspace/repo /memory /results /opt/nuget/packages && \
    chown -R agent:agent /workspace /memory /results /opt/nuget

# ───────────────────────── source from host build context ─────────────────────────
# `--from=eshop-src` is a named build context wired up at build time via:
#   docker buildx build --build-context eshop-src=./checkouts/eshop ...
# `--link` makes the layer content-addressable so subsequent rebuilds that
# only touch later instructions don't have to re-extract this layer.
COPY --link --chown=1001:1001 --from=eshop-src . /workspace/repo

# ───────────────────────── warm caches ─────────────────────────
USER agent
WORKDIR /workspace/repo

# Install dotnet-ef matching the EF Core packages eShop references
# (10.0.x per Directory.Packages.props) + restore NuGet packages into the
# layer so warm starts have a hot cache.
RUN dotnet tool install --global dotnet-ef --version "10.0.*" && \
    dotnet restore eShopOnWeb.sln

# Drop xunit.runner.json into each test project so the test stage uses
# parallel collections (xUnit's default is parallel-within-assembly but
# serial-across-collections; we want both). These are IMAGE-side overlays
# — we don't modify eShop source. Opt out via `PARALLEL_TESTS=0`
# environment in runner.sh (see do_test).
RUN for proj in tests/IntegrationTests tests/UnitTests tests/FunctionalTests tests/PublicApiIntegrationTests; do \
        if [ -d "/workspace/repo/$proj" ]; then \
            printf '{"parallelizeAssembly": true, "parallelizeTestCollections": true, "maxParallelThreads": 0}\n' \
                > "/workspace/repo/$proj/xunit.runner.json"; \
        fi; \
    done

# ───────────────────────── runner + healthcheck ─────────────────────────
WORKDIR /workspace
COPY --link --chown=1001:1001 runner.sh        /usr/local/bin/runner.sh
COPY --link --chown=1001:1001 healthcheck.sh   /usr/local/bin/healthcheck.sh
USER root
RUN chmod 0755 /usr/local/bin/runner.sh /usr/local/bin/healthcheck.sh
USER agent

# Default command is a long-lived sleep; the harness drives the sandbox via
# `docker compose exec`. tini handles signals when the harness sends down.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD /usr/local/bin/healthcheck.sh
