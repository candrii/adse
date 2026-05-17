"""Per-project handlers.

Each handler encapsulates everything the Manager needs to know about a
project's workload: which image, where the code lives, how to migrate the
DB, how to start the app, what health endpoints to probe, what command runs
the tests, what state needs to be cleared on reset.

To add a new project: write a handler subclass and register it in
`HANDLERS`. The Manager's HTTP API doesn't need to change.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..sandbox import SandboxClient


# ─────────────────────── handler protocol ───────────────────────


class OperationError(Exception):
    """Raised by handlers when an operation fails. The Manager's HTTP layer
    converts this into a structured 4xx/5xx response."""
    def __init__(self, stage: str, message: str, exit_code: int = 1,
                 stderr_tail: str = "", stdout_tail: str = ""):
        self.stage       = stage
        self.message     = message
        self.exit_code   = exit_code
        self.stderr_tail = stderr_tail
        self.stdout_tail = stdout_tail
        super().__init__(f"{stage}: {message}")


@dataclass
class ExecOutcome:
    ok:          bool
    exit_code:   int
    duration_s:  float
    stdout_tail: str = ""
    stderr_tail: str = ""
    artifacts:   list[str] = field(default_factory=list)


class ProjectHandler(Protocol):
    """What every project handler implements. The Manager calls these in
    response to HTTP operations."""

    image:       str           # OpenSandbox image to spawn for this project
    secrets:     list[str]     # env var names the handler needs at activity time

    def env_for_create(self, ref: str | None) -> dict[str, str]: ...
    def build(self,         client: SandboxClient, sbx: dict[str, Any]) -> ExecOutcome: ...
    def migrate(self,       client: SandboxClient, sbx: dict[str, Any]) -> ExecOutcome: ...
    def start_app(self,     client: SandboxClient, sbx: dict[str, Any]) -> ExecOutcome: ...
    def wait_healthy(self,  client: SandboxClient, sbx: dict[str, Any], timeout_s: int = 120) -> ExecOutcome: ...
    def run_tests(self,     client: SandboxClient, sbx: dict[str, Any]) -> ExecOutcome: ...
    def reset(self,         client: SandboxClient, sbx: dict[str, Any]) -> ExecOutcome: ...


# ─────────────────────── helpers ───────────────────────


def _exec(client: SandboxClient, sbx: dict, cmd: str, timeout_s: int,
          stage: str, fail_code: int = 1) -> ExecOutcome:
    """Run a command in the sandbox; tail captured output; raise on failure."""
    started = time.time()
    result = client.exec_capture(sbx, cmd, timeout_s=timeout_s)
    duration = time.time() - started
    outcome = ExecOutcome(
        ok=          result["exit_code"] == 0,
        exit_code=   result["exit_code"],
        duration_s=  duration,
        stdout_tail= result["stdout"][-8_000:],
        stderr_tail= result["stderr"][-4_000:],
    )
    if not outcome.ok:
        raise OperationError(
            stage=       stage,
            message=     f"{cmd[:80]}… exited {outcome.exit_code}",
            exit_code=   fail_code,
            stderr_tail= outcome.stderr_tail,
            stdout_tail= outcome.stdout_tail,
        )
    return outcome


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ─────────────────────── eshop ───────────────────────


@dataclass
class EshopHandler:
    image:   str       = "ai-harness/eshop:latest"
    secrets: list[str] = field(default_factory=lambda: ["MSSQL_SA_PASSWORD"])
    repo:    str       = "/workspace/repo"
    contexts: list[str] = field(default_factory=lambda: ["CatalogContext", "AppIdentityDbContext"])
    health_paths: list[str] = field(default_factory=lambda:
                                    ["/home_page_health_check", "/api_health_check"])

    def env_for_create(self, ref: str | None) -> dict[str, str]:
        env = {
            "SQL_HOST":          "sqlserver",
            "MSSQL_SA_PASSWORD": _required_env("MSSQL_SA_PASSWORD"),
        }
        if ref:
            env["GIT_REF"] = ref
        return env

    def _db_name(self, sbx: dict) -> str:
        """Per-env database name namespace. Two DBs per env (Catalog, Identity)."""
        sid = sbx["id"].replace("-", "")[:12]
        return f"eshop_{sid}"

    def _conn_strings(self, sbx: dict) -> dict[str, str]:
        pw = _required_env("MSSQL_SA_PASSWORD")
        base = self._db_name(sbx)
        return {
            "ConnectionStrings__CatalogConnection":
                f"Server=sqlserver;Database={base}-Catalog;User=sa;Password={pw};TrustServerCertificate=true",
            "ConnectionStrings__IdentityConnection":
                f"Server=sqlserver;Database={base}-Identity;User=sa;Password={pw};TrustServerCertificate=true",
        }

    def build(self, client: SandboxClient, sbx: dict) -> ExecOutcome:
        _exec(client, sbx,
              f"cd {self.repo} && dotnet restore eShopOnWeb.sln",
              timeout_s=300, stage="restore", fail_code=10)
        return _exec(client, sbx,
                     f"cd {self.repo} && dotnet build eShopOnWeb.sln --no-restore -c Release",
                     timeout_s=600, stage="build", fail_code=10)

    def migrate(self, client: SandboxClient, sbx: dict) -> ExecOutcome:
        conn = self._conn_strings(sbx)
        env_exports = " ".join(f"export {k}={_shquote(v)};" for k, v in conn.items())
        last: ExecOutcome | None = None
        for ctx in self.contexts:
            cmd = (
                f"cd {self.repo} && {env_exports} "
                f"dotnet ef database update "
                f"--project src/Infrastructure --startup-project src/Web "
                f"--context {ctx}"
            )
            last = _exec(client, sbx, cmd, timeout_s=180,
                         stage=f"migrate_{ctx.lower()}", fail_code=30)
        return last  # type: ignore[return-value]

    def start_app(self, client: SandboxClient, sbx: dict) -> ExecOutcome:
        conn = self._conn_strings(sbx)
        env_exports = " ".join(f"export {k}={_shquote(v)};" for k, v in conn.items())
        # Background-start with nohup; record pid for later teardown.
        cmd = (
            f"cd {self.repo}/src/Web && {env_exports} "
            f"nohup dotnet run --no-build -c Release --urls http://0.0.0.0:5000 "
            f">/results/app.log 2>&1 & echo $! > /tmp/app.pid && "
            f'echo "started dotnet run pid=$(cat /tmp/app.pid)"'
        )
        return _exec(client, sbx, cmd, timeout_s=30, stage="app_start", fail_code=40)

    def wait_healthy(self, client: SandboxClient, sbx: dict, timeout_s: int = 120) -> ExecOutcome:
        """Loop in the sandbox so we don't burn O(N) round-trips. Polls both
        eShop health endpoints; bails if the dotnet process dies."""
        attempts = timeout_s // 2
        probe_paths = " ".join(_shquote(p) for p in self.health_paths)
        cmd = (
            f"pid=$(cat /tmp/app.pid 2>/dev/null) ;"
            f"for i in $(seq 1 {attempts}); do "
            f"  for p in {probe_paths}; do "
            f'    curl -fsS "http://localhost:5000$p" >/dev/null 2>&1 '
            f'      && echo "health ok after ${{i}}*2s on $p" && exit 0 ; '
            f"  done ; "
            f'  if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then '
            f'    echo "dotnet died early; last app.log:" >&2 ; '
            f"    tail -n 80 /results/app.log >&2 ; exit 40 ; "
            f"  fi ; "
            f"  sleep 2 ; "
            f"done ; "
            f'echo "health probe timed out; last app.log:" >&2 ; '
            f"tail -n 80 /results/app.log >&2 ; exit 40"
        )
        return _exec(client, sbx, cmd, timeout_s=timeout_s + 30,
                     stage="wait_healthy", fail_code=40)

    def run_tests(self, client: SandboxClient, sbx: dict) -> ExecOutcome:
        cmd = (
            f"cd {self.repo} && "
            f"dotnet test eShopOnWeb.sln --no-build -c Release "
            f'--logger "trx;LogFileName=results.trx" '
            f"--results-directory /results"
        )
        outcome = _exec(client, sbx, cmd, timeout_s=900, stage="test", fail_code=20)
        # Discover artifacts the test run produced
        ls = client.exec_capture(sbx,
            "find /results -maxdepth 2 -type f \\( -name '*.trx' -o -name '*.log' \\) 2>/dev/null",
            timeout_s=5)
        outcome.artifacts = [p for p in ls["stdout"].splitlines() if p.strip()]
        return outcome

    def reset(self, client: SandboxClient, sbx: dict) -> ExecOutcome:
        """Drop the per-env DBs and kill any background app. Doesn't touch
        the sandbox itself — caller decides whether to return it to the pool."""
        pw = _required_env("MSSQL_SA_PASSWORD")
        base = self._db_name(sbx)
        drop_sql = (
            f"ALTER DATABASE [{base}-Catalog]  SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
            f"DROP DATABASE [{base}-Catalog]; "
            f"ALTER DATABASE [{base}-Identity] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
            f"DROP DATABASE [{base}-Identity];"
        )
        cmd = (
            f"kill $(cat /tmp/app.pid 2>/dev/null) 2>/dev/null ; "
            f"rm -f /tmp/app.pid /results/* ; "
            f'sqlcmd -S sqlserver -U sa -P {_shquote(pw)} -C -No '
            f'-Q "{drop_sql}" 2>/dev/null || true'
        )
        # Reset is best-effort — non-fatal if it partly fails
        result = client.exec_capture(sbx, cmd, timeout_s=60)
        return ExecOutcome(
            ok=True,   # reset is advisory
            exit_code=  result["exit_code"],
            duration_s= 0.0,
            stdout_tail=result["stdout"][-2000:],
            stderr_tail=result["stderr"][-2000:],
        )


# ─────────────────────── medplum (stub for now) ───────────────────────


@dataclass
class MedplumHandler:
    image:   str       = "ai-harness/medplum:latest"
    secrets: list[str] = field(default_factory=lambda: ["POSTGRES_PASSWORD"])

    def env_for_create(self, ref: str | None) -> dict[str, str]:
        env = {
            "PG_HOST":           "postgres",
            "REDIS_HOST":        "redis",
            "POSTGRES_PASSWORD": _required_env("POSTGRES_PASSWORD"),
        }
        if ref:
            env["GIT_REF"] = ref
        return env

    def build(self, client: SandboxClient, sbx: dict) -> ExecOutcome:
        raise OperationError("build", "medplum handler not yet implemented", exit_code=60)

    def migrate(self, *a, **kw):     raise OperationError("migrate", "stub", exit_code=60)
    def start_app(self, *a, **kw):   raise OperationError("start_app", "stub", exit_code=60)
    def wait_healthy(self, *a, **kw): raise OperationError("wait_healthy", "stub", exit_code=60)
    def run_tests(self, *a, **kw):   raise OperationError("run_tests", "stub", exit_code=60)
    def reset(self, *a, **kw):       raise OperationError("reset", "stub", exit_code=60)


# ─────────────────────── helpers ───────────────────────


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise OperationError(stage="env", message=f"${name} not set in Manager env",
                             exit_code=60)
    return val


# ─────────────────────── registry ───────────────────────


HANDLERS: dict[str, ProjectHandler] = {
    "eshop":   EshopHandler(),   # type: ignore[dict-item]
    "medplum": MedplumHandler(), # type: ignore[dict-item]
}


def get_handler(project: str) -> ProjectHandler:
    if project not in HANDLERS:
        raise OperationError(stage="handler", message=f"unknown project: {project}", exit_code=60)
    return HANDLERS[project]
