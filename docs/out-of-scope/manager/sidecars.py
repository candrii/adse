"""Sidecar attach/detach over OpenSandbox.

Compose-mode constraint: Docker has no true sidecar concept. We approximate
it by creating a second sandbox container on the same `ai-harness-net`
network, with metadata linking it to the parent env. Containers reach each
other by their OpenSandbox-assigned hostname `sandbox-<id>`.

This works fine for "run k6 against the env's app" or "playwright against
the env" patterns. It's not the same as a Kubernetes Pod-level sidecar
(no shared PID/IPC namespace, no shared filesystem). On k8s migration this
becomes a proper pod sidecar — interface stays the same.
"""
from __future__ import annotations

import time

from ..sandbox import SandboxClient
from .projects import OperationError
from .state import SidecarRecord, store


def attach_sidecar(
    client:     SandboxClient,
    env_id:     str,
    parent_sbx: dict,
    name:       str,
    image:      str,
    env:        dict[str, str],
    timeout_s:  int,
) -> SidecarRecord:
    """Spin up a sidecar sandbox on the same compose network as the parent env.

    The sidecar's env automatically gets HOST_TARGET pointing at the parent
    so common patterns ("hit the env's API") need no extra wiring.
    """
    if store.get_sidecar(env_id, name):
        raise OperationError("sidecar", f"sidecar {name} already attached to {env_id}",
                             exit_code=409)

    parent_hostname = f"sandbox-{parent_sbx['id']}"
    sidecar_env = {
        "HOST_TARGET":     parent_hostname,
        "PARENT_ENV_ID":   env_id,
        "PARENT_SANDBOX":  parent_sbx["id"],
        **env,
    }
    sbx = client.create(
        image=          image,
        env=            sidecar_env,
        timeout_s=      timeout_s,
        resource_limits={"cpu": "1", "memory": "1Gi"},
        metadata={
            "ai_harness.role":         "sidecar",
            "ai_harness.parent_env":   env_id,
            "ai_harness.sidecar_name": name,
        },
    )
    rec = SidecarRecord(
        name=        name,
        env_id=      env_id,
        sandbox_id=  sbx["id"],
        image=       image,
        state=       "running",
        created_at=  time.time(),
    )
    store.put_sidecar(rec)
    return rec


def detach_sidecar(client: SandboxClient, env_id: str, name: str) -> None:
    rec = store.get_sidecar(env_id, name)
    if not rec:
        # Idempotent: already gone is fine
        return
    try:
        client.delete(rec.sandbox_id)
    except Exception:
        # Best effort — OpenSandbox TTL is the safety net
        pass
    store.delete_sidecar(env_id, name)
