"""Strands-backed provider used for chat and task execution."""

from __future__ import annotations

import hashlib
import inspect
import os
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from kyber.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from kyber.agent.tools.shell import ExecTool
from kyber.agent.tools.web import WebFetchTool, WebSearchTool
from kyber.providers.base import LLMProvider, LLMResponse
from kyber.providers.litellm_provider import LiteLLMProvider


class _KyberLiteLLMModel:
    """Factory wrapper for a sanitized Strands LiteLLM model."""

    @staticmethod
    def create(*, client_args: dict[str, Any], model_id: str, params: dict[str, Any]) -> Any:
        from strands.models.litellm import LiteLLMModel

        class SanitizedLiteLLMModel(LiteLLMModel):
            @classmethod
            def format_request_message_content(cls, content: Any, **kwargs: Any) -> dict[str, Any]:
                # Some OpenAI-compatible endpoints reject reasoningContent / thinking
                # blocks in multi-turn chat completions. Convert them to plain text.
                if isinstance(content, dict):
                    if "reasoningContent" in content:
                        return {"type": "text", "text": "(internal reasoning omitted)"}
                    if content.get("type") == "thinking":
                        return {"type": "text", "text": "(internal reasoning omitted)"}
                return super().format_request_message_content(content, **kwargs)

        return SanitizedLiteLLMModel(
            client_args=client_args,
            model_id=model_id,
            params=params,
        )


