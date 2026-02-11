"""
Task Planner: Generate a concrete execution plan in ONE LLM call,
then execute steps deterministically without going back to the LLM
between each one.

This is the key insight: most tasks follow predictable patterns.
"Create a Python script that does X" is always:
  1. Maybe read some existing files for context
  2. Write the file
  3. Maybe run it to verify

The old approach: LLM call → tool → LLM call → tool → LLM call → tool → LLM call (done)
                  = 4 LLM round-trips, each waiting 2-5 seconds

The new approach: LLM call (plan) → execute all steps → LLM call (summarize if needed)
                  = 1-2 LLM round-trips total

The planner asks the LLM to emit a structured plan (JSON array of tool calls),
then the executor runs them all without going back to the LLM. If a step fails
or produces unexpected output, THEN we fall back to the adaptive loop.
"""

import json
from typing import Any

from loguru import logger

from kyber.providers.base import LLMProvider, LLMResponse

# The tool the LLM uses to emit its plan
PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "execute_plan",
        "description": (
            "Submit a concrete execution plan. Each step is a tool call that will be "
            "executed in order. Steps can reference results of previous steps using "
            "${step_N} syntax in string arguments (e.g., ${step_1} refers to the "
            "output of step 1). If any step fails, the system will ask you to adapt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "thinking": {
                    "type": "string",
                    "description": "Brief reasoning about the approach (1-2 sentences)",
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "enum": [
                                    "read_file", "write_file", "edit_file",
                                    "list_dir", "exec", "web_search", "web_fetch",
                                ],
                            },
                            "args": {
                                "type": "object",
                                "description": "Tool arguments. Use ${step_N} to reference output of step N.",
                            },
                            "purpose": {
                                "type": "string",
                                "description": "What this step accomplishes (shown to user as progress)",
                            },
                        },
                        "required": ["tool", "args", "purpose"],
                    },
                    "description": "Ordered list of tool calls to execute",
                },
                "summary_template": {
                    "type": "string",
                    "description": (
                        "Template for the final message to the user. "
                        "Use ${step_N} to include relevant outputs. "
                        "Write this in character, naturally."
                    ),
                },
            },
            "required": ["steps", "summary_template"],
        },
    },
}


class ExecutionPlan:
    """A parsed execution plan from the LLM."""

    def __init__(
        self,
        steps: list[dict[str, Any]],
        summary_template: str,
        thinking: str = "",
    ):
        self.steps = steps
        self.summary_template = summary_template
        self.thinking = thinking

    @classmethod
    def from_tool_call(cls, arguments: dict[str, Any]) -> "ExecutionPlan":
        return cls(
            steps=arguments.get("steps", []),
            summary_template=arguments.get("summary_template", "Done."),
            thinking=arguments.get("thinking", ""),
        )

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def __repr__(self) -> str:
        return f"ExecutionPlan({self.step_count} steps)"


async def generate_plan(
    provider: LLMProvider,
    model: str,
    system_prompt: str,
    task_description: str,
    workspace_index: str = "",
) -> ExecutionPlan | None:
    """
    Ask the LLM to generate an execution plan in a single call.

    Returns None if the LLM can't/won't produce a plan (falls back to
    the adaptive loop).
    """
    # Augment the system prompt with planning instructions
    planning_prompt = f"""{system_prompt}

## Planning Mode

You are in PLANNING MODE. Instead of executing tools one at a time, you will
submit a complete execution plan upfront. The system will execute all steps
automatically and report back.

Think about what tools you need and in what order. Be concrete — specify exact
file paths, exact commands, exact content.

{f"## Workspace Overview{chr(10)}{workspace_index}" if workspace_index else ""}

IMPORTANT:
- Your plan should DO the work, not just investigate. If the user wants something run, the plan should run it. If something is broken, the plan should fix it AND verify the fix.
- Be specific in every step. Don't say "read the relevant file" — say which file.
- For write_file, include the COMPLETE file content in args.content.
- For exec, include the exact command.
- For edit_file, include exact old_text and new_text.
- Steps execute in order. Use ${{step_N}} to reference earlier results.
- If you're unsure what files exist, start with list_dir or read_file steps.
- Keep plans focused. 1-8 steps is ideal.
- The summary_template should describe what was DONE, not what was found. Keep it under 800 characters. Do NOT include raw file contents or command output in the template.
"""

    messages = [
        {"role": "system", "content": planning_prompt},
        {"role": "user", "content": task_description},
    ]

    try:
        response = await provider.chat(
            messages=messages,
            tools=[PLAN_TOOL],
            model=model,
            temperature=0.3,
        )

        if response.has_tool_calls:
            for tc in response.tool_calls:
                if tc.name == "execute_plan":
                    plan = ExecutionPlan.from_tool_call(tc.arguments)
                    if plan.steps:
                        logger.info(
                            f"Generated plan: {plan.step_count} steps | "
                            f"thinking: {plan.thinking[:100]}"
                        )
                        return plan

        logger.warning("LLM did not produce a valid plan, falling back to adaptive loop")
        return None

    except Exception as e:
        logger.warning(f"Plan generation failed, falling back to adaptive loop: {e}")
        return None


def interpolate_refs(text: str, results: dict[int, str]) -> str:
    """Replace ${step_N} references with actual results."""
    import re

    def _replace(match: re.Match) -> str:
        step_num = int(match.group(1))
        return results.get(step_num, f"(step {step_num} result unavailable)")

    return re.sub(r"\$\{step_(\d+)\}", _replace, text)


def interpolate_args(args: dict[str, Any], results: dict[int, str]) -> dict[str, Any]:
    """Recursively interpolate ${step_N} references in tool arguments."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str):
            out[k] = interpolate_refs(v, results)
        elif isinstance(v, dict):
            out[k] = interpolate_args(v, results)
        elif isinstance(v, list):
            out[k] = [
                interpolate_refs(item, results) if isinstance(item, str) else item
                for item in v
            ]
        else:
            out[k] = v
    return out
