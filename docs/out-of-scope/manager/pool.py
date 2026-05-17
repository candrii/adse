"""Client-side warm pool.

OpenSandbox's native pool API is Kubernetes-only — for the Docker provider
we maintain pre-created envs ourselves. One background thread per pool keeps
`min_size` envs warm. Lease pops; release returns (after reset).

Trade-off: each warm env costs RAM/disk. Set min_size=1 for typical demos;
crank to 4–8 for real fleets.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ..sandbox import SandboxClient
from .models import PoolSpec, PoolStatus
from .projects import get_handler
from .state import EnvRecord, PoolRecord, store


log = logging.getLogger("manager.pool")


class PoolController:
    """Maintains warm pools per project. Run as a background thread.

    On startup we spin up one worker that walks every registered pool every
    few seconds and reconciles: top up to min_size, evict over max_size.
    """

    def __init__(self, _sandbox_client_factory: Callable[[], SandboxClient]):
        self._client_factory = _sandbox_client_factory
        self._stop_event     = threading.Event()
        self._reconciler:    Optional[threading.Thread] = None

    # ─── lifecycle ───

    def start(self) -> None:
        self._reconciler = threading.Thread(
            target=self._reconcile_forever,
            name="pool-reconciler",
            daemon=True,
        )
        self._reconciler.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._reconciler:
            self._reconciler.join(timeout=5)

    # ─── public API used by HTTP handlers ───

    def upsert(self, name: str, spec: PoolSpec) -> None:
        with store.lock():
            existing = store.get_pool(name)
            if existing:
                existing.min_size = spec.min_size
                existing.max_size = spec.max_size
                existing.ttl_s    = spec.ttl_s
            else:
                store.put_pool(PoolRecord(
                    name=     name,
                    project=  spec.project,
                    min_size= spec.min_size,
                    max_size= spec.max_size,
                    ttl_s=    spec.ttl_s,
                ))

    def status(self, name: str) -> Optional[PoolStatus]:
        rec = store.get_pool(name)
        if not rec:
            return None
        return PoolStatus(
            name=        rec.name,
            project=     rec.project,
            min_size=    rec.min_size,
            max_size=    rec.max_size,
            available=   len(rec.available),
            in_use=      len(rec.in_use),
            pending=     0,
        )

    def drain(self, name: str) -> None:
        rec = store.delete_pool(name)
        if not rec:
            return
        client = self._client_factory()
        for env_id in list(rec.available) + list(rec.in_use):
            env = store.delete_env(env_id)
            if env:
                try: client.delete(env.sandbox_id)
                except Exception: pass

    def try_lease(self, project: str) -> Optional[EnvRecord]:
        """Pop an idle env from any pool matching the project. Returns None
        if no warm env is available — caller should fall back to cold create."""
        with store.lock():
            for pool in store.list_pools():
                if pool.project != project or not pool.available:
                    continue
                env_id = pool.available.pop(0)
                pool.in_use.add(env_id)
                env = store.get_env(env_id)
                if env:
                    env.state = "in_use"
                    env.pool_id = pool.name
                    return env
        return None

    def release(self, pool_name: str, env_id: str) -> None:
        """Return a leased env to its pool. Already reset by the caller."""
        with store.lock():
            pool = store.get_pool(pool_name)
            if not pool:
                return
            pool.in_use.discard(env_id)
            env = store.get_env(env_id)
            if env:
                env.state = "ready"
                pool.available.append(env_id)

    # ─── internal: reconcile loop ───

    def _reconcile_forever(self) -> None:
        log.info("pool reconciler started")
        while not self._stop_event.is_set():
            try:
                self._reconcile_once()
            except Exception as exc:
                log.warning("reconciler iteration failed: %s", exc)
            self._stop_event.wait(10)
        log.info("pool reconciler stopped")

    def _reconcile_once(self) -> None:
        for pool in store.list_pools():
            available = len(pool.available)
            in_use    = len(pool.in_use)
            target    = max(pool.min_size - available, 0)
            ceiling   = pool.max_size - in_use - available
            to_create = min(target, max(ceiling, 0))
            for _ in range(to_create):
                try:
                    self._warm_one(pool)
                except Exception as exc:
                    log.warning("warm-up failed for pool %s: %s", pool.name, exc)
                    break  # don't hammer a broken backend

    def _warm_one(self, pool: PoolRecord) -> None:
        handler = get_handler(pool.project)
        client  = self._client_factory()
        env_id  = store.new_env_id()
        log.info("warming env=%s for pool=%s", env_id, pool.name)
        sbx = client.create(
            image=           handler.image,
            env=             handler.env_for_create(None),
            timeout_s=       pool.ttl_s,
            resource_limits= {"cpu": "2", "memory": "4Gi"},
            metadata={
                "ai_harness.env_id":  env_id,
                "ai_harness.project": pool.project,
                "ai_harness.pool":    pool.name,
            },
        )
        rec = EnvRecord(
            id=          env_id,
            project=     pool.project,
            sandbox_id=  sbx["id"],
            state=       "ready",
            created_at=  time.time(),
            pool_id=     pool.name,
            sandbox_meta=sbx,
        )
        store.put_env(rec)
        with store.lock():
            pool.available.append(env_id)
