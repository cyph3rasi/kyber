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
        timeout: float = 300.0,
    ):
        """Initialize the OpenAI-compatible provider.
        
        Args:
            api_key: API key. Falls back to env vars (OPENROUTER_API_KEY, OPENAI_API_KEY, etc.)
            api_base: API base URL. Auto-detected from provider if not set.
            provider: Provider name for defaults ("openrouter", "openai", "anthropic").
            default_model: Default model to use. Auto-detected from provider if not set.
            max_retries: Max retries on transient errors.
            timeout: Request timeout in seconds.
        """
        self.provider = provider.lower()
        
        # Resolve API key
        if api_key:
            self.api_key = api_key
        else:
            self.api_key = self._resolve_api_key()
        
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
            # Fallback: try OPENROUTER first, then OPENAI
            key = os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
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
            temperature: Sampling temperature.
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        model = model or self._default_model
        
        # Build request kwargs
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        # Add tools if provided
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        
        try:
            response = await self.client.chat.completions.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            logger.error(f"LLM API error: {e}")
            raise
    
    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse an OpenAI chat completion response into LLMResponse."""
        choice = response.choices[0]
        message = choice.message
        
        # Extract content
        content = message.content
        
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
