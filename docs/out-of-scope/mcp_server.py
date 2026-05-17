#!/usr/bin/env python3
"""
MCP server adapter — exposes the sandbox as a Model Context Protocol tool source.

This is the "headless agent runtime with orchestrating interface" piece. Any
MCP-aware agent (Claude Code, etc.) configures this as an MCP server in their
client config; the sandbox then shows up as a set of native tools the agent
calls like any other tool.

The server is bound to a single session at startup (one process per session).
That keeps state simple and matches how AI agents actually work — they want
*one* workspace they iterate against, not a registry to look up.

Wire protocol: stdio JSON-RPC per the MCP spec. Run via:

    python3 -m harness.mcp_server --session <name>

Tools exposed to the agent:

    bash(cmd, timeout=60)         — run a command, get stdout+stderr+exit
    read_file(path)               — read a file from the workspace
    write_file(path, content)     — write a file in the workspace
    apply_patch(diff)             — apply a unified diff via `git apply`
    run_pipeline()                — invoke the full runner.sh, return result.json
    list_artifacts()              — list files under /results
    reset_workspace()             — `git reset --hard` + clean (start over)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

# Reuse the existing client + session store from sandbox.py — single source of
# truth for "how do we talk to OpenSandbox."
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sandbox import SandboxClient, _load_sessions, _required_env, DEFAULT_API  # noqa: E402

try:
    from mcp.server import Server          # type: ignore
    from mcp.server.stdio import stdio_server  # type: ignore
    from mcp.types import TextContent, Tool   # type: ignore
except ImportError:
    sys.exit(
        "error: mcp package not installed. Run: pip install 'mcp>=1.0'\n"
        "       (or: pip install -r harness/requirements.txt)"
    )


# ─────────────────────────── session binding ───────────────────────────


def resolve_session(name: str) -> tuple[SandboxClient, str]:
    sessions = _load_sessions()
    session = sessions.get(name)
    if not session:
        sys.exit(
            f"error: no such session '{name}'. "
            f"Start one with: sandbox session start --project <p> --name {name}"
        )
    client = SandboxClient(
        os.environ.get("OPENSANDBOX_API", DEFAULT_API),
        _required_env("OPEN_SANDBOX_API_KEY"),
    )
    return client, session["sandbox_id"]


# ─────────────────────────── tool implementations ───────────────────────────
#
# Each tool is a thin wrapper over an OpenSandbox exec. The contract back to
# the agent is JSON — easier for the LLM to reason about than free-text logs.


async def tool_bash(client: SandboxClient, sandbox_id: str, args: dict[str, Any]) -> dict[str, Any]:
    cmd = args["cmd"]
    timeout = int(args.get("timeout", 60))

    # We use a small Python-level capture rather than client.exec's stream-to-
    # stdout behavior — the MCP tool result is the entire output, not a stream.
    import io, contextlib
    buf = io.StringIO()
    saved_stdout, sys.stdout = sys.stdout, buf
    saved_stderr, sys.stderr = sys.stderr, buf
    try:
        exit_code = await asyncio.to_thread(
            client.exec, sandbox_id,
            ["bash", "-lc", f"timeout {timeout} {cmd}"],
            None, True,
        )
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr

    return {
        "exit_code": exit_code,
        "output":    buf.getvalue()[-32_000:],   # cap at ~32 KB to avoid blowing the context
        "truncated": len(buf.getvalue()) > 32_000,
    }


async def tool_read_file(client: SandboxClient, sandbox_id: str, args: dict[str, Any]) -> dict[str, Any]:
    path = args["path"]
    # base64 round-trip handles binary; agents usually want text but this is safer
    return await tool_bash(client, sandbox_id, {
        "cmd": f'cat {_q(path)}',
        "timeout": 10,
    })


async def tool_write_file(client: SandboxClient, sandbox_id: str, args: dict[str, Any]) -> dict[str, Any]:
    import base64
    path = args["path"]
    content_b64 = base64.b64encode(args["content"].encode("utf-8")).decode()
    cmd = f'mkdir -p "$(dirname {_q(path)})" && echo {_q(content_b64)} | base64 -d > {_q(path)}'
    return await tool_bash(client, sandbox_id, {"cmd": cmd, "timeout": 30})


async def tool_apply_patch(client: SandboxClient, sandbox_id: str, args: dict[str, Any]) -> dict[str, Any]:
    import base64
    diff_b64 = base64.b64encode(args["diff"].encode("utf-8")).decode()
    cmd = (
        f'cd /workspace/repo && '
        f'echo {_q(diff_b64)} | base64 -d > /tmp/agent.patch && '
        f'git apply --whitespace=fix /tmp/agent.patch'
    )
    return await tool_bash(client, sandbox_id, {"cmd": cmd, "timeout": 60})


async def tool_run_pipeline(client: SandboxClient, sandbox_id: str, _args: dict[str, Any]) -> dict[str, Any]:
    res = await tool_bash(client, sandbox_id, {"cmd": "/usr/local/bin/runner.sh", "timeout": 1800})
    # Surface the structured result.json alongside the log tail.
    result = await tool_bash(client, sandbox_id, {"cmd": "cat /results/result.json", "timeout": 5})
    try:
        res["result_json"] = json.loads(result["output"])
    except json.JSONDecodeError:
        res["result_json"] = None
    return res


async def tool_list_artifacts(client: SandboxClient, sandbox_id: str, _args: dict[str, Any]) -> dict[str, Any]:
    return await tool_bash(client, sandbox_id, {"cmd": "ls -la /results", "timeout": 5})


async def tool_reset_workspace(client: SandboxClient, sandbox_id: str, _args: dict[str, Any]) -> dict[str, Any]:
    return await tool_bash(client, sandbox_id, {
        "cmd":     "cd /workspace/repo && git reset --hard HEAD && git clean -fdx",
        "timeout": 30,
    })


def _q(s: str) -> str:
    """Single-quote shell-escape."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ─────────────────────────── MCP server wiring ───────────────────────────


