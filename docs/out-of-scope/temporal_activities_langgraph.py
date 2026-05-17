"""LangGraph-driven agent loop, as a Temporal activity.

Same shape as `temporal_activities_agentic.py` (Claude drives the sandbox
until done), but the agent loop is implemented with LangGraph's prebuilt
ReAct agent instead of a hand-rolled `messages.create` loop.

What changes:
  - Loop, state, and tool dispatch live in LangGraph (`create_react_agent`).
  - The LLM is `ChatAnthropic` from `langchain-anthropic`.
  - Tools are `@tool`-decorated Python functions; LangChain handles JSON
    schema generation and argument validation.

What stays the same:
  - The execution layer is still OpenSandbox via `SandboxClient`.
  - Temporal still owns the activity lifecycle (timeouts, cancellation,
    heartbeats, sandbox teardown).
  - The result shape (`AgentResult`) is identical so the two activities
    are interchangeable from the workflow's perspective.

Trade-offs vs. the direct Anthropic SDK path:
  - Less code in the activity (LangGraph handles the loop).
  - LangChain dependency weight (~50 MB additional in the worker image).
  - Prompt caching is not as ergonomic — `langchain-anthropic` supports
    it via `cache_control` on message blocks, but `create_react_agent`
    builds messages internally so we don't get per-call cache_control
    placement without subclassing. For v0 we accept the slight cost
    inefficiency; document as a known limitation.
  - Token usage extraction goes through LangChain's `usage_metadata`,
    not Anthropic's native `usage` object.
"""
from __future__ import annotations

import base64
import os
import time
from typing import Any

from temporalio import activity

from .sandbox import SandboxClient
from .temporal_activities_agentic import AgentTask, AgentResult, SYSTEM_PROMPT


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _make_tools(client: SandboxClient, sbx: dict):
    """Bind a sandbox to a fresh set of @tool-decorated functions.

    LangChain's @tool decorator builds the input schema from the function
    signature and docstring. Keep docstrings model-facing — Claude reads
    them when deciding which tool to call.
    """
    from langchain_core.tools import tool

    @tool
    def bash(command: str, timeout_s: int = 60) -> str:
        """Execute a shell command in the sandbox via `bash -lc`.

        Returns formatted text containing exit_code, stdout (last 16 KB),
        and stderr (last 4 KB). Use for running tests, builds, git — any
        shell-shaped work.

        Args:
            command:   Shell command. Runs via `bash -lc`.
            timeout_s: Wall-clock timeout in seconds. Default 60.
        """
        result = client.exec_capture(sbx, command, timeout_s=int(timeout_s))
        return (
            f"exit_code: {result['exit_code']}\n"
            f"--- stdout (last 16 KB) ---\n{result['stdout'][-16_000:]}\n"
            f"--- stderr (last 4 KB) ---\n{result['stderr'][-4_000:]}"
        )

    @tool
    def read_file(path: str) -> str:
        """Read a UTF-8 file from the sandbox by absolute path.

        Returns up to ~64 KB of content. Prefer this over `cat` when you
        only need the bytes — fewer escaping bugs than going through bash.

        Args:
            path: Absolute path inside the sandbox.
        """
        result = client.exec_capture(
            sbx,
            f"cat {_shell_quote(path)} 2>&1 | head -c 65536",
            timeout_s=15,
        )
        if result["exit_code"] != 0:
            return f"read_file failed: {result['stderr'][:1000]}"
        return result["stdout"]

    @tool
    def write_file(path: str, content: str) -> str:
        """Write UTF-8 content to a file in the sandbox.

        Creates parent directories. Overwrites existing files. Use for
        code edits — much more reliable than echo/heredoc through bash.

        Args:
            path:    Absolute path inside the sandbox.
            content: UTF-8 file content.
        """
        b64 = base64.b64encode(content.encode("utf-8")).decode()
        cmd = (
            f'mkdir -p "$(dirname {_shell_quote(path)})" && '
            f'echo {_shell_quote(b64)} | base64 -d > {_shell_quote(path)}'
        )
        result = client.exec_capture(sbx, cmd, timeout_s=30)
        if result["exit_code"] != 0:
            return f"write_file failed: {result['stderr'][:1000]}"
        return f"wrote {len(content)} bytes to {path}"

    return [bash, read_file, write_file]


