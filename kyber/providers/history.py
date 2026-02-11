"""Utilities to convert between Kyber session dicts and PydanticAI ModelMessage types.

Kyber sessions store messages as ``[{"role": "user", "content": "..."}, ...]``.
PydanticAI expects typed ``ModelMessage`` objects (``ModelRequest`` / ``ModelResponse``).
This module bridges the two representations.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    RetryPromptPart,
    SystemPromptPart,
    ToolReturnPart,
    UserPromptPart,
)


def dicts_to_model_messages(
    messages: list[dict[str, Any]],
) -> list[ModelMessage]:
    """Convert a list of Kyber session dicts to PydanticAI ``ModelMessage`` objects.

    Args:
        messages: Session history in ``{"role": ..., "content": ...}`` format.
            Extra keys (e.g. ``timestamp``) are silently ignored.

    Returns:
        A list of ``ModelRequest`` / ``ModelResponse`` objects suitable for
        PydanticAI's ``message_history`` parameter.
    """
    result: list[ModelMessage] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if content is None:
            logger.warning("Skipping message with None content (role={})", role)
            continue

        if role == "user":
            result.append(
                ModelRequest(parts=[UserPromptPart(content=str(content))])
            )
        elif role == "assistant":
            result.append(
                ModelResponse(parts=[TextPart(content=str(content))])
            )
        elif role == "system":
            logger.warning(
                "Skipping system message in history conversion — "
                "system prompts should be passed via the 'instructions' parameter"
            )
        else:
            logger.warning("Skipping message with unknown role: {}", role)

    return result


def model_messages_to_dicts(
    messages: list[ModelRequest | ModelResponse],
) -> list[dict[str, str]]:
    """Convert PydanticAI ``ModelMessage`` objects back to Kyber session dicts.

    This is the reverse of :func:`dicts_to_model_messages`, used for backward
    compatibility when session history needs to be persisted in dict format.

    Args:
        messages: PydanticAI message objects.

    Returns:
        A list of ``{"role": "user"|"assistant", "content": "..."}`` dicts.
    """
    result: list[dict[str, str]] = []

    for msg in messages:
        if isinstance(msg, ModelRequest):
            text_parts: list[str] = []
            for part in msg.parts:
                if isinstance(part, UserPromptPart):
                    # content can be str or Sequence[UserContent]; handle str
                    if isinstance(part.content, str):
                        text_parts.append(part.content)
                    else:
                        # Multi-modal content — extract text segments
                        for item in part.content:
                            if isinstance(item, str):
                                text_parts.append(item)
                elif isinstance(part, (SystemPromptPart, RetryPromptPart, ToolReturnPart)):
                    # Skip non-user-text parts
                    continue
                else:
                    logger.debug("Skipping unknown request part type: {}", type(part).__name__)

            if text_parts:
                result.append({"role": "user", "content": " ".join(text_parts)})

        elif isinstance(msg, ModelResponse):
            text_parts_resp: list[str] = []
            for part in msg.parts:
                if isinstance(part, TextPart):
                    text_parts_resp.append(part.content)
                elif isinstance(part, ToolCallPart):
                    # Skip tool calls — only extract text
                    continue
                else:
                    logger.debug("Skipping unknown response part type: {}", type(part).__name__)

            if text_parts_resp:
                result.append({"role": "assistant", "content": " ".join(text_parts_resp)})

        else:
            logger.warning("Skipping unknown message type: {}", type(msg).__name__)

    return result

