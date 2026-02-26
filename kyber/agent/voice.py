"""
Character Voice: All messages come from the bot's personality.

No system messages, no robotic status dumps. Everything sounds
like the character is talking naturally.
"""

import random
from typing import Any

from loguru import logger

from kyber.meta_messages import looks_like_prompt_leak, looks_like_robotic_meta




class CharacterVoice:
    """
    Transforms raw information into character voice.
    Every message the user sees passes through this layer.
    """
    
    # Templates for fast-path messages (no LLM call needed)
    PROGRESS_TEMPLATES = [
        "Still at it — {summary}",
        "Making progress — {summary}",
        "Chugging along — {summary}",
        "Quick update — {summary}",
        "Working on it — {summary}",
    ]
    
    COMPLETION_TEMPLATES = [
        "Done! {summary}",
        "Finished up — {summary}",
        "All set! {summary}",
        "Wrapped that up — {summary}",
        "Got it done — {summary}",
    ]
    
    FAILURE_TEMPLATES = [
        "Hit a snag — {summary}",
        "Ran into an issue — {summary}",
        "Something went wrong — {summary}",
        "Couldn't finish that — {summary}",
    ]
    
    def __init__(self, persona_prompt: str, llm_provider: Any, model: str | None = None):
        self.persona = persona_prompt
        self.llm = llm_provider
        # Ensure we always target the same model as the main agent; some providers
        # can behave oddly if model is omitted (returning empty content/tool_calls).
        try:
            self.model = model or llm_provider.get_default_model()
        except Exception:
            self.model = model
    
    async def speak(
        self,
        content: str,
        context: str | None = None,
        must_include: list[str] | None = None,
        use_llm: bool = True,
        strict_llm: bool = False,
    ) -> str:
        """
        Transform content into character voice.

        Args:
            content: The raw information to convey
            context: Optional context (e.g., "progress update", "task completion")
            must_include: Strings that MUST appear in output (references, etc.)
            use_llm: Whether to use LLM for generation (False = template only)

        Returns:
            The message in character voice
        """
        if not use_llm:
            # Only used in exceptional situations; keep behavior simple.
            return (content or "").strip()

        # Retry a few times. Voice is user-facing; we prefer to regenerate rather
        # than ship prompt leaks / empty / robotic meta output.
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                result = await self._speak_with_llm(content, context, must_include)
                result = (result or "").strip()
                if not result:
                    raise RuntimeError("Voice generation returned empty output")
                if looks_like_prompt_leak(result):
                    raise RuntimeError("Voice generation produced prompt/instruction leakage")
                if strict_llm and looks_like_robotic_meta(result):
                    raise RuntimeError("Voice generation produced robotic meta phrasing")
                return result
            except Exception as e:
                last_err = e
                # Nudge away from repeating instructions on subsequent attempts.
                if attempt < 2:
                    context = (
                        (context or "general message")
                        + " (Important: output only the user-facing message. Exclude any meta-instructions, role labels, or tool policy.)"
                    )
                continue

        raise last_err or RuntimeError("Voice generation failed")
    
    async def _speak_with_llm(
        self,
        content: str,
        context: str | None,
        must_include: list[str] | None,
    ) -> str:
        """Use LLM to generate in-character message.

        Uses plain-text mode (no tool-calling) so the provider can route
        directly through its text generation path.  Previous tool-calling
        approach (``say(message=...)``) was incompatible with the PydanticAI
        provider which converts tool calls into AgentResponse structured
        output, yielding ``respond`` instead of ``say``.
        """
        system = f"""You are speaking as this character:

    {self.persona}

    Rules:
    - Stay completely in character
    - Be concise (prefer 1-3 short sentences; for progress/status, a short list is fine)
    - Sound natural and conversational, not robotic
    - Don't mention being an AI or break character
    - Don't use phrases like "I've completed" or "Task completed" - be more natural"""

        if must_include:
            system += f"\n- You MUST include these exact strings somewhere in your response: {must_include}"
        system += "\n- Never fabricate task references like 'Ref: ⚡...'. Only include references that already appear in the provided content."

        system += "\n- Respond ONLY with the final user-facing message. No preamble, no labels, no tool syntax."

        user_msg = f"Convey this naturally, in character:\n\n{content}\n\nContext: {context or 'general message'}"

        response = await self.llm.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            model=getattr(self, "model", None),
        )

        result = (response.content or "").strip()

        if not result:
            logger.warning(
                f"Voice generation returned empty content | "
                f"finish_reason={getattr(response, 'finish_reason', None)!r}"
            )

        # Verify must_include strings are present — but only if we actually
        # got a real result. Appending refs to an empty string creates a
        # zombie result (" ✅ref") that passes the empty check but fails
        # substance checks downstream, causing infinite retry loops.
        if result and must_include:
            for s in must_include:
                if s not in result:
                    result = f"{result} {s}"

        return result
    
    async def speak_task_started(self, label: str, reference: str) -> str:
        """Generate in-character task start acknowledgment."""
        return await self.speak(
            content=f"Starting work on: {label}",
            context="acknowledging new task, be enthusiastic but brief",
            must_include=[reference],
        )
    
    async def speak_progress(self, summaries: list[str]) -> str:
        """Generate in-character progress update (LLM-backed; no canned fallbacks)."""
        if not summaries:
            return await self.speak(
                content="Still working on it.",
                context="progress update",
                use_llm=True,
                strict_llm=True,
            )

        if len(summaries) == 1:
            payload = summaries[0]
        else:
            payload = "\n".join(f"- {s}" for s in summaries)

        def _bad_progress(text: str) -> bool:
            t = (text or "").strip()
            if not t:
                return True
            lower = t.lower()
            # Absolutely never allow header-y "Progress update:" text.
            if "progress update:" in lower:
                return True
            # Never allow internal tool identifiers in user-visible progress pings.
            import re
            forbidden_words = [
                r"\bread_file\b",
                r"\bwrite_file\b",
                r"\bedit_file\b",
                r"\blist_dir\b",
                r"\bweb_search\b",
                r"\bweb_fetch\b",
                r"\btool_call\b",
                r"\btool_calls\b",
            ]
            for pat in forbidden_words:
                if re.search(pat, lower):
                    return True
            # NOTE: looks_like_prompt_leak / looks_like_robotic_meta are already
            # checked inside speak() — don't double-filter here. Double-filtering
            # was causing valid output to be rejected on the second pass, exhausting
            # all retry contexts and dropping the update entirely.
            return False

        contexts = [
            (
                "progress update (background ping). "
                "Be natural and informative. "
                "Use ONLY the facts provided; do not invent next steps. "
                "Do NOT use any headers. "
                "Do NOT say internal tool names (exec/read_file/etc.)."
            ),
            (
                "progress update (background ping). "
                "One short in-character update, 1-2 sentences. "
                "Never include any of these words: exec, read_file, write_file, edit_file, list_dir, web_search, web_fetch. "
                "No headings, no colons-as-headers."
            ),
        ]

        last_err: Exception | None = None
        for ctx in contexts:
            try:
                result = await self.speak(
                    content=payload,
                    context=ctx,
                    use_llm=True,
                    strict_llm=True,
                )
                if not _bad_progress(result):
                    return result.strip()
            except Exception as e:
                last_err = e

        # Strict: no canned fallbacks. Surface the failure to the caller.
        if last_err:
            raise last_err
        raise RuntimeError("Voice generation produced unusable progress output")
    
    async def speak_completion(
        self,
        label: str,
        result: str,
        completion_reference: str,
    ) -> str:
        """Generate in-character completion message (LLM-backed; no canned fallbacks)."""
        result_preview = result[:800] if result else "done"

        def _has_substance(text: str) -> bool:
            t = (text or "").strip()
            if not t:
                return False
            # Remove the completion ref and check there's still meaningful content.
            body = t.replace(completion_reference, "").strip()
            # If it's just punctuation/whitespace, that's useless.
            cleaned = "".join(ch for ch in body if ch.isalnum() or ch.isspace()).strip()
            # Require at least a few words.
            if len(cleaned.split()) < 4:
                return False
            return True

        contexts = [
            "task completion. Speak naturally in character. 1-3 short sentences. Include what you did or found (based only on the provided result).",
            "task completion. You MUST include a real completion sentence with substance (not just the receipt). Mention the task label and one concrete detail from the result.",
            "task completion. Write 2 short sentences max. Do not output only the receipt. Do not use headings. Be specific but concise.",
        ]

        last_err: Exception | None = None
        for ctx in contexts:
            try:
                notification = await self.speak(
                    content=f"Finished {label}. Result: {result_preview}",
                    context=ctx,
                    must_include=[completion_reference],
                    strict_llm=True,
                )
                if _has_substance(notification):
                    return notification.strip()
            except Exception as e:
                last_err = e

        if last_err:
            raise last_err
        raise RuntimeError("Voice generation produced unusable completion output")
    
    async def speak_failure(
        self,
        label: str,
        error: str,
        completion_reference: str,
    ) -> str:
        """Generate in-character failure message (LLM-backed; no canned fallbacks)."""
        def _has_substance(text: str) -> bool:
            t = (text or "").strip()
            if not t:
                return False
            body = t.replace(completion_reference, "").strip()
            cleaned = "".join(ch for ch in body if ch.isalnum() or ch.isspace()).strip()
            if len(cleaned.split()) < 4:
                return False
            return True

        contexts = [
            "task failure. Speak naturally in character. Be honest, say what went wrong (from the error), and suggest what would help next.",
            "task failure. You MUST include a real failure sentence with substance (not just the receipt). Mention the task label and the error succinctly.",
            "task failure. 1-2 short sentences. No headings. Do not output only the receipt.",
        ]

        last_err: Exception | None = None
        for ctx in contexts:
            try:
                notification = await self.speak(
                    content=f"Failed to complete {label}: {error}",
                    context=ctx,
                    must_include=[completion_reference],
                    strict_llm=True,
                )
                if _has_substance(notification):
                    return notification.strip()
            except Exception as e:
                last_err = e

        if last_err:
            raise last_err
        raise RuntimeError("Voice generation produced unusable failure output")
    
    async def speak_status(self, status_info: str) -> str:
        """Generate in-character status response."""
        result = await self.speak(
            content=status_info,
            context="status request; be accurate, do not invent anything; keep key fields",
            use_llm=True,
            strict_llm=True,
        )
        return result.strip()
    
    async def speak_no_tasks(self) -> str:
        """Generate in-character response when no tasks are running."""
        return await self.speak(
            content="No tasks currently running or recently completed",
            context="responding to status check with nothing to report, be helpful",
        )