class StrandsProvider(LLMProvider):
    """LLM provider that executes via Strands Agents."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "openai/gpt-5-mini",
        provider_name: str | None = None,
        is_custom: bool = False,
        workspace: Path | None = None,
        exec_timeout: int = 60,
        brave_api_key: str | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.provider_name = (provider_name or "").strip().lower() or "openai"
        self.is_custom = bool(is_custom)
        self.workspace = Path(workspace or Path.cwd())
        self.exec_timeout = max(10, int(exec_timeout))
        self.brave_api_key = (brave_api_key or "").strip() or None

        self._fallback = LiteLLMProvider(
            api_key=api_key,
            api_base=api_base,
            default_model=default_model,
            provider_name=provider_name,
            is_custom=is_custom,
        )

        self._chat_tools: list[Any] | None = None
        self._task_tools: list[Any] | None = None

    def get_default_model(self) -> str:
        return self.default_model

    def uses_provider_native_orchestration(self) -> bool:
        return True

    def uses_native_session_context(self) -> bool:
        # We rebuild conversation context from orchestrator messages each turn.
        return False

    @staticmethod
    def _extract_content(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts = [StrandsProvider._extract_content(v) for v in value]
            return "\n".join(p for p in parts if p).strip()
        if isinstance(value, dict):
            for key in ("text", "content", "output", "message"):
                out = StrandsProvider._extract_content(value.get(key))
                if out:
                    return out
            return str(value).strip()
        return str(value).strip()

    @staticmethod
    def _normalize_model(model: str | None, provider_name: str) -> str:
        m = (model or "").strip()
        p = (provider_name or "").strip().lower()
        if not m:
            return ""
        provider_prefixes: dict[str, tuple[str, ...]] = {
            "openrouter": ("openrouter/",),
            "openai": ("openai/",),
            "deepseek": ("deepseek/",),
            "groq": ("groq/",),
            "anthropic": ("anthropic/",),
            "google": ("google/", "google_genai/", "gemini/"),
            "gemini": ("google/", "google_genai/", "gemini/"),
            "azure_openai": ("azure_openai/", "openai/"),
            "zai": ("zai/", "z.ai/"),
            "z.ai": ("z.ai/", "zai/"),
        }
        for prefix in provider_prefixes.get(p, (f"{p}/",)):
            if m.lower().startswith(prefix):
                return m[len(prefix) :]
        return m

    @staticmethod
    def _split_provider_model_spec(model: str | None) -> tuple[str | None, str]:
        raw = (model or "").strip()
        if not raw:
            return None, ""
        if ":" not in raw:
            return None, raw
        head, tail = raw.split(":", 1)
        provider_hint = head.strip().lower()
        if not tail.strip():
            return None, raw
        known = {
            "openai",
            "anthropic",
            "google",
            "google_genai",
            "gemini",
            "groq",
            "deepseek",
            "openrouter",
            "azure_openai",
            "zai",
            "z.ai",
        }
        if provider_hint in known:
            return provider_hint, tail.strip()
        return None, raw

    def _resolve_provider_and_model(self, selected_model: str) -> tuple[str, str]:
        aliases = {
            "google": "gemini",
            "google_genai": "gemini",
            "z.ai": "zai",
        }
        hint, raw = self._split_provider_model_spec(selected_model)
        provider = aliases.get((hint or self.provider_name).lower(), (hint or self.provider_name).lower())
        model_id = self._normalize_model(raw or selected_model, provider)
        return provider, model_id

    def _resolve_base_url(self, provider_name: str) -> str | None:
        explicit = (self.api_base or "").strip()
        if explicit:
            return explicit
        defaults = {
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "groq": "https://api.groq.com/openai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "z.ai": "https://api.z.ai/api/coding/paas/v4",
            "zai": "https://api.z.ai/api/coding/paas/v4",
        }
        return defaults.get(provider_name)

    async def _emit_callback(self, callback: Any | None, message: str) -> None:
        if callback is None:
            return
        try:
            maybe = callback(message)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception as e:
            logger.debug(f"Strands progress callback failed: {e}")

    def _resolve_path(self, raw_path: str | None) -> str:
        if not raw_path:
            return str(self.workspace)
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return str(candidate.resolve())

    def _build_tools(self, mode: str) -> list[Any]:
        from strands import tool

        read_tool = ReadFileTool()
        write_tool = WriteFileTool()
        edit_tool = EditFileTool()
        list_tool = ListDirTool()
        exec_tool = ExecTool(
            timeout=self.exec_timeout,
            working_dir=str(self.workspace),
            restrict_to_workspace=False,
        )
        web_fetch_tool = WebFetchTool()
        web_search_tool = WebSearchTool(api_key=self.brave_api_key or "")

        @tool
        async def read_file(path: str) -> str:
            """Read a UTF-8 text file from the workspace."""
            return await read_tool.execute(path=self._resolve_path(path))

        @tool
        async def write_file(path: str, content: str) -> str:
            """Write text content to a file. Creates directories when needed."""
            return await write_tool.execute(path=self._resolve_path(path), content=content)

        @tool
        async def edit_file(path: str, old_text: str, new_text: str) -> str:
            """Replace exact text in a file. old_text must match exactly once."""
            return await edit_tool.execute(
                path=self._resolve_path(path),
                old_text=old_text,
                new_text=new_text,
            )

        @tool
        async def list_dir(path: str | None = None) -> str:
            """List directory contents. Defaults to the workspace root."""
            return await list_tool.execute(path=self._resolve_path(path))

        @tool
        async def exec(command: str, working_dir: str | None = None) -> str:
            """Execute a shell command and return stdout/stderr and exit details."""
            wd = self._resolve_path(working_dir) if working_dir else str(self.workspace)
            return await exec_tool.execute(command=command, working_dir=wd)

        @tool
        async def web_fetch(url: str, extractMode: str = "markdown", maxChars: int | None = None) -> str:
            """Fetch and extract content from a URL."""
            return await web_fetch_tool.execute(url=url, extractMode=extractMode, maxChars=maxChars)

        tools: list[Any] = [read_file, write_file, edit_file, list_dir, exec, web_fetch]

        if self.brave_api_key:

            @tool
            async def web_search(query: str, count: int | None = None) -> str:
                """Search the web and return concise results."""
                return await web_search_tool.execute(query=query, count=count)

            tools.append(web_search)

        return tools

    def _get_tools(self, mode: str) -> list[Any]:
        if mode == "task":
            if self._task_tools is None:
                self._task_tools = self._build_tools(mode)
            return self._task_tools
        if self._chat_tools is None:
            self._chat_tools = self._build_tools(mode)
        return self._chat_tools

    def _to_strands_messages(self, messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []

        for msg in messages:
            role = str(msg.get("role") or "user").lower()
            content = self._extract_content(msg.get("content"))
            if not content:
                continue

            if role == "system":
                system_parts.append(content)
                continue
            if role not in {"user", "assistant"}:
                continue

            out.append({"role": role, "content": [{"text": content}]})

        if not out:
            out = [{"role": "user", "content": [{"text": "Hello."}]}]

        system_prompt = "\n\n".join(system_parts).strip() or None
        return system_prompt, out

    def _resolve_strands_model_id(self, selected_model: str) -> tuple[str, str]:
        provider, model = self._resolve_provider_and_model(selected_model)

        # Custom providers route through OpenAI-compatible mode.
        if self.is_custom:
            if model.lower().startswith("openai/"):
                return provider, model
            return provider, f"openai/{model}"

        prefix_map = {
            "openai": "openai",
            "openrouter": "openrouter",
            "anthropic": "anthropic",
            "deepseek": "deepseek",
            "groq": "groq",
            "gemini": "gemini",
            "azure_openai": "azure_openai",
            "zai": "openai",
        }
        prefix = prefix_map.get(provider, "openai")
        if model.lower().startswith(f"{prefix}/"):
            return provider, model
        return provider, f"{prefix}/{model}"

    def _build_model(self, selected_model: str, *, temperature: float, max_tokens: int) -> Any:
        import litellm

        provider_name, model_id = self._resolve_strands_model_id(selected_model)
        base_url = self._resolve_base_url(provider_name)

        client_args: dict[str, Any] = {
            "timeout": float(self.exec_timeout),
        }
        if self.api_key:
            client_args["api_key"] = self.api_key
        if base_url:
            client_args["api_base"] = base_url
        if provider_name == "openrouter":
            client_args["extra_headers"] = {
                "HTTP-Referer": "https://kyber.chat",
                "X-Title": "Kyber",
            }

        params: dict[str, Any] = {
            "temperature": temperature,
        }
        if max_tokens and max_tokens > 0:
            params["max_tokens"] = max_tokens

        # Keep LiteLLM quiet unless we explicitly log an error.
        litellm.drop_params = True
        litellm.suppress_debug_info = True

        return _KyberLiteLLMModel.create(
            client_args=client_args,
            model_id=model_id,
            params=params,
        )

    @staticmethod
    def _is_max_tokens_error(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        if "maxtokensreachedexception" in name:
            return True
        return "max_tokens" in msg and ("limit" in msg or "unrecoverable" in msg)

    @staticmethod
    def _compact_retry_messages(messages: list[dict[str, Any]], keep_last: int = 8) -> list[dict[str, Any]]:
        if len(messages) <= keep_last:
            return messages
        trimmed = messages[-keep_last:]
        return [
            {"role": m.get("role", "user"), "content": (str(m.get("content", ""))[:4000])}
            for m in trimmed
        ]

    @staticmethod
    def _effective_max_tokens(max_tokens: int | None) -> int | None:
        env_cap = (os.getenv("KYBER_STRANDS_MAX_TOKENS", "") or "").strip()
        if env_cap:
            try:
                v = int(env_cap)
                if v > 0:
                    return v
            except ValueError:
                pass
        if max_tokens is None:
            return None
        if max_tokens <= 0:
            return None
        # Don't force the generic default cap; let the upstream model choose.
        if max_tokens == 4096:
            return None
        return max_tokens

    def _extract_result_text(self, result: Any) -> str:
        text = str(result or "").strip()
        if text:
            return text

        message = getattr(result, "message", None)
        if isinstance(message, dict):
            return self._extract_content(message.get("content"))
        return ""

    async def _run_strands(
        self,
        *,
        selected_model: str,
        mode: str,
        prompt: list[dict[str, Any]] | str,
        system_prompt: str | None,
        temperature: float,
        max_tokens: int | None,
        callback: Any | None = None,
    ) -> str:
        from strands import Agent

        model = self._build_model(
            selected_model,
            temperature=temperature,
            max_tokens=max_tokens or 0,
        )
        agent = Agent(
            model=model,
            tools=self._get_tools(mode),
            system_prompt=system_prompt,
            callback_handler=None,
        )

        if callback is None:
            result = await agent.invoke_async(prompt)
            return self._extract_result_text(result)

        seen_tools: set[str] = set()
        final_result: Any = None
        async for event in agent.stream_async(prompt):
            current_tool = event.get("current_tool_use") if isinstance(event, dict) else None
            if isinstance(current_tool, dict):
                name = str(current_tool.get("name") or "").strip()
                if name and name not in seen_tools:
                    seen_tools.add(name)
                    await self._emit_callback(callback, f"Using tool: `{name}`...")

            if isinstance(event, dict) and "result" in event:
                final_result = event.get("result")

        return self._extract_result_text(final_result)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        tool_choice: Any | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        session_id: str | None = None,
        callback: Any | None = None,
    ) -> LLMResponse:
        # OpenAI-style tool-calling is still needed for orchestrator respond-tool
        # compatibility and voice generation; route those through LiteLLM.
        if tools:
            _ = session_id
            fallback_max_tokens = 4096 if max_tokens is None else max_tokens
            return await self._fallback.chat(
                messages=messages,
                tools=tools,
                model=model,
                tool_choice=tool_choice,
                max_tokens=fallback_max_tokens,
                temperature=temperature,
            )

        selected = (model or self.default_model or "").strip()
        if not selected:
            return LLMResponse(content="Error calling LLM: no model configured", finish_reason="error")

        try:
            effective_max_tokens = self._effective_max_tokens(max_tokens)
            system_prompt, strands_messages = self._to_strands_messages(messages)
            text = await self._run_strands(
                selected_model=selected,
                mode="chat",
                prompt=strands_messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=effective_max_tokens,
                callback=callback,
            )
            return LLMResponse(content=text, finish_reason="stop")
        except Exception as e:
            if self._is_max_tokens_error(e):
                logger.warning(
                    f"Strands hit max_tokens | provider={self.provider_name} | model={selected} | "
                    "retrying with compact context and no cap"
                )
                try:
                    system_prompt, strands_messages = self._to_strands_messages(
                        self._compact_retry_messages(messages)
                    )
                    text = await self._run_strands(
                        selected_model=selected,
                        mode="chat",
                        prompt=strands_messages,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        max_tokens=None,
                        callback=callback,
                    )
                    return LLMResponse(content=text, finish_reason="stop")
                except Exception as retry_error:
                    logger.error(
                        f"Strands retry after max_tokens failed | provider={self.provider_name} | "
                        f"model={selected} | error={retry_error}"
                    )
            logger.error(f"Strands chat failed | provider={self.provider_name} | model={selected} | error={e}")
            return LLMResponse(content=f"Error calling LLM: {str(e)}", finish_reason="error")

    async def execute_task(
        self,
        *,
        task_description: str,
        persona_prompt: str,
        timezone: str | None = None,
        workspace: Path | None = None,
    ) -> str:
        from kyber.utils.helpers import current_datetime_str

        ws = Path(workspace or self.workspace).resolve()
        selected = (self.default_model or "").strip()
        if not selected:
            raise RuntimeError("No model configured for task execution.")

        task_fingerprint = hashlib.sha1(
            f"{task_description}\n{persona_prompt}\n{ws}".encode("utf-8")
        ).hexdigest()[:12]
        _ = f"task:{task_fingerprint}:{uuid.uuid4().hex[:8]}"

        os.environ.setdefault("STRANDS_NON_INTERACTIVE", "true")
        os.environ.setdefault("BYPASS_TOOL_CONSENT", "true")
        system = (
            f"{persona_prompt}\n\n"
            "You are executing a user-requested task directly.\n"
            f"Current time: {current_datetime_str(timezone)}\n"
            f"Workspace: {ws}\n"
            "Do the work using tools, then return a concise natural-language summary.\n"
            "Do not ask for permission.\n"
            "Use relative paths inside the workspace when possible."
        )
        return await self._run_strands(
            selected_model=selected,
            mode="task",
            prompt=task_description,
            system_prompt=system,
            temperature=0.2,
            max_tokens=None,
            callback=None,
        )
