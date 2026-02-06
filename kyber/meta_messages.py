"""
User-visible "meta messages" (tool status updates, long-running acks).

These are intentionally small and dependency-light so they can be tested
without importing the full agent stack.
"""

from __future__ import annotations

import os


def llm_meta_messages_enabled() -> bool:
    """
    Allow LLM-authored meta-messages (tool status + long-running acknowledgements).

    If you're ever seeing prompt/instruction leakage in status updates, set
    `KYBER_LLM_META_MESSAGES=0` to force deterministic templates.
    """
    v = (os.environ.get("KYBER_LLM_META_MESSAGES", "1") or "1").strip().lower()
    return v not in {"0", "false", "no", "off"}


def looks_like_prompt_leak(text: str) -> bool:
    """
    Best-effort guard against leaking instructions/prompts.

    This intentionally prefers false-positives over letting obvious leaks through.
    """
    t = " ".join((text or "").split()).strip()
    if not t:
        return True

    # Common prompt/instruction markers.
    needles = [
        "system prompt",
        "system message",
        "developer message",
        "role:",
        "assistant:",
        "user:",
        "tool:",
        "tools:",
        "tool call",
        "tool_calls",
        "do not",
        "don't",
        "rules",
        "instructions",
        "no markdown",
        "markdown",
        "no emojis",
        "no code",
        "just the response",
        "just a brief update",
        "begin",
        "end",
        "```",
        "<instructions>",
        "</instructions>",
    ]
    lower = t.lower()
    if any(n in lower for n in needles):
        return True

    # If it contains lots of quotes/brackets, it's often copying prompt text.
    if t.count('"') >= 2 or t.count("'") >= 4:
        return True

    return False


def clean_one_liner(text: str) -> str:
    t = (text or "").strip().replace("`", "")
    if not t:
        return ""
    return t.splitlines()[0].strip()


def build_tool_status_text(tool_name: str) -> str:
    """Deterministic status update template (safe fallback)."""
    tool = (tool_name or "").strip()
    mapping: dict[str, str] = {
        "read_file": "On it, checking the relevant file next.",
        "list_dir": "Taking a quick look through the folder next.",
        "write_file": "Making that change now.",
        "edit_file": "Applying the edit now.",
        "exec": "Running a quick command to confirm things.",
        "web_search": "Looking that up now.",
        "web_fetch": "Pulling that page up now.",
        "message": "Sending that over now.",
        "spawn": "Kicking this off in the background now.",
        "task_status": "Checking on progress now.",
    }
    return mapping.get(tool, "On it, working on that now.")


def build_offload_ack_fallback() -> str:
    return (
        "Still working on that in the background. "
        "Feel free to keep chatting, I'll send the result when it's ready."
    )
