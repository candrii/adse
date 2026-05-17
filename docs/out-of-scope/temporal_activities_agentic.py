"""LLM-driven agent loop, wrapped as a Temporal activity.

The orchestrator (Temporal) owns retries, durability, cancellation. The agent
loop owns: ask Claude what to do → execute the tool call inside an
OpenSandbox sandbox → feed result back → repeat until the agent says it's
done or we hit the iteration ceiling.

What this file deliberately does:

  - Defaults to claude-opus-4-7 (latest, adaptive thinking).
  - Uses top-level cache_control to cache the stable prefix (tools + system
    prompt + accumulated turns) — only new content costs full input tokens
    after iteration 0. Verify via the `usage.cache_read_input_tokens` field
    on the returned AgentResult.
  - Appends the FULL `response.content` (thinking + tool_use + text blocks,
    with thinking signatures intact) back to messages. Appending only the
    text loses tool state and silently invalidates the cache.
  - Heartbeats to Temporal every iteration so the orchestrator can cancel
    mid-loop and so a stuck activity is detectable.
  - Tears the sandbox down in `finally` even on partial failure.

What it deliberately doesn't:

  - No durable agent state across activity retries. If the worker crashes
    mid-loop, the next attempt starts from scratch (Temporal restarts the
    activity, the sandbox is gone, the messages list is empty). For
    crash-resilient agent loops, persist `messages` to a workflow variable
    or to memory — out of scope for the v0 demo.
  - No tool result truncation strategy beyond hard byte caps. A noisy build
    log can still chew through the context window over many iterations.
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic
from temporalio import activity

from .sandbox import SandboxClient


# ─────────────────────────── tool surface ───────────────────────────
#
# Three primitives are enough for code-fixing work. bash is the workhorse;
# read_file / write_file exist because passing big strings through bash
# heredocs is fragile (escaping, exit-code conflation) and because dedicated
# tools let the agent reason about file operations without thinking about
# shell quoting.

TOOLS: list[dict[str, Any]] = [
    {
        "name": "bash",
        "description": (
            "Execute a shell command in the sandbox. Use for running tests, builds, "
            "git, anything shell-shaped. Returns exit_code, stdout, stderr (both "
            "truncated to recent output)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command":   {"type": "string", "description": "Shell command, run via `bash -lc`."},
                "timeout_s": {"type": "integer", "description": "Wall-clock timeout. Default 60."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a UTF-8 file from the sandbox by absolute path. Capped at ~64 KB "
            "of returned content. Use this instead of `cat` when you only need the bytes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path inside the sandbox."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write UTF-8 content to a file in the sandbox. Creates parent directories. "
            "Overwrites existing files. Use this for code edits — much more reliable "
            "than echo/heredoc through bash."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Absolute path inside the sandbox."},
                "content": {"type": "string", "description": "UTF-8 file content."},
            },
            "required": ["path", "content"],
        },
    },
]


SYSTEM_PROMPT = """You are a software engineering agent working in a sandboxed Linux container.

Available tools:
- bash(command, timeout_s=60) — run shell commands
- read_file(path) — read a UTF-8 file by absolute path
- write_file(path, content) — create/overwrite a UTF-8 file (creates parent dirs)

The workspace is at /workspace. When a repo is mounted it usually lives at /workspace/repo.

Your job: complete the user's ticket. Work iteratively — explore the codebase, make focused changes, run tests, fix what breaks. End your turn (no further tool calls) only when the work is done or you've concluded you can't complete it.

