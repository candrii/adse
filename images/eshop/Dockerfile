# syntax=docker/dockerfile:1.7-labs
#
# eShopOnWeb sandbox image.
#
# Strategy: clone the repo at build time and restore NuGet deps into the layer
# cache. At task time the harness re-checks-out the requested ref and applies
# the agent's patch — restore is incremental and usually a no-op.
#
# Multi-stage isn't needed here because the final image must contain the SDK
# (the agent runs `dotnet build`/`dotnet test`); we're not shipping a runtime
# image, we're shipping a build/test environment.
#
# Note: eShopOnWeb's `main` branch targets .NET 10 (per its global.json), so
# the base image is the .NET 10 SDK on Ubuntu Noble (24.04). MS doesn't ship
# a Debian/bookworm variant for the SDK 10 line yet.

ARG DOTNET_SDK_TAG=10.0-noble
FROM mcr.microsoft.com/dotnet/sdk:${DOTNET_SDK_TAG}

# ───────────────────────── system deps ─────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git ca-certificates curl jq tini \
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
RUN groupadd -g 1001 agent && \
    useradd  -u 1001 -g 1001 -m -s /bin/bash agent && \
    mkdir -p /workspace /results /opt/nuget/packages && \
    chown -R agent:agent /workspace /results /opt/nuget

# ───────────────────────── clone + warm caches ─────────────────────────
USER agent
WORKDIR /workspace

ARG ESHOP_REF=main
RUN git clone --depth 1 --branch "${ESHOP_REF}" \
        https://github.com/NimblePros/eShopOnWeb.git repo && \
    cd repo && \
    # Install dotnet-ef matching the EF Core packages eShop references
    # (10.0.x per Directory.Packages.props). The runner's `migrate` stage
    # calls this tool. Installed to ~/.dotnet/tools/ (on PATH above).
    dotnet tool install --global dotnet-ef --version "10.0.*" && \
    # Restore once so NuGet packages are baked into the image layer.
    dotnet restore eShopOnWeb.sln

# ───────────────────────── runner + healthcheck ─────────────────────────
COPY --chown=agent:agent runner.sh /usr/local/bin/runner.sh
COPY --chown=agent:agent healthcheck.sh /usr/local/bin/healthcheck.sh
USER root
RUN chmod 0755 /usr/local/bin/runner.sh /usr/local/bin/healthcheck.sh
USER agent

# Default command is a long-lived sleep; harness drives the sandbox via the
# OpenSandbox exec API (`POST /v1/sandboxes/{id}/commands`). Using tini so
# signal handling works when the harness sends DELETE.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD /usr/local/bin/healthcheck.sh
