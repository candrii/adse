"""In-memory state for the Runtime Manager.

Kept deliberately simple — process-local. For real production: move to Redis
or Postgres so multiple Manager replicas can share state. For the demo, one
Manager process is enough.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────── env record ───────────────────────


@dataclass
class EnvRecord:
    id:         str
    project:    str
    sandbox_id: str
    state:      str                 # "creating" | "ready" | "in_use" | "draining" | "terminated"
    created_at: float
    pool_id:    Optional[str] = None
    sidecars:   list[str]   = field(default_factory=list)   # sidecar names attached
    # Cache the OpenSandbox metadata so we don't re-GET on every operation
    sandbox_meta: dict      = field(default_factory=dict)


# ─────────────────────── sidecar record ───────────────────────


@dataclass
class SidecarRecord:
    name:        str
    env_id:      str
    sandbox_id:  str            # OpenSandbox id of the sidecar container
    image:       str
    state:       str            # "running" | "terminated"
    created_at:  float


# ─────────────────────── pool record ───────────────────────


@dataclass
class PoolRecord:
    name:      str
    project:   str
    min_size:  int
    max_size:  int
    ttl_s:     int
    # `available`: env_ids that are warm and unleased
    # `in_use`:    env_ids that have been leased
    available: list[str] = field(default_factory=list)
    in_use:    set[str]  = field(default_factory=set)


# ─────────────────────── store ───────────────────────


class Store:
    """Thread-safe state. The Manager is single-process but FastAPI handlers
    run on multiple threads via uvicorn workers, so a lock is mandatory."""

    def __init__(self) -> None:
        self._lock     = threading.RLock()
        self._envs:     dict[str, EnvRecord]     = {}
        self._sidecars: dict[tuple[str, str], SidecarRecord] = {}   # (env_id, name) -> rec
        self._pools:    dict[str, PoolRecord]    = {}

    # ─── envs ───
    def new_env_id(self) -> str:
        return f"env_{uuid.uuid4().hex[:12]}"

    def put_env(self, rec: EnvRecord) -> None:
        with self._lock:
            self._envs[rec.id] = rec

    def get_env(self, env_id: str) -> Optional[EnvRecord]:
        with self._lock:
            return self._envs.get(env_id)

    def update_env(self, env_id: str, **fields) -> Optional[EnvRecord]:
        with self._lock:
            rec = self._envs.get(env_id)
            if rec is None:
                return None
            for k, v in fields.items():
                setattr(rec, k, v)
            return rec

    def delete_env(self, env_id: str) -> Optional[EnvRecord]:
        with self._lock:
            return self._envs.pop(env_id, None)

    def list_envs(self) -> list[EnvRecord]:
        with self._lock:
            return list(self._envs.values())

    # ─── sidecars ───
    def put_sidecar(self, rec: SidecarRecord) -> None:
        with self._lock:
            self._sidecars[(rec.env_id, rec.name)] = rec
            env = self._envs.get(rec.env_id)
            if env and rec.name not in env.sidecars:
                env.sidecars.append(rec.name)

    def get_sidecar(self, env_id: str, name: str) -> Optional[SidecarRecord]:
        with self._lock:
            return self._sidecars.get((env_id, name))

    def list_sidecars_for_env(self, env_id: str) -> list[SidecarRecord]:
        with self._lock:
            return [r for (e, _), r in self._sidecars.items() if e == env_id]

    def delete_sidecar(self, env_id: str, name: str) -> Optional[SidecarRecord]:
        with self._lock:
            rec = self._sidecars.pop((env_id, name), None)
            env = self._envs.get(env_id)
            if env and name in env.sidecars:
                env.sidecars.remove(name)
            return rec

    # ─── pools ───
    def put_pool(self, rec: PoolRecord) -> None:
        with self._lock:
            self._pools[rec.name] = rec

    def get_pool(self, name: str) -> Optional[PoolRecord]:
        with self._lock:
            return self._pools.get(name)

    def list_pools(self) -> list[PoolRecord]:
        with self._lock:
            return list(self._pools.values())

    def delete_pool(self, name: str) -> Optional[PoolRecord]:
        with self._lock:
            return self._pools.pop(name, None)

    def lock(self):
        return self._lock


# Module-level singleton (one Manager process)
store = Store()
