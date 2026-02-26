"""Delegate Tool -- Subagent Architecture for Kyber.

Spawns child AgentCore instances with isolated context, restricted toolsets,
and their own terminal sessions. Supports single-task and batch (parallel)
modes. The parent blocks until all children complete.

Each child gets:
  - A fresh conversation (no parent history)
  - Its own session (isolated terminal session)
  - A restricted toolset (blocked tools always stripped)
  - A focused system prompt built from the delegated goal + context

The parent's context only sees the delegation call and the summary result,
never the child's intermediate tool calls or reasoning.
"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry


logger = logging.getLogger(__name__)

# Tools that children must never have access to
DELEGATE_BLOCKED_TOOLS = frozenset([
    "delegate_task",   # no recursive delegation
    "clarify",         # no user interaction
    "memory",          # no writes to shared MEMORY.md
    "send_message",    # no cross-platform side effects
])

MAX_CONCURRENT_CHILDREN = 3
MAX_DEPTH = 2  # parent (0) -> child (1) -> grandchild rejected (2)
DEFAULT_MAX_ITERATIONS = 0
DEFAULT_TOOLSETS = ["terminal", "file", "web"]


def _build_child_system_prompt(goal: str, context: str | None = None) -> str:
    """Build a focused system prompt for a child agent."""
    parts = [
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    parts.append(
        "\nComplete this task using the tools available to you. "
        "When finished, provide a clear, concise summary of:\n"
        "- What you did\n"
        "- What you found or accomplished\n"
        "- Any files you created or modified\n"
        "- Any issues encountered\n\n"
        "Be thorough but concise -- your response is returned to the "
        "parent agent as a summary."
    )
    return "\n".join(parts)


def _get_toolsets_for_child(toolsets: list[str] | None) -> list[str]:
    """Get allowed toolsets for child, stripping blocked ones."""
    if toolsets is None:
        toolsets = DEFAULT_TOOLSETS
    # Filter out toolsets that are completely blocked
    blocked_toolset_names = {"delegation", "clarify", "memory"}
    return [t for t in toolsets if t not in blocked_toolset_names]


async def _run_single_child(
    task_index: int,
    goal: str,
    context: str | None,
    toolsets: list[str] | None,
    model: str | None,
    max_iterations: int,
    parent_core,  # AgentCore instance
) -> dict[str, Any]:
    """
    Spawn and run a single child agent.
    Returns a structured result dict.
    """
    from kyber.agent.core import AgentCore
    from kyber.bus.queue import MessageBus
    
    child_start = time.monotonic()
    
    child_toolsets = _get_toolsets_for_child(toolsets)
    child_prompt = _build_child_system_prompt(goal, context)
    
    try:
        # Create a fresh bus for the child (isolated)
        child_bus = MessageBus()
        
        # Create child AgentCore with restricted toolsets
        child = AgentCore(
            bus=child_bus,
            provider=parent_core.provider,
            workspace=parent_core.workspace,
            model=model or parent_core.model,
            max_iterations=max_iterations,
            persona_prompt=child_prompt,
            timezone=getattr(parent_core.context, '_timezone', None),
        )
        
        # Set delegation depth so children can't spawn grandchildren
        child._delegate_depth = getattr(parent_core, '_delegate_depth', 0) + 1
        
        # Run the child agent directly
        result = await child.process_direct(
            content=goal,
            session_key=f"subagent:{task_index}",
            channel="internal",
            chat_id=f"subagent-{task_index}",
        )
        
        duration = round(time.monotonic() - child_start, 2)
        
        if result and result.strip():
            status = "completed"
        else:
            status = "failed"
        
        return {
            "task_index": task_index,
            "status": status,
            "summary": result or "",
            "duration_seconds": duration,
        }

    except Exception as exc:
        duration = round(time.monotonic() - child_start, 2)
        logger.exception(f"[subagent-{task_index}] failed")
        return {
            "task_index": task_index,
            "status": "error",
            "summary": None,
            "error": str(exc),
            "duration_seconds": duration,
        }


class DelegateTool(Tool):
    """Spawn subagents to work on tasks in isolated contexts."""

    toolset = "delegation"

    @property
    def name(self) -> str:
        return "delegate_task"

    @property
    def description(self) -> str:
        return (
            "Spawn one or more subagents to work on tasks in isolated contexts. "
            "Each subagent gets its own conversation, terminal session, and toolset. "
            "Only the final summary is returned -- intermediate tool results "
            "never enter your context window.\n\n"
            "TWO MODES (one of 'goal' or 'tasks' is required):\n"
            "1. Single task: provide 'goal' (+ optional context, toolsets)\n"
            "2. Batch (parallel): provide 'tasks' array with up to 3 items. "
            "All run concurrently and results are returned together.\n\n"
            "WHEN TO USE delegate_task:\n"
            "- Reasoning-heavy subtasks (debugging, code review, research synthesis)\n"
            "- Tasks that would flood your context with intermediate data\n"
            "- Parallel independent workstreams (research A and B simultaneously)\n\n"
            "WHEN NOT TO USE (use these instead):\n"
            "- Mechanical multi-step work with no reasoning needed -> use execute_code\n"
            "- Single tool call -> just call the tool directly\n"
            "- Tasks needing user interaction -> subagents cannot use clarify\n\n"
            "IMPORTANT:\n"
            "- Subagents have NO memory of your conversation. Pass all relevant "
            "info (file paths, error messages, constraints) via the 'context' field.\n"
            "- Subagents CANNOT call: delegate_task, clarify, memory, send_message.\n"
            "- Each subagent gets its own terminal session (separate working directory and state).\n"
            "- Results are always returned as an array, one entry per task."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": (
                        "What the subagent should accomplish. Be specific and "
                        "self-contained -- the subagent knows nothing about your "
                        "conversation history."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Background information the subagent needs: file paths, "
                        "error messages, project structure, constraints. The more "
                        "specific you are, the better the subagent performs."
                    ),
                },
                "toolsets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Toolsets to enable for this subagent. "
                        "Default: ['terminal', 'file', 'web']. "
                        "Common patterns: ['terminal', 'file'] for code work, "
                        "['web'] for research, ['terminal', 'file', 'web'] for "
                        "full-stack tasks."
                    ),
                },
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string", "description": "Task goal"},
                            "context": {"type": "string", "description": "Task-specific context"},
                            "toolsets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Toolsets for this specific task",
                            },
                        },
                        "required": ["goal"],
                    },
                    "maxItems": 3,
                    "description": (
                        "Batch mode: up to 3 tasks to run in parallel. Each gets "
                        "its own subagent with isolated context and terminal session. "
                        "When provided, top-level goal/context/toolsets are ignored."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Model override for the subagent(s). Omit to use your "
                        "same model. Use a cheaper/faster model for simple subtasks."
                    ),
                },
                "max_iterations": {
                    "type": "integer",
                    "description": (
                        "Max tool-calling turns per subagent (default: 0 = unlimited). "
                        "Use a positive value to enforce a hard cap."
                    ),
                },
            },
            "required": [],
        }

    async def execute(
        self,
        goal: str | None = None,
        context: str | None = None,
        toolsets: list[str] | None = None,
        tasks: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_iterations: int | None = None,
        **kwargs
    ) -> str:
        # Get parent agent from kwargs
        parent_core = kwargs.get("agent_core")
        if parent_core is None:
            return json.dumps({"error": "delegate_task requires agent context."})

        # Depth limit
        depth = getattr(parent_core, '_delegate_depth', 0)
        if depth >= MAX_DEPTH:
            return json.dumps({
                "error": (
                    f"Delegation depth limit reached ({MAX_DEPTH}). "
                    "Subagents cannot spawn further subagents."
                )
            })

        effective_max_iter = DEFAULT_MAX_ITERATIONS if max_iterations is None else max_iterations
        if effective_max_iter < 0:
            effective_max_iter = 0

        # Normalize to task list
        if tasks and isinstance(tasks, list):
            task_list = tasks[:MAX_CONCURRENT_CHILDREN]
        elif goal and isinstance(goal, str) and goal.strip():
            task_list = [{"goal": goal, "context": context, "toolsets": toolsets}]
        else:
            return json.dumps({"error": "Provide either 'goal' (single task) or 'tasks' (batch)."})

        if not task_list:
            return json.dumps({"error": "No tasks provided."})

        # Validate each task has a goal
        for i, task in enumerate(task_list):
            if not task.get("goal", "").strip():
                return json.dumps({"error": f"Task {i} is missing a 'goal'."})

        overall_start = time.monotonic()
        results = []

        n_tasks = len(task_list)

        if n_tasks == 1:
            # Single task -- run directly
            t = task_list[0]
            result = await _run_single_child(
                task_index=0,
                goal=t["goal"],
                context=t.get("context"),
                toolsets=t.get("toolsets") or toolsets,
                model=model,
                max_iterations=effective_max_iter,
                parent_core=parent_core,
            )
            results.append(result)
        else:
            # Batch -- run in parallel
            async_tasks = []
            for i, t in enumerate(task_list):
                async_task = _run_single_child(
                    task_index=i,
                    goal=t["goal"],
                    context=t.get("context"),
                    toolsets=t.get("toolsets") or toolsets,
                    model=model,
                    max_iterations=effective_max_iter,
                    parent_core=parent_core,
                )
                async_tasks.append(async_task)
            
            # Run all tasks concurrently
            results = await asyncio.gather(*async_tasks, return_exceptions=True)
            
            # Process results
            processed_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    processed_results.append({
                        "task_index": i,
                        "status": "error",
                        "summary": None,
                        "error": str(result),
                        "duration_seconds": 0,
                    })
                else:
                    processed_results.append(result)
            results = processed_results

            # Sort by task_index so results match input order
            results.sort(key=lambda r: r["task_index"])

        total_duration = round(time.monotonic() - overall_start, 2)

        return json.dumps({
            "results": results,
            "total_duration_seconds": total_duration,
        }, ensure_ascii=False)


# Self-register
registry.register(DelegateTool())
