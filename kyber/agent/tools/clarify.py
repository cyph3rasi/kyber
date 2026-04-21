"""Clarify tool for interactive clarifying questions.

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. The actual user-interaction logic lives in the platform layer.
"""

import json
from typing import Any

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry


MAX_CHOICES = 4


class ClarifyTool(Tool):
    """Ask the user a question when clarification is needed."""

    @property
    def name(self) -> str:
        return "clarify"

    @property
    def description(self) -> str:
        return (
            "Ask the user before proceeding when a decision is ambiguous. "
            "Pass up to 4 `choices` for multiple-choice, or omit for open-"
            "ended. Don't use for yes/no confirmation of risky shell "
            "commands — the shell tool handles that."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_CHOICES,
                    "description": "Up to 4 options; an 'Other' fallback is added automatically.",
                },
            },
            "required": ["question"],
        }

    @property
    def toolset(self) -> str:
        return "interaction"

    async def execute(self, question: str, choices: list[str] | None = None, **kwargs) -> str:
        callback = kwargs.get("clarify_callback")

        if not question or not question.strip():
            return json.dumps({"error": "Question text is required."}, ensure_ascii=False)

        question = question.strip()

        # Validate and trim choices
        if choices is not None:
            if not isinstance(choices, list):
                return json.dumps({"error": "choices must be a list of strings."}, ensure_ascii=False)
            choices = [str(c).strip() for c in choices if str(c).strip()]
            if len(choices) > MAX_CHOICES:
                choices = choices[:MAX_CHOICES]
            if not choices:
                choices = None

        if callback is None:
            return json.dumps(
                {"error": "Clarify tool is not available in this execution context."},
                ensure_ascii=False,
            )

        try:
            user_response = callback(question, choices)
        except Exception as exc:
            return json.dumps(
                {"error": f"Failed to get user input: {exc}"},
                ensure_ascii=False,
            )

        return json.dumps({
            "question": question,
            "choices_offered": choices,
            "user_response": str(user_response).strip(),
        }, ensure_ascii=False)


# Self-register
registry.register(ClarifyTool())
