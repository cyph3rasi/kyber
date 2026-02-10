"""LiteLLM provider implementation for multi-provider support."""

import os
from typing import Any

import litellm
from litellm import acompletion
from loguru import logger

litellm.drop_params = True

from kyber.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.
    
    Supports OpenRouter, Anthropic, OpenAI, Gemini, and many other providers through
    a unified interface.
    """
    
    def __init__(
        self, 
        api_key: str | None = None, 
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        provider_name: str | None = None,
        is_custom: bool = False,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.provider_name = provider_name.strip().lower() if provider_name else None
        self.is_custom = is_custom
        
        if self.is_custom:
            # Custom OpenAI-compatible provider — route through openai/ prefix
            self.is_openrouter = False
            if api_key:
                os.environ.setdefault("OPENAI_API_KEY", api_key)
        elif self.provider_name:
            self.is_openrouter = self.provider_name == "openrouter"
        else:
            self.is_openrouter = (
                (api_key and api_key.startswith("sk-or-")) or
                (api_base and "openrouter" in api_base)
            )
        
        # Configure LiteLLM based on provider
        if api_key and not self.is_custom:
            if self.is_openrouter:
                os.environ["OPENROUTER_API_KEY"] = api_key
            elif self.provider_name == "deepseek" or "deepseek" in default_model:
                os.environ.setdefault("DEEPSEEK_API_KEY", api_key)
            elif self.provider_name == "anthropic" or "anthropic" in default_model:
                os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
            elif self.provider_name == "openai" or "openai" in default_model or "gpt" in default_model:
                os.environ.setdefault("OPENAI_API_KEY", api_key)
            elif self.provider_name == "gemini" or "gemini" in default_model.lower():
                os.environ.setdefault("GEMINI_API_KEY", api_key)
            elif self.provider_name == "groq" or "groq" in default_model:
                os.environ.setdefault("GROQ_API_KEY", api_key)
        
        if api_base:
            litellm.api_base = api_base
        
        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
    
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        tool_choice: Any | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        model = model or self.default_model
        
        # Custom OpenAI-compatible providers — use openai/ prefix
        if self.is_custom:
            if not model.startswith("openai/"):
                model = f"openai/{model}"
        else:
            # For OpenRouter, prefix model name if not already prefixed
            if self.is_openrouter and not model.startswith("openrouter/"):
                model = f"openrouter/{model}"
            
            # For DeepSeek, ensure deepseek/ prefix.
            if (
                self.provider_name == "deepseek"
                and not model.startswith("deepseek/")
                and not model.startswith("openrouter/")
            ):
                model = f"deepseek/{model}"

            # For Groq, ensure groq/ prefix.
            if (
                self.provider_name == "groq"
                and not model.startswith("groq/")
                and not model.startswith("openrouter/")
            ):
                model = f"groq/{model}"

            # For OpenAI, ensure openai/ prefix.
            if (
                self.provider_name == "openai"
                and not model.startswith("openai/")
                and not model.startswith("openrouter/")
            ):
                model = f"openai/{model}"

            # For Anthropic, ensure anthropic/ prefix.
            if (
                self.provider_name == "anthropic"
                and not model.startswith("anthropic/")
                and not model.startswith("openrouter/")
            ):
                model = f"anthropic/{model}"

            # For Gemini, ensure gemini/ prefix if not already present.
            # Skip if routing via OpenRouter (openrouter/...) since that provider handles it.
            if (
                (self.provider_name == "gemini" or "gemini" in model.lower())
                and not model.startswith("gemini/")
                and not model.startswith("openrouter/")
            ):
                model = f"gemini/{model}"
        
        # Sanitize messages for provider compatibility
        sanitized_messages = self._sanitize_messages(messages)
        
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": sanitized_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timeout": 120,  # seconds — generous enough for reasoning models; callers add their own tighter timeouts
        }
        
        # Pass api_base directly for custom endpoints
        if self.api_base:
            kwargs["api_base"] = self.api_base
        
        # Brand OpenRouter requests so they show as Kyber on the leaderboard
        if self.is_openrouter:
            kwargs["extra_headers"] = {
                "HTTP-Referer": "https://kyber.chat",
                "X-Title": "Kyber",
            }
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await acompletion(**kwargs)
                return self._parse_response(response)
            except Exception as e:
                last_error = e
                error_str = str(e)
                # Retry on transient / parse errors from the provider
                is_transient = any(tok in error_str.lower() for tok in [
                    "unable to get json response",
                    "expecting value",
                    "jsondecodeerror",
                    "timeout",
                    "rate limit",
                    "429",
                    "500",
                    "502",
                    "503",
                    "504",
                    "overloaded",
                    "connection",
                ])
                if is_transient and attempt < 2:
                    wait = 2 ** attempt  # 1s, 2s
                    logger.warning(
                        f"LLM call failed (attempt {attempt + 1}/3), "
                        f"retrying in {wait}s: {error_str}"
                    )
                    import asyncio
                    await asyncio.sleep(wait)
                    continue
                # Non-transient or final attempt — log debug info and return error
                sys_msg = next((m for m in sanitized_messages if m.get("role") == "system"), None)
                sys_len = len(sys_msg["content"]) if sys_msg and sys_msg.get("content") else 0
                roles = [m.get("role", "?") for m in sanitized_messages]
                logger.error(
                    f"LLM request failed (non-transient) | model={model} | "
                    f"messages={len(sanitized_messages)} | roles={roles} | "
                    f"system_prompt_chars={sys_len} | "
                    f"tools={len(tools) if tools else 0} | "
                    f"error={error_str}"
                )
                return LLMResponse(
                    content=f"Error calling LLM: {error_str}",
                    finish_reason="error",
                )
        # Should not reach here, but just in case
        return LLMResponse(
            content=f"Error calling LLM: {str(last_error)}",
            finish_reason="error",
        )

    def _normalize_content(self, content: Any) -> str | None:
        """
        Normalize provider message.content into a string.

        Some providers/models return content as:
        - None (tool-calls-only turns)
        - [] (empty content blocks)
        - list of blocks (e.g., Anthropic-style [{"type":"text","text":"..."}])
        - dict blocks
        """
        if content is None:
            return None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if item is None:
                    continue
                if isinstance(item, str):
                    if item.strip():
                        parts.append(item)
                    continue
                if isinstance(item, dict):
                    # Common block shapes: {"type":"text","text":"..."} or {"text":"..."}
                    txt = item.get("text") or item.get("content")
                    if isinstance(txt, str) and txt.strip():
                        parts.append(txt)
                        continue
                    # If there are nested structures, fall back to a compact str().
                    s = str(item).strip()
                    if s:
                        parts.append(s)
                    continue
                s = str(item).strip()
                if s:
                    parts.append(s)
            out = "\n".join(parts).strip()
            return out or None
        if isinstance(content, dict):
            txt = content.get("text") or content.get("content")
            if isinstance(txt, str):
                return txt
            s = str(content).strip()
            return s or None
        s = str(content).strip()
        return s or None
    
    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        try:
            choice = response.choices[0]
        except (IndexError, AttributeError):
            logger.warning("LLM response has no choices")
            return LLMResponse(content=None, finish_reason="error")

        message = choice.message
        
        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                try:
                    # Parse arguments from JSON string if needed
                    args = tc.function.arguments
                    if isinstance(args, str):
                        import json
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"raw": args}
                    
                    tool_calls.append(ToolCallRequest(
                        id=tc.id or f"call_{id(tc)}",
                        name=tc.function.name,
                        arguments=args,
                    ))
                except (AttributeError, TypeError) as e:
                    logger.warning(f"Skipping malformed tool call: {e}")
                    continue
        
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        
        content = self._normalize_content(getattr(message, "content", None))
        # Some wrappers expose "text" instead of "content".
        if not content:
            content = self._normalize_content(getattr(message, "text", None))

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
    
    def _sanitize_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sanitize messages for provider compatibility.
        
        Some providers (e.g., DeepSeek, certain OpenAI-compatible endpoints) 
        reject messages with certain fields or structures. This method ensures
        all messages conform to a compatible format.
        """
        sanitized = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")
            
            # Skip messages without a role
            if not role:
                logger.debug("Skipping message without role")
                continue
            
            # Skip empty messages (no content and no tool_calls)
            if not content and not tool_calls and role != "tool":
                logger.debug(f"Skipping empty {role} message")
                continue
            
            clean = {"role": role}
            
            if role == "tool":
                # Tool result messages - must have tool_call_id
                if not msg.get("tool_call_id"):
                    logger.debug("Skipping tool message without tool_call_id")
                    continue
                clean["tool_call_id"] = msg["tool_call_id"]
                clean["content"] = content or ""
                # Note: Some providers don't support 'name' field on tool messages
                # We include it if present since most do support it
                if msg.get("name"):
                    clean["name"] = msg["name"]
            elif role == "assistant" and tool_calls:
                # Assistant message with tool calls
                clean["content"] = content or ""
                clean["tool_calls"] = tool_calls
            else:
                # Regular message (system, user, or assistant without tool_calls)
                clean["content"] = content or ""
            
            sanitized.append(clean)
        
        return sanitized
    
    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