TOOLS: dict[str, dict[str, Any]] = {
    "bash": {
        "description": "Execute a shell command in the sandbox. Returns stdout/stderr + exit code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd":     {"type": "string", "description": "Shell command to run (via bash -lc)"},
                "timeout": {"type": "integer", "description": "Wall-clock seconds (default 60)", "default": 60},
            },
            "required": ["cmd"],
        },
        "handler": tool_bash,
    },
    "read_file": {
        "description": "Read a text file from the sandbox workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path inside sandbox"}},
            "required": ["path"],
        },
        "handler": tool_read_file,
    },
    "write_file": {
        "description": "Write a UTF-8 text file in the sandbox workspace. Creates parent dirs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        "handler": tool_write_file,
    },
    "apply_patch": {
        "description": "Apply a unified diff (git apply) to the cloned repo at /workspace/repo.",
        "input_schema": {
            "type": "object",
            "properties": {"diff": {"type": "string", "description": "Unified diff text"}},
            "required": ["diff"],
        },
        "handler": tool_apply_patch,
    },
    "run_pipeline": {
        "description": "Run the full project pipeline (build, migrate, test, health). Returns result.json + log tail.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_run_pipeline,
    },
    "list_artifacts": {
        "description": "List files in /results (build logs, JUnit XML, etc.).",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_list_artifacts,
    },
    "reset_workspace": {
        "description": "Reset the workspace to a clean checkout (git reset --hard + clean).",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_reset_workspace,
    },
}


def build_server(session_name: str) -> Server:
    client, sandbox_id = resolve_session(session_name)
    server = Server("ai-harness-sandbox")

    @server.list_tools()                       # type: ignore[misc]
    async def _list() -> list[Tool]:
        return [
            Tool(name=name, description=spec["description"], inputSchema=spec["input_schema"])
            for name, spec in TOOLS.items()
        ]

    @server.call_tool()                        # type: ignore[misc]
    async def _call(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        spec = TOOLS.get(name)
        if not spec:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
        try:
            result = await spec["handler"](client, sandbox_id, arguments)
        except Exception as exc:  # surface as tool error rather than crashing server
            result = {"error": str(exc), "type": type(exc).__name__}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


async def main_async(session_name: str) -> None:
    server = build_server(session_name)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True,
                        help="Session name (from `sandbox session start`)")
    args = parser.parse_args()
    asyncio.run(main_async(args.session))


if __name__ == "__main__":
    main()
