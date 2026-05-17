# syntax=docker/dockerfile:1.7-labs
#
# Medplum sandbox image.
#
# Strategy: same shape as eshop — clone at build time, warm the pnpm store
# into a baked layer, then let the runner re-checkout + patch at task time.
# pnpm with `--frozen-lockfile` makes the warm cache near-deterministic.

ARG NODE_TAG=20-bookworm-slim
FROM node:${NODE_TAG}

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git ca-certificates curl jq tini \
        # postgres-client gives us `psql` for migrate/seed; redis-tools for ping
        postgresql-client redis-tools && \
    rm -rf /var/lib/apt/lists/*

# Pin pnpm via corepack — Medplum's repo pins a specific pnpm version in
# package.json; corepack will pick that up. Globally enable it here.
RUN corepack enable && corepack prepare pnpm@latest --activate

RUN groupadd -g 1000 agent && \
    useradd  -u 1000 -g 1000 -m -s /bin/bash agent && \
    mkdir -p /workspace /results /opt/pnpm-store && \
    chown -R agent:agent /workspace /results /opt/pnpm-store

USER agent
WORKDIR /workspace

ENV PNPM_STORE_DIR=/opt/pnpm-store \
    PNPM_HOME=/home/agent/.local/share/pnpm \
    PATH=/home/agent/.local/share/pnpm:$PATH \
    CI=1

ARG MEDPLUM_REF=main
RUN git clone --depth 1 --branch "${MEDPLUM_REF}" \
        https://github.com/medplum/medplum.git repo && \
    cd repo && \
    pnpm config set store-dir "${PNPM_STORE_DIR}" && \
    # Warm the store. `--prefer-offline` later means repeat installs hit
    # the store first.
    pnpm install --frozen-lockfile --prefer-offline

COPY --chown=agent:agent runner.sh /usr/local/bin/runner.sh
COPY --chown=agent:agent healthcheck.sh /usr/local/bin/healthcheck.sh
USER root
RUN chmod 0755 /usr/local/bin/runner.sh /usr/local/bin/healthcheck.sh
USER agent

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD /usr/local/bin/healthcheck.sh
