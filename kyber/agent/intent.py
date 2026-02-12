"""
Structured intent schema for LLM responses.

The LLM declares what it wants to happen, the system executes.
This prevents hallucination - the LLM can't claim actions it didn't declare.
"""

import json
import re
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
    """Structured intent extracted from LLM response.

    DEPRECATED: This class is kept for backward compatibility with legacy parsing.
    New code should use the flattened AgentResponse fields directly.
    """
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
    """Structured response from the LLM.

    Flattened schema for better structured output reliability.
    Intent fields are now direct properties instead of nested.
    """
    message: str = Field(
        description="Natural language response to send to the user"
    )
    # Intent fields (flattened from nested Intent object)
    action: IntentAction = Field(
        default=IntentAction.NONE,
        description="Action: none, spawn_task, check_status, cancel_task"
    )
    task_description: str | None = Field(
        default=None,
        description="For spawn_task: what the task should do"
    )
    task_label: str | None = Field(
        default=None,
        description="For spawn_task: short label"
    )
    task_ref: str | None = Field(
        default=None,
        description="For check_status/cancel_task: the reference"
    )
    complexity: str | None = Field(
        default=None,
        description="For spawn_task: simple, moderate, or complex"
    )

    @property
    def intent(self) -> Intent:
        """Backward compatibility property for legacy code accessing response.intent."""
        return Intent(
            action=self.action,
            task_description=self.task_description,
            task_label=self.task_label,
            task_ref=self.task_ref,
            complexity=self.complexity,
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
    """Parse a tool call response into AgentResponse.

    Handles both legacy nested format (with 'intent' object) and direct format.
    """
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

    # Check if this is legacy nested format with 'intent' object
    intent_data = args.get("intent", {})
    if isinstance(intent_data, str):
        try:
            intent_data = json.loads(intent_data)
        except (json.JSONDecodeError, ValueError):
            intent_data = {}
    if not isinstance(intent_data, dict):
        intent_data = {}

    # Parse action (from intent object or direct field)
    action_raw = intent_data.get("action", "none") if intent_data else args.get("action", "none")
    if isinstance(action_raw, str):
        action_raw = action_raw.strip().lower()
    try:
        action = IntentAction(action_raw)
    except Exception:
        action = IntentAction.NONE

    # Extract fields (prefer intent object if present, otherwise direct fields)
    if intent_data:
        # Legacy nested format
        task_description = intent_data.get("task_description")
        task_label = intent_data.get("task_label")
        task_ref = intent_data.get("task_ref")
        complexity = intent_data.get("complexity")
    else:
        # Direct flattened format
        task_description = args.get("task_description")
        task_label = args.get("task_label")
        task_ref = args.get("task_ref")
        complexity = args.get("complexity")

    return AgentResponse(
        message=args.get("message", ""),
        action=action,
        task_description=task_description,
        task_label=task_label,
        task_ref=task_ref,
        complexity=complexity,
    )


def parse_response_content(content: str) -> AgentResponse | None:
    """
    Parse a raw assistant content string into AgentResponse when it contains
    a JSON envelope (e.g., {"message": "...", "intent": {...}}).
    """
    text = (content or "").strip()
    if not text:
        return None

    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
    if fence:
        fenced_obj = (fence.group(1) or "").strip()
        if fenced_obj:
            candidates.insert(0, fenced_obj)

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(payload, dict) and ("message" in payload or "intent" in payload):
            try:
                return parse_tool_call(payload)
            except Exception:
                continue

    return None
