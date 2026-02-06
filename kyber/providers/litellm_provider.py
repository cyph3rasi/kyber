"""LiteLLM provider implementation for multi-provider support."""

import os
from typing import Any

import litellm
from litellm import acompletion
from loguru import logger

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
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.provider_name = provider_name.strip().lower() if provider_name else None
        
        if self.provider_name:
            self.is_openrouter = self.provider_name == "openrouter"
            self.is_vllm = self.provider_name == "vllm"
        else:
            # Detect OpenRouter by api_key prefix or explicit api_base
            self.is_openrouter = (
                (api_key and api_key.startswith("sk-or-")) or
                (api_base and "openrouter" in api_base)
            )
            # Track if using custom endpoint (vLLM, etc.)
            self.is_vllm = bool(api_base) and not self.is_openrouter
        
        # Configure LiteLLM based on provider
        if api_key:
            if self.is_openrouter:
                # OpenRouter mode - set key
                os.environ["OPENROUTER_API_KEY"] = api_key
            elif self.is_vllm:
                # vLLM/custom endpoint - uses OpenAI-compatible API
                os.environ.setdefault("OPENAI_API_KEY", api_key)
            elif self.provider_name == "deepseek" or "deepseek" in default_model:
                os.environ.setdefault("DEEPSEEK_API_KEY", api_key)
            elif self.provider_name == "anthropic" or "anthropic" in default_model:
                os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
            elif self.provider_name == "openai" or "openai" in default_model or "gpt" in default_model:
                os.environ.setdefault("OPENAI_API_KEY", api_key)
            elif self.provider_name == "gemini" or "gemini" in default_model.lower():
                os.environ.setdefault("GEMINI_API_KEY", api_key)
            elif self.provider_name == "zhipu" or "glm" in default_model or "zhipu" in default_model or "zai" in default_model:
                os.environ.setdefault("ZHIPUAI_API_KEY", api_key)
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
        
        # For OpenRouter, prefix model name if not already prefixed
        if self.is_openrouter and not model.startswith("openrouter/"):
            model = f"openrouter/{model}"
        
        # For Zhipu/Z.ai, ensure prefix is present
        # Handle cases like "glm-4.7-flash" -> "zai/glm-4.7-flash"
        if ("glm" in model.lower() or "zhipu" in model.lower()) and not (
            model.startswith("zhipu/") or 
            model.startswith("zai/") or 
            model.startswith("openrouter/")
        ):
            model = f"zai/{model}"

        # For vLLM, use hosted_vllm/ prefix per LiteLLM docs
        if self.is_vllm:
            model = f"hosted_vllm/{model}"
        
        # For Gemini, ensure gemini/ prefix if not already present.
        # Skip if routing via OpenRouter (openrouter/...) since that provider handles it.
        if (
            "gemini" in model.lower()
            and not model.startswith("gemini/")
            and not model.startswith("openrouter/")
        ):
            model = f"gemini/{model}"
        
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        # Pass api_base directly for custom endpoints (vLLM, etc.)
        if self.api_base:
            kwargs["api_base"] = self.api_base
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        
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
                # Non-transient or final attempt — return error as content
                return LLMResponse(
                    content=f"Error calling LLM: {error_str}",
                    finish_reason="error",
                )
        # Should not reach here, but just in case
        return LLMResponse(
            content=f"Error calling LLM: {str(last_error)}",
            finish_reason="error",
        )
    
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
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
    
    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
