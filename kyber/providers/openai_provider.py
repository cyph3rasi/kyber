"""OpenAI-compatible LLM provider.

Works with any OpenAI-compatible API:
- OpenRouter (default)
- OpenAI direct
- Anthropic (via OpenAI-compatible endpoint)
- Any local/self-hosted model with OpenAI-compatible API

Uses the `openai` Python package directly — no middleman SDKs.
"""

import json
import logging
import os
import re
from typing import Any

from openai import AsyncOpenAI

from kyber.providers.base import LLMProvider, LLMResponse, ToolCallRequest

logger = logging.getLogger(__name__)

# Default models per provider
_DEFAULT_MODELS = {
    "openrouter": "anthropic/claude-sonnet-4",
    "openai": "gpt-4.1",
    "anthropic": "claude-sonnet-4-20250514",
}

# API base URLs per provider
_API_BASES = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


def _normalize_api_key(value: str | None) -> str:
    """Normalize provider API keys for OpenAI-compatible clients.

    Some users paste values like ``Bearer sk-...`` from docs/examples.
    The OpenAI SDK already injects ``Bearer`` in the Authorization header,
    so keep only the raw secret here.
    """
    token = (value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _without_private_flags(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``msg`` with any ``_kyber_*`` internal flags stripped.

    Agent core marks messages with ``_kyber_cache_pin`` etc. to coordinate
    with the provider layer. Those private keys must never hit the wire —
    OpenAI's strict endpoints reject unknown fields.
    """
    return {k: v for k, v in msg.items() if not k.startswith("_kyber_")}


def _strip_leading_think_blocks(text: str | None) -> str | None:
    """Remove leaked leading <think>...</think> blocks from model output.

    Some providers/models emit reasoning text wrapped in <think> tags. We keep
    normal output untouched, but strip leading think blocks when a real answer
    follows. If the whole response is wrapped, we remove just the tags.
    """
    if not isinstance(text, str):
        return text

    pattern = re.compile(
        r"^\s*(?:<think\b[^>]*>[\s\S]*?<\/think>\s*)+",
        flags=re.IGNORECASE,
    )
    match = pattern.match(text)
    if not match:
        return text

    remainder = text[match.end():].lstrip()
    if remainder:
        return remainder

    # If everything is wrapped in think tags, unwrap markers but keep content.
    unwrapped = re.sub(r"</?think\b[^>]*>", "", text, flags=re.IGNORECASE).strip()
    return unwrapped


class OpenAIProvider(LLMProvider):
    """LLM provider using the OpenAI-compatible chat completions API.
    
    Supports OpenRouter, OpenAI, Anthropic, and any compatible endpoint.
    Handles tool calling, retries, and error normalization.
    """
    
    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        provider: str = "openrouter",
        default_model: str | None = None,
        max_retries: int = 3,
        timeout: float = 600.0,
        enable_prompt_cache: bool = True,
    ):
        """Initialize the OpenAI-compatible provider.

        Args:
            api_key: API key. Falls back to env vars (OPENROUTER_API_KEY, OPENAI_API_KEY, etc.)
            api_base: API base URL. Auto-detected from provider if not set.
            provider: Provider name for defaults ("openrouter", "openai", "anthropic").
            default_model: Default model to use. Auto-detected from provider if not set.
            max_retries: Max retries on transient errors.
            timeout: Request timeout in seconds.
            enable_prompt_cache: If True, inject Anthropic-style ``cache_control``
                markers when the provider is anthropic (or an openrouter model
                that routes to anthropic). OpenAI-compat providers auto-cache
                stable prefixes regardless of this flag.
        """
        self.provider = provider.lower()
        self.enable_prompt_cache = enable_prompt_cache
        
        # Resolve API key
        if api_key:
            self.api_key = _normalize_api_key(api_key)
        else:
            self.api_key = _normalize_api_key(self._resolve_api_key())
        
        # Resolve API base
        if api_base:
            self.api_base = api_base
        else:
            self.api_base = _API_BASES.get(self.provider, _API_BASES["openrouter"])
        
        self._default_model = default_model or _DEFAULT_MODELS.get(
            self.provider, _DEFAULT_MODELS["openrouter"]
        )
        
        # Build the async client
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.api_base,
            max_retries=max_retries,
            timeout=timeout,
        )
    
    def _resolve_api_key(self) -> str:
        """Resolve API key from environment variables."""
        env_keys = {
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        env_var = env_keys.get(self.provider, "OPENROUTER_API_KEY")
        key = os.environ.get(env_var, "")
        if not key:
            # Fallback chain for OpenAI-compatible providers.
            # Includes MiniMax because many users follow their docs with MINIMAX_API_KEY.
            key = (
                os.environ.get("OPENROUTER_API_KEY", "")
                or os.environ.get("OPENAI_API_KEY", "")
                or os.environ.get("MINIMAX_API_KEY", "")
            )
        return key
    
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        return self._default_model
    
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        tool_choice: Any | None = None,
        max_tokens: int = 16384,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Send a chat completion request.

        Args:
            messages: Conversation messages in OpenAI format.
            tools: Tool definitions in OpenAI format.
            model: Model to use (defaults to provider default).
            tool_choice: Tool choice mode ("auto", "none", "required", or specific tool).
            max_tokens: Maximum response tokens.
            temperature: Kept for back-compat; **not sent** to any provider.
                Modern Anthropic models reject it, Codex overrides it server
                side, and omitting it from OpenAI-compat providers doesn't
                change output quality but keeps the prompt prefix stable for
                caching.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        del temperature  # intentionally never sent — see docstring
        model = model or self._default_model

        # Optionally mark the static prefix for Anthropic's prompt cache.
        # Messages are mutated by reference to avoid an expensive deep copy —
        # callers treat the list as ephemeral per request.
        prepared_messages = self._prepare_messages_for_caching(messages, model)
        prepared_tools = self._prepare_tools_for_caching(tools, model)

        # Build request kwargs. No temperature, no extraneous fields.
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": prepared_messages,
            "max_tokens": max_tokens,
        }

        # Add tools if provided
        if prepared_tools:
            kwargs["tools"] = prepared_tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice

        try:
            response = await self.client.chat.completions.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            logger.error(f"LLM API error: {e}")
            msg = str(e)
            if "Please carry the API secret key" in msg or "(1004)" in msg:
                raise RuntimeError(
                    "Provider auth failed: ensure the API key is the raw secret "
                    "(no 'Bearer ' prefix) and is configured for the active provider."
                ) from e
            raise

    def _routes_to_anthropic(self, model: str) -> bool:
        """True when the configured endpoint terminates at Anthropic.

        Covers direct Anthropic, OpenRouter's ``anthropic/*`` models, and
        any other route whose model id includes ``claude``.
        """
        if self.provider == "anthropic":
            return True
        m = (model or "").lower()
        return "claude" in m or m.startswith("anthropic/")

    def _prepare_messages_for_caching(
        self,
        messages: list[dict[str, Any]],
        model: str,
    ) -> list[dict[str, Any]]:
        """Mark the last pinned system message with an Anthropic cache hint.

        Only applied when ``enable_prompt_cache`` is on AND the route ends
        at Anthropic. For OpenAI-compatible endpoints pointed elsewhere we
        send messages through unchanged — those providers auto-cache any
        stable prefix ≥1024 tokens without explicit markers.

        Agent core tags the system message it considers "static" with a
        private ``_kyber_cache_pin: True`` flag; we convert that into a
        proper Anthropic ``cache_control`` content block here.
        """
        if not self.enable_prompt_cache or not self._routes_to_anthropic(model):
            # Strip the private flag either way so it never reaches the wire.
            return [_without_private_flags(m) for m in messages]

        out: list[dict[str, Any]] = []
        pinned_indices = [
            i for i, m in enumerate(messages)
            if m.get("_kyber_cache_pin") and m.get("role") == "system"
        ]
        # Only the last pin gets the breakpoint — Anthropic's cache works by
        # walking backwards from the newest cache_control mark.
        last_pin = pinned_indices[-1] if pinned_indices else -1

        for i, m in enumerate(messages):
            clean = _without_private_flags(m)
            if i == last_pin and isinstance(clean.get("content"), str):
                # Convert plain string content into a single content block
                # carrying the Anthropic cache hint. Anthropic's OpenAI-compat
                # endpoint accepts this array form on system messages.
                clean["content"] = [
                    {
                        "type": "text",
                        "text": clean["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            out.append(clean)
        return out

    def _prepare_tools_for_caching(
        self,
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> list[dict[str, Any]] | None:
        """Mark the last tool definition as a cache breakpoint.

        Tool schemas are the second-biggest chunk of a typical request
        prefix after the system prompt. Pinning them with cache_control
        lets Anthropic cache the ``system + tools`` prefix once and reuse
        it across every iteration of the agent loop.
        """
        if not tools:
            return tools
        if not self.enable_prompt_cache or not self._routes_to_anthropic(model):
            return tools

        # Don't mutate the caller's list — it's cached in the registry.
        out = [dict(t) for t in tools]
        out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
        return out
    
    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse an OpenAI chat completion response into LLMResponse."""
        choice = response.choices[0]
        message = choice.message
        
        # Extract content
        content = message.content
        if isinstance(content, str):
            content = _strip_leading_think_blocks(content)
        
        # Extract tool calls
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                    logger.warning(f"Failed to parse tool call arguments for {tc.function.name}")
                
                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))
        
        # Extract usage
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }
        
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
