# OpenSandbox control-plane image.
#
# Upstream publishes the server as a PyPI package, not a Docker image — so
# we wrap it ourselves. Pinned version to keep the demo reproducible; bump
# when you want.

FROM python:3.12-slim

ARG OPENSANDBOX_SERVER_VERSION=0.1.13

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates wget tini && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir "opensandbox-server==${OPENSANDBOX_SERVER_VERSION}"

EXPOSE 8080

# tini gives us proper signal handling so `docker stop` is fast and clean.
ENTRYPOINT ["/usr/bin/tini", "--", "opensandbox-server"]
CMD ["--config", "/etc/opensandbox/sandbox.toml"]
