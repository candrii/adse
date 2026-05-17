# syntax=docker/dockerfile:1.7-labs
#
# Medplum sandbox image.
#
# Strategy: same shape as eshop. Source comes from a host-managed checkout
# (`checkouts/medplum/`) via BuildKit's named build context — no `git clone`
# at build time. `npm ci` installs the full workspace deps into a baked
# layer so warm starts have a hot node_modules.
#
# Build invocation (handled by harness/sandbox.py cmd_build):
#
#   docker buildx build \
#       --build-context medplum-src=./checkouts/medplum \
#       -t ai-harness/medplum:latest \
#       images/medplum
#
# Medplum's repo uses npm + npm workspaces (per package.json
# `"packageManager": "npm@10.9.8"`). NOT pnpm. The test framework is
# jest, not vitest.

ARG NODE_TAG=22-bookworm-slim
FROM node:${NODE_TAG}

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git ca-certificates curl jq tini rsync \
        # postgres-client gives us `psql` for migrate/seed; redis-tools for ping
        postgresql-client redis-tools && \
    rm -rf /var/lib/apt/lists/*

# Pin npm via corepack — package.json sets packageManager: "npm@10.9.8".
RUN corepack enable && corepack prepare npm@10.9.8 --activate

# /workspace/src and /memory pre-created as directories — see eshop
# Dockerfile for the WSL2 cache-poisoning rationale. uid 1001 to avoid
# collision with node:20's built-in `node` user at uid 1000.
RUN groupadd -g 1001 agent && \
    useradd  -u 1001 -g 1001 -m -s /bin/bash agent && \
    mkdir -p /workspace /workspace/src /workspace/repo /memory /results /opt/npm-cache && \
    chown -R agent:agent /workspace /memory /results /opt/npm-cache

ENV NPM_CONFIG_CACHE=/opt/npm-cache \
    CI=1

# Source from host build context.
COPY --link --chown=1001:1001 --from=medplum-src . /workspace/repo

USER agent
WORKDIR /workspace/repo

# `npm ci` reads package-lock.json and installs deterministically.
# `--include=dev` keeps the dev dependencies (jest, tsx, typescript)
# because the runner runs build + test from source inside the container.
RUN npm ci --include=dev

WORKDIR /workspace
COPY --link --chown=1001:1001 runner.sh        /usr/local/bin/runner.sh
COPY --link --chown=1001:1001 healthcheck.sh   /usr/local/bin/healthcheck.sh
USER root
RUN chmod 0755 /usr/local/bin/runner.sh /usr/local/bin/healthcheck.sh
USER agent

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD /usr/local/bin/healthcheck.sh
