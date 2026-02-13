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
    # IMPORTANT: These must be specific enough to avoid false-positives on
    # normal conversational output. Overly broad needles (e.g. "rules",
    # "markdown", "instructions") cause voice generation to reject valid
    # messages and exhaust retries, leading to dropped updates/completions.
    needles = [
        "system prompt",
        "system message",
        "developer message",
        "role: system",
        "role: assistant",
        "role: user",
        "role: tool",
        "assistant:",
        "tool call",
        "tool_calls",
        "no markdown",
        "no emojis",
        "no code blocks",
        "just the response",
        "just a brief update",
        "begin_patch",
        "end_patch",
        "```",
        "<instructions>",
        "</instructions>",
    ]
    lower = t.lower()
    if any(n in lower for n in needles):
        return True

    # High-signal instruction leaks that show up in background/meta messages.
    # Keep this list tight: false positives here are expensive because they can
    # suppress normal conversational phrasing (e.g., "don't worry").
    high_signal = [
        "no more tool calls",
        "do not use any more tools",
        "do not use any tools",
        "do not call any tools",
        "tools are disabled",
        "stay in character",  # often part of meta-instructions rather than user content
        "as you finish",
        "tell the user exactly what to do next",
        "if you used a virtual environment",
        "use the apply_patch tool",
        "use apply_patch",
    ]
    if any(n in lower for n in high_signal):
        return True

    # If it contains many quotes/brackets, it's often copying prompt text.
    # Threshold must be high enough to allow legitimate structured output
    # (task results with quoted strings, JSON snippets, etc.).
    if t.count('"') >= 20 or t.count("'") >= 20:
        return True

    return False


def clean_one_liner(text: str) -> str:
    t = (text or "").strip().replace("`", "")
    if not t:
        return ""
    return t.splitlines()[0].strip()


def looks_like_robotic_meta(text: str) -> bool:
    """
    Catch overly-formal/robotic meta-updates like "I will now proceed...".

    These are not leaks, but they sound bad in chat. Prefer falling back to
    in-character templates or retrying once with a better prompt.
    """
    t = " ".join((text or "").split()).strip().lower()
    if not t:
        return True

    needles = [
        "i will now",
        "i will proceed",
        "proceed with",
        "the requested",
        "requested execution",
        "requested operation",
        "execute the requested",
        "run the requested",
        "requested code",
        "requested command",
        "to provide the results",
        "to get those results",
        "to get the results",
        "requested execution for you",
        "requested operation to",
    ]
    return any(n in t for n in needles)


def tool_action_hint(tool_name: str) -> str:
    """Friendly, non-technical hint for the LLM about what's next."""
    tool = (tool_name or "").strip()
    mapping: dict[str, str] = {
        "read_file": "read the file",
        "list_dir": "scan the directory",
        "write_file": "write the file",
        "edit_file": "apply the edit",
        "exec": "run a command",
        "web_search": "search the web",
        "web_fetch": "fetch the page",
        "message": "send a message",
        "spawn": "kick it off",
        "task_status": "check status",
    }
    return mapping.get(tool, "continue")


def build_tool_status_text(tool_name: str) -> str:
    """Deterministic status update template (safe fallback)."""
    tool = (tool_name or "").strip()
    mapping: dict[str, str] = {
        "read_file": "checking the relevant file now.",
        "list_dir": "looking through the folder now.",
        "write_file": "writing the file changes now.",
        "edit_file": "applying the edit to the file now.",
        "exec": "running a command to confirm now.",
        "web_search": "searching the web for you now.",
        "web_fetch": "pulling up the page content now.",
        "message": "sending the message for you now.",
        "spawn": "kicking off the background work now.",
        "task_status": "checking on the task progress now.",
    }
    return mapping.get(tool, "working on the task for you now.")


def describe_tool_action(tool_name: str, tense: str = "present") -> str:
    """Human description of what a tool *does*, avoiding internal tool names."""
    tool = (tool_name or "").strip()
    tense = (tense or "present").strip().lower()
    is_past = tense in {"past", "done", "completed"}

    present: dict[str, str] = {
        "read_file": "reading a file",
        "list_dir": "looking through a folder",
        "write_file": "writing a file",
        "edit_file": "editing a file",
        "exec": "running a shell command",
        "web_search": "searching the web",
        "web_fetch": "opening a web page",
        "message": "sending a message",
        "spawn": "kicking work off",
        "task_status": "checking status",
    }

    past: dict[str, str] = {
        "read_file": "read a file",
        "list_dir": "looked through a folder",
        "write_file": "wrote a file",
        "edit_file": "edited a file",
        "exec": "ran a shell command",
        "web_search": "searched the web",
        "web_fetch": "opened a web page",
        "message": "sent a message",
        "spawn": "kicked work off",
        "task_status": "checked status",
    }

    if is_past:
        return past.get(tool, "made progress")
    return present.get(tool, "making progress")


def build_offload_ack_fallback() -> str:
    return (
        "Still on it. Keep chatting if you want, and I'll drop the result as soon as it's ready."
    )
