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
            "Ask the user a question when you need clarification, feedback, or a "
            "decision before proceeding. Supports two modes:\n\n"
            "1. **Multiple choice** — provide up to 4 choices. The user picks one "
            "or types their own answer via a 5th 'Other' option.\n"
            "2. **Open-ended** — omit choices entirely. The user types a free-form "
            "response.\n\n"
            "Use this tool when:\n"
            "- The task is ambiguous and you need the user to choose an approach\n"
            "- You want post-task feedback ('How did that work out?')\n"
            "- You want to offer to save a skill or update memory\n"
            "- A decision has meaningful trade-offs the user should weigh in on\n\n"
            "Do NOT use this tool for simple yes/no confirmation of dangerous "
            "commands (the terminal tool handles that). Prefer making a reasonable "
            "default choice yourself when the decision is low-stakes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to present to the user."
                },
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_CHOICES,
                    "description": (
                        "Up to 4 answer choices. Omit this parameter entirely to "
                        "ask an open-ended question. When provided, the UI "
                        "automatically appends an 'Other (type your answer)' option."
                    ),
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
