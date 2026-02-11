"""
Structured intent schema for LLM responses.

The LLM declares what it wants to happen, the system executes.
This prevents hallucination - the LLM can't claim actions it didn't declare.
"""

from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class IntentAction(str, Enum):
    """Actions the LLM can request the system to perform."""
    NONE = "none"  # Pure conversation, no action needed
    SPAWN_TASK = "spawn_task"  # Start a background task
    CHECK_STATUS = "check_status"  # Check task status (specific or all)
    CANCEL_TASK = "cancel_task"  # Cancel a running task


class Intent(BaseModel):
    """Structured intent extracted from LLM response."""
    action: IntentAction = Field(
        default=IntentAction.NONE,
        description="What action the system should take"
    )
    task_description: str | None = Field(
        default=None,
        description="For spawn_task: what the task should do"
    )
    task_label: str | None = Field(
        default=None,
        description="For spawn_task: short human-readable label"
    )
    task_ref: str | None = Field(
        default=None,
        description="For check_status/cancel_task: the reference to look up"
    )
    complexity: str | None = Field(
        default=None,
        description=(
            "For spawn_task: estimated complexity. "
            "'simple' (check a file, quick lookup), "
            "'moderate' (multi-step investigation, small build), "
            "'complex' (deep debugging, multi-service analysis, large builds)"
        ),
    )


class AgentResponse(BaseModel):
    """Structured response from the LLM."""
    message: str = Field(
        description="Natural language response to send to the user"
    )
    intent: Intent = Field(
        default_factory=Intent,
        description="What the LLM wants the system to do"
    )


# Tool definition for function calling
RESPOND_TOOL = {
    "type": "function",
    "function": {
        "name": "respond",
        "description": (
            "Respond to the user and optionally request an action. "
            "Use this for EVERY response. If you want to start a task, "
            "set intent.action to 'spawn_task' and provide task_description. "
            "If the user asks about status, set intent.action to 'check_status'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Your natural language response to the user"
                },
                "intent": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["none", "spawn_task", "check_status", "cancel_task"],
                            "description": (
                                "What action to take. Use 'spawn_task' when the user wants "
                                "you to DO something (create, build, fix, write, install, etc.). "
                                "Use 'check_status' when they ask about progress or status. "
                                "Use 'none' for pure conversation."
                            )
                        },
                        "task_description": {
                            "type": "string",
                            "description": "For spawn_task: detailed description of what to do"
                        },
                        "task_label": {
                            "type": "string",
                            "description": "For spawn_task: short label (e.g., 'HN Scraper', 'Bug Fix')"
                        },
                        "task_ref": {
                            "type": "string",
                            "description": "For check_status/cancel_task: the ⚡ or ✅ reference"
                        }
                    },
                    "required": ["action"]
                }
            },
            "required": ["message", "intent"]
        }
    }
}


def parse_tool_call(tool_call: Any) -> AgentResponse:
    """Parse a tool call response into AgentResponse."""
    import json

    if hasattr(tool_call, 'arguments'):
        args = tool_call.arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, ValueError):
                args = {}
    else:
        args = tool_call

    # Guard: if args is still not a dict after parsing, wrap it
    if not isinstance(args, dict):
        args = {}

    intent_data = args.get("intent", {})
    if isinstance(intent_data, str):
        try:
            intent_data = json.loads(intent_data)
        except (json.JSONDecodeError, ValueError):
            intent_data = {}
    if not isinstance(intent_data, dict):
        intent_data = {}

    intent = Intent(
        action=IntentAction(intent_data.get("action", "none")),
        task_description=intent_data.get("task_description"),
        task_label=intent_data.get("task_label"),
        task_ref=intent_data.get("task_ref"),
        complexity=intent_data.get("complexity"),
    )

    return AgentResponse(
        message=args.get("message", ""),
        intent=intent,
    )
