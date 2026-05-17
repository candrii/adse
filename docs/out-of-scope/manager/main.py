"""Runtime Manager — FastAPI service.

Sits between the orchestrator (Temporal activities, or any HTTP caller) and
OpenSandbox. Exposes higher-level environment operations than OpenSandbox's
raw container lifecycle: build, migrate, app/start, wait_healthy, tests,
reset, lease/release, sidecars.

Run inside the compose stack as service `runtime-manager`. The Temporal
worker calls it via `http://runtime-manager:8090/v1/envs/...`.

Env vars consumed:
  OPENSANDBOX_API           default http://opensandbox:8080
  OPEN_SANDBOX_API_KEY      optional
  MANAGER_HOST / MANAGER_PORT
  MSSQL_SA_PASSWORD         passed through to the eshop handler at op time
  POSTGRES_PASSWORD         passed through to medplum handler at op time
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ..sandbox import SandboxClient
from .models import (
    AttachSidecarRequest, CreateEnvRequest, EnvSummary, ExecResult,
    PoolSpec, PoolStatus, SidecarSummary, TestResult,
)
from .pool import PoolController
from .projects import OperationError, get_handler
from .state import EnvRecord, PoolRecord, SidecarRecord, store
from .sidecars import attach_sidecar, detach_sidecar


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("manager")


# ─────────────────────── client ───────────────────────


def _sandbox_client() -> SandboxClient:
    return SandboxClient(
        os.environ.get("OPENSANDBOX_API", "http://opensandbox:8080"),
        os.environ.get("OPEN_SANDBOX_API_KEY"),
    )


# ─────────────────────── app ───────────────────────


app = FastAPI(
    title=       "AI Harness Runtime Manager",
    description= "HTTP layer above OpenSandbox: envs + operations + sidecars + pools",
    version=     "0.1.0",
)


# ─────────────────────── error mapping ───────────────────────


@app.exception_handler(OperationError)
async def operation_error_handler(_, exc: OperationError):
    return JSONResponse(
        status_code= 502 if exc.exit_code >= 60 else 400,
        content={
            "error":       "operation_failed",
            "stage":       exc.stage,
            "message":     exc.message,
            "exit_code":   exc.exit_code,
            "stderr_tail": exc.stderr_tail,
            "stdout_tail": exc.stdout_tail,
        },
    )


# ─────────────────────── health ───────────────────────


@app.get("/health")
def health() -> dict:
    return {"status": "healthy", "envs": len(store.list_envs())}


# ─────────────────────── envs ───────────────────────


def _to_summary(rec: EnvRecord) -> EnvSummary:
    return EnvSummary(
        id=         rec.id,
        project=    rec.project,
        sandbox_id= rec.sandbox_id,
        state=      rec.state,   # type: ignore[arg-type]
        created_at= rec.created_at,
        pool_id=    rec.pool_id,
    )


@app.post("/v1/envs", response_model=EnvSummary)
def create_env(req: CreateEnvRequest) -> EnvSummary:
    handler = get_handler(req.project)

    # Try leasing from a pool first if asked
    if req.use_pool:
        leased = pool_controller.try_lease(req.project)
        if leased:
            log.info("env %s leased from pool", leased.id)
            return _to_summary(leased)

    env_id = store.new_env_id()
    log.info("creating env=%s project=%s", env_id, req.project)
    env_for_create = handler.env_for_create(req.ref)
    client = _sandbox_client()
    sbx = client.create(
        image=          handler.image,
        env=            env_for_create,
        timeout_s=      req.timeout_s,
        resource_limits={"cpu": "2", "memory": "4Gi"},
        metadata={
            "ai_harness.env_id":   env_id,
            "ai_harness.project":  req.project,
            **(req.metadata or {}),
        },
    )
    rec = EnvRecord(
        id=          env_id,
        project=     req.project,
        sandbox_id=  sbx["id"],
        state=       "ready",
        created_at=  time.time(),
        sandbox_meta=sbx,
    )
    store.put_env(rec)
    return _to_summary(rec)


@app.get("/v1/envs", response_model=list[EnvSummary])
def list_envs() -> list[EnvSummary]:
    return [_to_summary(e) for e in store.list_envs()]


@app.get("/v1/envs/{env_id}", response_model=EnvSummary)
def get_env(env_id: str) -> EnvSummary:
    rec = store.get_env(env_id)
    if not rec:
        raise HTTPException(404, f"env not found: {env_id}")
    return _to_summary(rec)


@app.delete("/v1/envs/{env_id}", status_code=204)
def delete_env(env_id: str):
    rec = store.get_env(env_id)
    if not rec:
        raise HTTPException(404, f"env not found: {env_id}")
    # If env is pooled, return to pool with reset rather than tearing down
    if rec.pool_id:
        client = _sandbox_client()
        handler = get_handler(rec.project)
        try:
            handler.reset(client, rec.sandbox_meta)
        except OperationError as exc:
            log.warning("reset failed for %s; destroying instead: %s", env_id, exc)
            _destroy(env_id)
        else:
            pool_controller.release(rec.pool_id, env_id)
        return
    _destroy(env_id)


def _destroy(env_id: str):
    rec = store.delete_env(env_id)
    if not rec:
        return
    # Detach any sidecars first
    for sc in store.list_sidecars_for_env(env_id):
        try:
            detach_sidecar(_sandbox_client(), env_id, sc.name)
        except Exception as exc:
            log.warning("sidecar teardown failed for %s/%s: %s", env_id, sc.name, exc)
    # Then the env's sandbox
    try:
        _sandbox_client().delete(rec.sandbox_id)
    except Exception as exc:
        log.warning("sandbox delete failed for %s: %s", rec.sandbox_id, exc)


# ─────────────────────── operations ───────────────────────


def _env_or_404(env_id: str) -> EnvRecord:
    rec = store.get_env(env_id)
    if not rec:
        raise HTTPException(404, f"env not found: {env_id}")
    if rec.state == "terminated":
        raise HTTPException(410, f"env {env_id} is terminated")
    return rec


def _run_op(env_id: str, op_name: str) -> ExecResult:
    rec     = _env_or_404(env_id)
    handler = get_handler(rec.project)
    client  = _sandbox_client()
    method  = getattr(handler, op_name)
    outcome = method(client, rec.sandbox_meta)
    return ExecResult(
        ok=          outcome.ok,
        exit_code=   outcome.exit_code,
        duration_s=  outcome.duration_s,
        stdout_tail= outcome.stdout_tail,
        stderr_tail= outcome.stderr_tail,
    )


@app.post("/v1/envs/{env_id}/build",      response_model=ExecResult)
def op_build(env_id: str):       return _run_op(env_id, "build")

@app.post("/v1/envs/{env_id}/db/migrate", response_model=ExecResult)
def op_migrate(env_id: str):     return _run_op(env_id, "migrate")

@app.post("/v1/envs/{env_id}/app/start",  response_model=ExecResult)
def op_start_app(env_id: str):   return _run_op(env_id, "start_app")

@app.post("/v1/envs/{env_id}/app/wait_healthy", response_model=ExecResult)
def op_wait_healthy(env_id: str, timeout_s: int = 120):
    rec     = _env_or_404(env_id)
    handler = get_handler(rec.project)
    client  = _sandbox_client()
    outcome = handler.wait_healthy(client, rec.sandbox_meta, timeout_s=timeout_s)
    return ExecResult(
        ok=          outcome.ok,
        exit_code=   outcome.exit_code,
        duration_s=  outcome.duration_s,
        stdout_tail= outcome.stdout_tail,
        stderr_tail= outcome.stderr_tail,
    )

@app.post("/v1/envs/{env_id}/tests/run",  response_model=TestResult)
def op_tests(env_id: str):
    rec     = _env_or_404(env_id)
    handler = get_handler(rec.project)
    client  = _sandbox_client()
    outcome = handler.run_tests(client, rec.sandbox_meta)
    return TestResult(
        ok=          outcome.ok,
        exit_code=   outcome.exit_code,
        duration_s=  outcome.duration_s,
        stdout_tail= outcome.stdout_tail,
        stderr_tail= outcome.stderr_tail,
        artifacts=   outcome.artifacts,
    )

@app.post("/v1/envs/{env_id}/reset",      response_model=ExecResult)
def op_reset(env_id: str):       return _run_op(env_id, "reset")


# ─────────────────────── sidecars ───────────────────────


@app.post("/v1/envs/{env_id}/sidecars", response_model=SidecarSummary)
def op_attach_sidecar(env_id: str, req: AttachSidecarRequest):
    rec = _env_or_404(env_id)
    client = _sandbox_client()
    sidecar = attach_sidecar(client, env_id=env_id, parent_sbx=rec.sandbox_meta,
                             name=req.name, image=req.image, env=req.env,
                             timeout_s=req.timeout_s)
    return SidecarSummary(
        name=        sidecar.name,
        sandbox_id=  sidecar.sandbox_id,
        image=       sidecar.image,
        parent_env=  env_id,
        state=       sidecar.state,
    )


@app.delete("/v1/envs/{env_id}/sidecars/{name}", status_code=204)
def op_detach_sidecar(env_id: str, name: str):
    _env_or_404(env_id)
    client = _sandbox_client()
    detach_sidecar(client, env_id=env_id, name=name)


@app.get("/v1/envs/{env_id}/sidecars", response_model=list[SidecarSummary])
def list_sidecars(env_id: str):
    _env_or_404(env_id)
    return [
        SidecarSummary(
            name=s.name, sandbox_id=s.sandbox_id, image=s.image,
            parent_env=env_id, state=s.state,
        )
        for s in store.list_sidecars_for_env(env_id)
    ]


# ─────────────────────── pools ───────────────────────


pool_controller = PoolController(_sandbox_client_factory=_sandbox_client)


@app.post("/v1/pools/{name}", response_model=PoolStatus)
def create_pool(name: str, spec: PoolSpec):
    pool_controller.upsert(name=name, spec=spec)
    return pool_controller.status(name)


@app.get("/v1/pools/{name}", response_model=PoolStatus)
def get_pool(name: str):
    s = pool_controller.status(name)
    if not s:
        raise HTTPException(404, f"pool not found: {name}")
    return s


@app.get("/v1/pools", response_model=list[PoolStatus])
def list_pools():
    return [pool_controller.status(p.name) for p in store.list_pools()]


@app.delete("/v1/pools/{name}", status_code=204)
def delete_pool(name: str):
    pool_controller.drain(name)


# ─────────────────────── lifecycle ───────────────────────


@app.on_event("startup")
def on_startup():
    log.info("Runtime Manager starting; OpenSandbox=%s",
             os.environ.get("OPENSANDBOX_API", "http://opensandbox:8080"))
    pool_controller.start()


@app.on_event("shutdown")
def on_shutdown():
    log.info("Runtime Manager shutting down")
    pool_controller.stop()
    # Best-effort: tear down any envs we own. In production the orchestrator
    # would be tracking these, not us — this is just for clean local dev.
    for env in store.list_envs():
        try:
            _destroy(env.id)
        except Exception:
            pass