Conventions:
- Prefer read_file / write_file over cat / echo for code I/O — fewer escaping bugs.
- Keep diffs minimal and focused on the ticket. Don't refactor adjacent code.
- Run the project's test suite to verify your changes before finishing.
- If a command fails, read the actual error before guessing — the sandbox is real, the failure is real.
"""


# ─────────────────────────── data contracts ───────────────────────────


@dataclass
class AgentTask:
    """What a workflow asks the agentic activity to do."""
    ticket:          str                                 # the task description
    image:           str                                 # sandbox image to spawn
    timeout_s:       int = 1800
    max_iterations:  int = 25
    model:           str = "claude-opus-4-7"
    env:             dict[str, str] = field(default_factory=dict)
    resource_limits: dict[str, str] = field(default_factory=lambda: {"cpu": "2", "memory": "4Gi"})


@dataclass
class AgentResult:
    """Compact result. We deliberately don't return the full message history —
    Temporal serializes this into the workflow's event log, and a 30-iteration
    loop with tool outputs can easily be 1 MB+. Keep it small."""
    sandbox_id:    str
    iterations:    int
    stop_reason:   str               # "end_turn" | "max_iterations" | "max_tokens" | "refusal" | "error" | ...
    final_message: str               # last assistant text block, truncated
    duration_s:    float
    usage:         dict[str, int]    # input / output / cache_creation / cache_read (totals)
    error:         str | None = None


# ─────────────────────────── helpers ───────────────────────────


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _dispatch_tool(client: SandboxClient, sbx: dict, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute one tool call inside the sandbox. Returns {content, is_error}.

    The content is what gets fed back to the LLM as a tool_result block —
    keep it small enough that 20 iterations don't blow the context window.
    """
    if name == "bash":
        cmd       = args["command"]
        timeout_s = int(args.get("timeout_s", 60))
        result    = client.exec_capture(sbx, cmd, timeout_s=timeout_s)
        body = (
            f"exit_code: {result['exit_code']}\n"
            f"--- stdout (last 16 KB) ---\n{result['stdout'][-16_000:]}\n"
            f"--- stderr (last 4 KB) ---\n{result['stderr'][-4_000:]}"
        )
        return {"content": body, "is_error": result["exit_code"] != 0}

    if name == "read_file":
        path = args["path"]
        # head -c 65536 caps the response; without this a multi-MB log would
        # blow through both the context and the activity's stdout buffer.
        result = client.exec_capture(
            sbx,
            f"cat {_shell_quote(path)} 2>&1 | head -c 65536",
            timeout_s=15,
        )
        if result["exit_code"] != 0:
            return {"content": f"read_file failed: {result['stderr'][:1000]}", "is_error": True}
        return {"content": result["stdout"], "is_error": False}

    if name == "write_file":
        path    = args["path"]
        content = args["content"]
        # base64 round-trip is the only escape-safe way to ship arbitrary
        # UTF-8 (including newlines, quotes, backslashes) through a shell.
        b64 = base64.b64encode(content.encode("utf-8")).decode()
        cmd = (
            f'mkdir -p "$(dirname {_shell_quote(path)})" && '
            f'echo {_shell_quote(b64)} | base64 -d > {_shell_quote(path)}'
        )
        result = client.exec_capture(sbx, cmd, timeout_s=30)
        if result["exit_code"] != 0:
            return {"content": f"write_file failed: {result['stderr'][:1000]}", "is_error": True}
        return {"content": f"wrote {len(content)} bytes to {path}", "is_error": False}

    return {"content": f"unknown tool: {name}", "is_error": True}


def _sandbox_client() -> SandboxClient:
    return SandboxClient(
        os.environ.get("OPENSANDBOX_API", "http://opensandbox:8080"),
        os.environ.get("OPEN_SANDBOX_API_KEY"),
    )


def _llm_client() -> anthropic.Anthropic:
    # SDK reads ANTHROPIC_API_KEY from env. Worker container must have it set.
    return anthropic.Anthropic()


# ─────────────────────────── the activity ───────────────────────────