def _sandbox_client() -> SandboxClient:
    return SandboxClient(
        os.environ.get("OPENSANDBOX_API", "http://opensandbox:8080"),
        os.environ.get("OPEN_SANDBOX_API_KEY"),
    )


@activity.defn(name="langgraph_ticket")
async def langgraph_ticket(task: AgentTask) -> AgentResult:
    """Drive a LangGraph ReAct agent over an OpenSandbox sandbox.

    Streams agent events so we can heartbeat per iteration. Aggregates
    token usage from the streamed messages' `usage_metadata`. Tears the
    sandbox down in `finally` regardless of outcome.
    """
    # Imports inside the function: keeps module-level import time low for
    # other activities, and avoids touching LangChain at all unless this
    # specific activity is invoked.
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langgraph.prebuilt import create_react_agent

    sbx_client = _sandbox_client()
    started    = time.time()

    activity.logger.info("langgraph: creating sandbox image=%s", task.image)
    sbx = sbx_client.create(
        image=           task.image,
        env=             task.env,
        timeout_s=       task.timeout_s,
        resource_limits= task.resource_limits,
        metadata={
            "temporal_workflow_id": activity.info().workflow_id,
            "temporal_activity_id": activity.info().activity_id,
            "agent_framework":      "langgraph",
        },
    )
    sandbox_id = sbx["id"]
    activity.heartbeat({"stage": "sandbox_created", "sandbox_id": sandbox_id})

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
        llm = ChatAnthropic(
            model=          task.model,
            max_tokens=     16000,
            timeout=        None,
        )

        agent_graph = create_react_agent(
            llm,
            tools= _make_tools(sbx_client, sbx),
            prompt= SYSTEM_PROMPT,
        )

        # Stream so we can heartbeat per node visit and accumulate usage
        # from each AIMessage as it comes off the graph. `recursion_limit`
        # is LangGraph's ceiling on graph-node visits — we set it to a
        # safe multiple of `max_iterations` since a single agent turn can
        # produce several node visits (call → tool → call → ...).
        config = {"recursion_limit": max(task.max_iterations * 3, 25)}
        inputs = {"messages": [HumanMessage(content=task.ticket)]}

        async for chunk in agent_graph.astream(inputs, config=config, stream_mode="updates"):
            iteration += 1
            activity.heartbeat({"stage": "node", "iteration": iteration, "usage": totals})

            # chunk shape: {node_name: {"messages": [<new messages>]}}
            for node_name, node_state in (chunk or {}).items():
                for msg in node_state.get("messages", []):
                    # Aggregate token usage from each LLM call
                    usage_meta = getattr(msg, "usage_metadata", None) or {}
                    if usage_meta:
                        totals["input_tokens"]  += usage_meta.get("input_tokens", 0)
                        totals["output_tokens"] += usage_meta.get("output_tokens", 0)
                        # langchain-anthropic surfaces cache stats under
                        # input_token_details when present
                        details = usage_meta.get("input_token_details") or {}
                        totals["cache_creation_input_tokens"] += details.get("cache_creation", 0)
                        totals["cache_read_input_tokens"]     += details.get("cache_read",     0)
                    # Capture the latest assistant text for the result
                    if isinstance(msg, AIMessage):
                        # AIMessage.content can be a string or a list of blocks
                        if isinstance(msg.content, str):
                            last_text = msg.content
                        elif isinstance(msg.content, list):
                            for block in msg.content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    last_text = block.get("text", last_text)

        stop_reason = "end_turn"

    except Exception as exc:
        activity.logger.exception("langgraph loop failed")
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
        final_message= str(last_text)[:32_000],
        duration_s=    time.time() - started,
        usage=         totals,
        error=         error_msg,
    )
