"""OpenHands SDK-backed provider used for chat + intent extraction."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from loguru import logger
from openhands.sdk import LLM
from openhands.sdk.llm.message import Message, TextContent

from kyber.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from kyber.utils.openhands_runtime import ensure_openhands_runtime_dirs


class OpenHandsProvider(LLMProvider):
    """LLM provider built directly on OpenHands SDK's LLM client."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "openrouter/google/gemini-3-flash-preview",
        provider_name: str | None = None,
        is_custom: bool = False,
        subscription_mode: bool = False,
        workspace: Any | None = None,
        exec_timeout: int = 60,
        brave_api_key: str | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.provider_name = (provider_name or "").strip().lower() or "openai"
        self.is_custom = bool(is_custom)
        self.subscription_mode = bool(subscription_mode)
        # Kept for constructor compatibility with existing callers.
        self.workspace = workspace
        self.exec_timeout = max(10, int(exec_timeout))
        self.brave_api_key = (brave_api_key or "").strip() or None

    def get_default_model(self) -> str:
        return self.default_model

    def uses_provider_native_orchestration(self) -> bool:
        return False

    def uses_native_session_context(self) -> bool:
        return False

    @staticmethod
    def _is_openrouter_route(provider_name: str | None, api_base: str | None) -> bool:
        prov = (provider_name or "").strip().lower()
        base = (api_base or "").strip().lower()
        return prov == "openrouter" or "openrouter" in prov or "openrouter.ai" in base

    @staticmethod
    def _looks_like_retryable_openrouter_error(message: str) -> bool:
        msg = (message or "").strip().lower()
        if not msg:
            return False
        # OpenRouter + LiteLLM can surface transient failures as
        # `OpenrouterException` with various 5xx payloads.
        if "openrouterexception" in msg and (
            "internal server error" in msg or "service unavailable" in msg
        ):
            return True
        if "status code:" in msg:
            m = re.search(r"status code:\s*(\d{3})", msg)
            if m and m.group(1).startswith("5"):
                return True
        if any(code in msg for code in [" 500 ", " 501 ", " 502 ", " 503 ", " 504 "]):
            return True
        return False

    @staticmethod
    def _resolve_model_string(
        model: str,
        provider_name: str | None,
        is_custom: bool,
        api_base: str | None = None,
    ) -> str:
        m = (model or "").strip()
        if not m:
            raise ValueError("No model specified")
        prov = (provider_name or "").strip().lower()
        base = (api_base or "").strip().lower()
        is_openrouter_route = OpenHandsProvider._is_openrouter_route(prov, base)
        if "/" in m:
            # OpenRouter requires provider-prefixed routing, e.g.
            # openrouter/google/gemini-3-flash-preview.
            if is_openrouter_route and not m.lower().startswith("openrouter/"):
                return f"openrouter/{m}"
            # LiteLLM expects gemini/*, not google/*, for direct Gemini calls.
            if m.lower().startswith("google/"):
                return f"gemini/{m.split('/', 1)[1]}"
            return m
        if is_custom:
            return f"openai/{m}"
        if is_openrouter_route:
            return f"openrouter/{m}"
        prefix_map = {
            "openai": "openai",
            "anthropic": "anthropic",
            "google": "gemini",
            "gemini": "gemini",
            "xai": "xai",
            "deepseek": "deepseek",
            "groq": "groq",
            "openrouter": "openrouter",
        }
        prefix = prefix_map.get(prov, "openai")
        return f"{prefix}/{m}"

    @staticmethod
    def _normalize_openai_model_name(model: str) -> str:
        raw = (model or "").strip()
        lower = raw.lower()
        if lower.startswith("openai/"):
            return raw.split("/", 1)[1].strip()
        if lower.startswith("openai:"):
            return raw.split(":", 1)[1].strip()
        return raw

    @classmethod
    def _is_openai_subscription_model(cls, model: str) -> bool:
        m = cls._normalize_openai_model_name(model).strip().lower()
        return m in {
            "gpt-5.2",
            "gpt-5.2-codex",
            "gpt-5.1-codex-max",
            "gpt-5.1-codex-mini",
            "gpt-5.3-codex",
        }

    def _build_llm(self, selected_model: str) -> LLM:
        llm_model = self._resolve_model_string(
            selected_model,
            self.provider_name,
            self.is_custom,
            self.api_base,
        )

        if self.subscription_mode:
            sub_model = self._normalize_openai_model_name(llm_model)
            if not self._is_openai_subscription_model(sub_model):
                raise RuntimeError(
                    f"Unsupported ChatGPT subscription model '{sub_model}'."
                )
            # Do not open browser from regular chat path. Dashboard login endpoint
            # handles interactive OAuth explicitly.
            return LLM.subscription_login(
                vendor="openai",
                model=sub_model,
                open_browser=False,
                skip_consent=True,
            )

        kwargs: dict[str, Any] = {
            "model": llm_model,
            "api_key": self.api_key or "",
            "temperature": 0.2,
        }
        if self.api_base:
            kwargs["base_url"] = self.api_base
        return LLM(**kwargs)

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(p for p in parts if p).strip()
        text = getattr(content, "text", None)
        if isinstance(text, str):
            return text
        return str(content or "").strip()

    @classmethod
    def _to_message(cls, msg: dict[str, Any]) -> Message:
        role = str(msg.get("role") or "user").lower()
        if role not in {"user", "assistant", "system", "tool"}:
            role = "user"
        text = cls._content_to_text(msg.get("content"))
        return Message(role=role, content=[TextContent(text=text)])

    @staticmethod
    def _json_mode_hint() -> str:
        return (
            "Respond with ONLY a valid JSON object, no markdown. "
            "Schema: "
            '{"message":"string","intent":{"action":"none|spawn_task|check_status|cancel_task",'
            '"task_description":"string|null","task_label":"string|null","task_ref":"string|null",'
            '"complexity":"simple|moderate|complex|null"}}'
        )

    @staticmethod
    def _extract_finish_reason(raw_response: Any) -> str:
        reason = getattr(raw_response, "finish_reason", None)
        if isinstance(reason, str) and reason:
            return reason
        choices = getattr(raw_response, "choices", None) or []
        if choices:
            ch0 = choices[0]
            finish = getattr(ch0, "finish_reason", None)
            if isinstance(finish, str) and finish:
                return finish
        return "stop"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        tool_choice: Any | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        session_id: str | None = None,
        callback: Any | None = None,
    ) -> LLMResponse:
        _ = (tool_choice, session_id, callback, max_tokens, temperature)
        selected = (model or self.default_model or "").strip()
        if not selected:
            return LLMResponse(content="Error calling LLM: no model configured", finish_reason="error")

        try:
            ensure_openhands_runtime_dirs()
            msg_objs = [self._to_message(m) for m in messages]
            if tools:
                # Enforce JSON envelope in legacy "tool" mode.
                msg_objs = [Message(role="system", content=[TextContent(text=self._json_mode_hint())])] + msg_objs

            def _discard_token(_: Any) -> None:
                # OpenHands SDK requires on_token when model/transport forces
                # streaming. We don't stream partial tokens to the chat layer.
                return

            max_attempts = 2 if self._is_openrouter_route(self.provider_name, self.api_base) else 1
            last_error: Exception | None = None
            used_model: str = selected

            for attempt in range(1, max_attempts + 1):
                try:
                    # OpenHands subscription_login can call asyncio.run internally;
                    # build the client off-loop in subscription mode.
                    if self.subscription_mode:
                        llm = await asyncio.to_thread(self._build_llm, selected)
                    else:
                        llm = self._build_llm(selected)

                    # OpenAI subscription models are routed through the Responses API
                    # in OpenHands SDK; calling completion() can hit a LiteLLM bridge
                    # bug (metadata=None / "Instructions are required"). Use
                    # responses() directly in subscription mode.
                    llm_call = llm.responses if self.subscription_mode else llm.completion

                    # OpenHands SDK call is sync; run it off the event loop so
                    # channel heartbeats and other async work stay responsive.
                    response = await asyncio.to_thread(
                        llm_call,
                        messages=msg_objs,
                        on_token=_discard_token,
                    )
                    out_msg = response.message
                    content = self._content_to_text(out_msg.content)
                    finish_reason = self._extract_finish_reason(response.raw_response)

                    tool_calls: list[ToolCallRequest] = []
                    if out_msg.tool_calls:
                        for tc in out_msg.tool_calls:
                            args_raw = tc.arguments
                            args: dict[str, Any] = {}
                            if isinstance(args_raw, str):
                                try:
                                    parsed = json.loads(args_raw)
                                    if isinstance(parsed, dict):
                                        args = parsed
                                except Exception:
                                    args = {}
                            elif isinstance(args_raw, dict):
                                args = args_raw
                            tool_calls.append(
                                ToolCallRequest(
                                    id=str(tc.id),
                                    name=str(tc.name),
                                    arguments=args,
                                )
                            )

                    return LLMResponse(
                        content=content,
                        tool_calls=tool_calls,
                        finish_reason=finish_reason,
                    )
                except Exception as e:
                    last_error = e
                    should_retry_openrouter = (
                        self._is_openrouter_route(self.provider_name, self.api_base)
                        and self._looks_like_retryable_openrouter_error(str(e))
                    )
                    if should_retry_openrouter and attempt < max_attempts:
                        logger.warning(
                            f"OpenHandsProvider chat failed on OpenRouter model={used_model} "
                            f"(attempt {attempt}/2). Retrying same model once: error={e}"
                        )
                        continue
                    break

            logger.error(
                f"OpenHandsProvider chat failed | provider={self.provider_name} | "
                f"model={used_model} | error={last_error}"
            )
            return LLMResponse(content=f"Error calling LLM: {str(last_error)}", finish_reason="error")
        except Exception as e:
            logger.error(
                f"OpenHandsProvider chat failed | provider={self.provider_name} | model={selected} | error={e}"
            )
            return LLMResponse(content=f"Error calling LLM: {str(e)}", finish_reason="error")