@activity.defn(name="agentic_ticket")
async def agentic_ticket(task: AgentTask) -> AgentResult:
    """Drive an LLM agent loop inside a sandbox.

    Lifecycle: create sandbox → loop(call LLM → execute tool calls → repeat)
    until stop_reason==end_turn or max_iterations → delete sandbox.

    Prompt caching: top-level `cache_control={"type":"ephemeral"}` auto-places
    a breakpoint on the last cacheable block of each request. Tools and the
    system prompt sit at positions 0–1 in the rendered prefix and are stable
    across iterations, so the cache hit rate climbs after iteration 0. Watch
    `usage.cache_read_input_tokens` in the returned result — if it stays at
    zero, something is mutating the prefix (don't put timestamps in the
    system prompt, don't reorder tools).
    """
    sbx_client = _sandbox_client()
    llm        = _llm_client()
    started    = time.time()

    activity.logger.info("agent: creating sandbox image=%s", task.image)
    sbx = sbx_client.create(
        image=           task.image,
        env=             task.env,
        timeout_s=       task.timeout_s,
        resource_limits= task.resource_limits,
        metadata= {
            "temporal_workflow_id": activity.info().workflow_id,
            "temporal_activity_id": activity.info().activity_id,
            "agent_task":           "true",
        },
    )
    sandbox_id = sbx["id"]
    activity.heartbeat({"stage": "sandbox_created", "sandbox_id": sandbox_id})

    messages: list[dict[str, Any]] = [{"role": "user", "content": task.ticket}]
    totals = {
        "input_tokens":                0,
        "output_tokens":               0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens":     0,
    }
    last_text   = ""
    stop_reason = "max_iterations"
    error_msg: str | None = None
    iteration   = 0

    try:
        for iteration in range(1, task.max_iterations + 1):
            activity.heartbeat({"stage": "llm_call", "iteration": iteration, "usage": totals})

            response = llm.messages.create(
                model=         task.model,
                max_tokens=    16000,
                system=        SYSTEM_PROMPT,
                tools=         TOOLS,
                messages=      messages,
                cache_control= {"type": "ephemeral"},
                thinking=      {"type": "adaptive"},
            )

            u = response.usage
            totals["input_tokens"]                += u.input_tokens
            totals["output_tokens"]               += u.output_tokens
            totals["cache_creation_input_tokens"] += (u.cache_creation_input_tokens or 0)
            totals["cache_read_input_tokens"]     += (u.cache_read_input_tokens or 0)

            # Pull the latest assistant text for the final result. The agent
            # may emit thinking + text + tool_use blocks; we want the text.
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    last_text = block.text

            # CRITICAL: append the FULL content list, not just the text.
            # The API needs original blocks back (with thinking signatures
            # preserved) for the next iteration. Stripping to text breaks
            # both the protocol and the cache.
            messages.append({"role": "assistant", "content": response.content})

            sr = response.stop_reason
            if sr == "end_turn":
                stop_reason = "end_turn"
                break
            if sr in ("max_tokens", "refusal"):
                stop_reason = sr
                break
            if sr == "pause_turn":
                # Server-side tool hit its internal step limit. Re-send to
                # continue — no new user message needed.
                continue
            if sr == "tool_use":
                # Execute every tool_use block in the assistant turn. All
                # tool results must come back in a SINGLE user message — the
                # API rejects partial result sets.
                tool_results = []
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        out = _dispatch_tool(sbx_client, sbx, block.name, dict(block.input))
                        tool_results.append({
                            "type":         "tool_result",
                            "tool_use_id":  block.id,
                            "content":      out["content"],
                            "is_error":     out["is_error"],
                        })
                messages.append({"role": "user", "content": tool_results})
                continue

            # Anything else — treat as terminal so we don't loop forever.
            stop_reason = str(sr) if sr else "unknown"
            break

    except Exception as exc:  # surface as an error in the result rather than crashing the activity
        activity.logger.exception("agent loop failed")
        stop_reason = "error"
        error_msg   = f"{type(exc).__name__}: {exc}"

    finally:
        try:
            sbx_client.delete(sandbox_id)
        except Exception as exc:
            activity.logger.warning("teardown failed for %s: %s", sandbox_id, exc)

    return AgentResult(
        sandbox_id=    sandbox_id,
        iterations=    iteration,
        stop_reason=   stop_reason,
        final_message= last_text[:32_000],
        duration_s=    time.time() - started,
        usage=         totals,
        error=         error_msg,
    )
