"""Pydantic models for the Manager's HTTP surface.

Separate from the OpenSandbox / Temporal models so the API contract is
explicit and the Manager can evolve independently of the underlying
runtime engine.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ─────────────────────── env lifecycle ───────────────────────


class CreateEnvRequest(BaseModel):
    project:   str            = Field(..., description="Project key (eshop|medplum|...)")
    ref:       Optional[str]  = Field(None, description="Git ref to checkout (defaults to image's baked branch)")
    timeout_s: int            = Field(1800, ge=60, description="Sandbox TTL (matches OpenSandbox minimum)")
    use_pool:  bool           = Field(False, description="Lease from warm pool if available")
    metadata:  dict[str, str] = Field(default_factory=dict)


class EnvSummary(BaseModel):
    id:         str
    project:    str
    sandbox_id: str
    state:      Literal["creating", "ready", "in_use", "draining", "terminated"]
    created_at: float
    pool_id:    Optional[str] = None


# ─────────────────────── operations ───────────────────────


class ExecResult(BaseModel):
    ok:          bool
    exit_code:   int
    duration_s:  float
    stdout_tail: str = ""    # last 8K
    stderr_tail: str = ""    # last 4K


class TestResult(ExecResult):
    artifacts: list[str] = Field(default_factory=list, description="Paths inside the env (under /results)")


# ─────────────────────── pools ───────────────────────


class PoolSpec(BaseModel):
    project:  str
    min_size: int = Field(1, ge=0)
    max_size: int = Field(4, ge=1)
    ttl_s:    int = Field(3600, ge=300)


class PoolStatus(BaseModel):
    name:        str
    project:     str
    min_size:    int
    max_size:    int
    available:   int
    in_use:      int
    pending:     int


# ─────────────────────── sidecars ───────────────────────


class AttachSidecarRequest(BaseModel):
    name:  str          = Field(..., description="Sidecar name, unique within env")
    image: str          = Field(..., description="Sidecar container image (must be allowlisted)")
    env:   dict[str, str] = Field(default_factory=dict)
    timeout_s: int      = Field(1800, ge=60)
    # Sidecar shares the compose network and resolves its env's hostname:
    # set up sidecars whose `env.HOST_TARGET` points back into the env.


class SidecarSummary(BaseModel):
    name:        str
    sandbox_id:  str
    image:       str
    parent_env:  str
    state:       str
