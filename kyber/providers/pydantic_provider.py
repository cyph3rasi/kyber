"""PydanticAI-backed provider used for chat and task execution."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import uuid
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from kyber.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from kyber.agent.tools.shell import ExecTool
from kyber.agent.tools.web import WebFetchTool, WebSearchTool
from kyber.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _ToolEnvelope(BaseModel):
    """Structured tool-call envelope produced by the model."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class PydanticAIProvider(LLMProvider):
    """LLM provider that executes through PydanticAI agents."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "openai:gpt-5-mini",
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

    def get_default_model(self) -> str:
        return self.default_model

    def uses_provider_native_orchestration(self) -> bool:
        # Hard-locked: orchestration stays in Kyber's orchestrator layer.
        return False

    def uses_native_session_context(self) -> bool:
        # Conversation context is rebuilt by orchestrator message history.
        return False

    @staticmethod
    def _extract_content(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts = [PydanticAIProvider._extract_content(v) for v in value]
            return "\n".join(p for p in parts if p).strip()
        if isinstance(value, dict):
            for key in ("text", "content", "output", "message"):
                out = PydanticAIProvider._extract_content(value.get(key))
                if out:
                    return out
            return str(value).strip()
        return str(value).strip()

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
            "google-gla",
            "google-vertex",
            "gemini",
            "groq",
            "deepseek",
            "openrouter",
        }
        if provider_hint in known:
            return provider_hint, tail.strip()
        return None, raw

    @staticmethod
    def _canonical_provider_name(provider: str) -> str:
        p = (provider or "").strip().lower()
        aliases = {
            "google": "gemini",
            "google-gla": "gemini",
            "google-vertex": "gemini",
        }
        return aliases.get(p, p)

    @staticmethod
    def _normalize_model_for_provider(model: str, provider_name: str) -> str:
        m = (model or "").strip()
        p = (provider_name or "").strip().lower()
        if not m:
            return ""

        if p == "openrouter" and m.lower().startswith("openrouter/"):
            return m[len("openrouter/") :]
        if p == "openai" and m.lower().startswith("openai/"):
            return m[len("openai/") :]
        if p == "anthropic" and m.lower().startswith("anthropic/"):
            return m[len("anthropic/") :]
        if p == "deepseek" and m.lower().startswith("deepseek/"):
            return m[len("deepseek/") :]
        if p == "groq" and m.lower().startswith("groq/"):
            return m[len("groq/") :]
        if p == "gemini":
            for pref in ("gemini/", "google/", "google-gla/", "google-vertex/"):
                if m.lower().startswith(pref):
                    return m[len(pref) :]
        return m

    def _resolve_provider_and_model(self, selected_model: str) -> tuple[str, str]:
        hint, raw = self._split_provider_model_spec(selected_model)
        provider = self._canonical_provider_name(hint or self.provider_name)
        model_id = self._normalize_model_for_provider(raw or selected_model, provider)
        return provider, model_id

    def _set_provider_api_key_env(self, provider_name: str) -> None:
        if not self.api_key:
            return
        p = self._canonical_provider_name(provider_name)
        if p == "openrouter":
            os.environ.setdefault("OPENROUTER_API_KEY", self.api_key)
        elif p == "openai":
            os.environ.setdefault("OPENAI_API_KEY", self.api_key)
        elif p == "anthropic":
            os.environ.setdefault("ANTHROPIC_API_KEY", self.api_key)
        elif p == "deepseek":
            os.environ.setdefault("DEEPSEEK_API_KEY", self.api_key)
        elif p == "groq":
            os.environ.setdefault("GROQ_API_KEY", self.api_key)
        elif p == "gemini":
            os.environ.setdefault("GOOGLE_API_KEY", self.api_key)
            os.environ.setdefault("GEMINI_API_KEY", self.api_key)

    def _build_model(self, selected_model: str) -> Any:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        provider_name, model_id = self._resolve_provider_and_model(selected_model)
        self._set_provider_api_key_env(provider_name)

        if self.is_custom:
            if not self.api_base:
                raise RuntimeError("Custom provider requires api_base.")
            if model_id.lower().startswith("openai/"):
                model_id = model_id[len("openai/") :]
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(base_url=self.api_base, api_key=self.api_key),
            )

        # For OpenAI-compatible endpoints with explicit base URL, use OpenAI provider.
        if self.api_base and provider_name in {"openai", "deepseek", "groq", "openrouter", "gemini"}:
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(base_url=self.api_base, api_key=self.api_key),
            )

        provider_prefix = {
            "openrouter": "openrouter",
            "openai": "openai",
            "anthropic": "anthropic",
            "deepseek": "deepseek",
            "groq": "groq",
            "gemini": "google-gla",
        }.get(provider_name, "openai")
        return f"{provider_prefix}:{model_id}"

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(
            token in msg
            for token in [
                "timeout",
                "timed out",
                "rate limit",
                "429",
                "500",
                "502",
                "503",
                "504",
                "overloaded",
                "connection",
                "temporarily unavailable",
            ]
        )

    async def _run_with_retries(self, call, *, retries: int = 3) -> Any:
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                return await call()
            except Exception as e:
                last_error = e
                if attempt < retries - 1 and self._is_transient_error(e):
                    wait = 2**attempt
                    logger.warning(
                        f"PydanticAI call failed (attempt {attempt + 1}/{retries}), "
                        f"retrying in {wait}s: {e}"
                    )
                    await asyncio.sleep(wait)
                    continue
                raise
        raise RuntimeError(f"PydanticAI call failed: {last_error}")

    @staticmethod
    def _to_prompt(messages: list[dict[str, Any]]) -> tuple[str | None, str]:
        system_parts: list[str] = []
        convo_parts: list[str] = []

        for msg in messages:
            role = str(msg.get("role") or "user").lower()
            content = PydanticAIProvider._extract_content(msg.get("content"))
            if not content:
                continue
            if role == "system":
                system_parts.append(content)
                continue
            if role not in {"user", "assistant"}:
                continue
            convo_parts.append(f"{role.upper()}:\n{content}")

        prompt = "\n\n".join(convo_parts).strip() or "USER:\nHello."
        system_prompt = "\n\n".join(system_parts).strip() or None
        return system_prompt, prompt

    @staticmethod
    def _model_settings(*, max_tokens: int | None, temperature: float) -> dict[str, Any]:
        settings: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None and max_tokens > 0:
            settings["max_tokens"] = max_tokens
        return settings

    async def _emit_callback(self, callback: Any | None, message: str) -> None:
        if callback is None:
            return
        try:
            maybe = callback(message)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception as e:
            logger.debug(f"PydanticAI progress callback failed: {e}")

    async def _run_text(
        self,
        *,
        selected_model: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float,
    ) -> str:
        from pydantic_ai import Agent

        model = self._build_model(selected_model)
        system_prompt, prompt = self._to_prompt(messages)
        agent = Agent(model, instructions=system_prompt or "")
        result = await self._run_with_retries(
            lambda: agent.run(
                prompt,
                model_settings=self._model_settings(max_tokens=max_tokens, temperature=temperature),
            )
        )
        return self._extract_content(result.output)

    async def _run_tool_selection(
        self,
        *,
        selected_model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float,
    ) -> _ToolEnvelope:
        from pydantic_ai import Agent

        model = self._build_model(selected_model)
        system_prompt, prompt = self._to_prompt(messages)

        tool_lines: list[str] = []
        names: list[str] = []
        for t in tools:
            fn = t.get("function", {})
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            names.append(name)
            desc = str(fn.get("description") or "").strip()
            params = fn.get("parameters")
            tool_lines.append(f"- {name}: {desc}\n  parameters schema: {params}")

        if not names:
            raise RuntimeError("No tool definitions provided.")

        instructions = (
            (system_prompt + "\n\n" if system_prompt else "")
            + "You must select exactly one tool and provide valid arguments.\n"
            + "Return structured output with:\n"
            + "- name: selected tool name\n"
            + "- arguments: JSON object of arguments for that tool\n"
            + f"Allowed tools:\n{chr(10).join(tool_lines)}"
        )

        agent = Agent(model, instructions=instructions, output_type=_ToolEnvelope)
        result = await self._run_with_retries(
            lambda: agent.run(
                prompt,
                model_settings=self._model_settings(max_tokens=max_tokens, temperature=temperature),
            )
        )
        out = result.output
        if out.name not in names:
            raise RuntimeError(f"Model selected unknown tool '{out.name}', allowed={names}")
        if not isinstance(out.arguments, dict):
            raise RuntimeError("Model returned non-object tool arguments.")
        return out

    @staticmethod
    def _tool_parameters_schema(
        tools: list[dict[str, Any]],
        tool_name: str,
    ) -> dict[str, Any] | None:
        for t in tools:
            fn = t.get("function", {})
            name = str(fn.get("name") or "").strip()
            if name == tool_name:
                params = fn.get("parameters")
                if isinstance(params, dict):
                    return params
        return None

    @classmethod
    def _validate_tool_arguments(
        cls,
        value: Any,
        schema: dict[str, Any] | None,
        *,
        path: str = "arguments",
    ) -> list[str]:
        if not isinstance(schema, dict):
            return []

        errors: list[str] = []
        schema_type = schema.get("type")
        if schema_type == "object":
            if not isinstance(value, dict):
                return [f"{path} must be an object"]
            required = schema.get("required") or []
            properties = schema.get("properties") or {}

            for key in required:
                if key not in value:
                    errors.append(f"{path}.{key} is required")
                    continue
                v = value.get(key)
                if isinstance(v, str) and not v.strip():
                    errors.append(f"{path}.{key} must be non-empty")

            if isinstance(properties, dict):
                for key, prop_schema in properties.items():
                    if key not in value:
                        continue
                    errors.extend(
                        cls._validate_tool_arguments(
                            value[key],
                            prop_schema if isinstance(prop_schema, dict) else None,
                            path=f"{path}.{key}",
                        )
                    )
            return errors

        if schema_type == "array":
            if not isinstance(value, list):
                return [f"{path} must be an array"]
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for i, item in enumerate(value):
                    errors.extend(
                        cls._validate_tool_arguments(
                            item,
                            item_schema,
                            path=f"{path}[{i}]",
                        )
                    )
            return errors

        if schema_type == "string":
            if not isinstance(value, str):
                return [f"{path} must be a string"]
            if not value.strip():
                return [f"{path} must be non-empty"]
            enum = schema.get("enum")
            if isinstance(enum, list) and value not in enum:
                return [f"{path} must be one of {enum}"]
            return []

        if schema_type == "integer":
            if not isinstance(value, int):
                return [f"{path} must be an integer"]
            return []

        if schema_type == "number":
            if not isinstance(value, (int, float)):
                return [f"{path} must be a number"]
            return []

        if schema_type == "boolean":
            if not isinstance(value, bool):
                return [f"{path} must be a boolean"]
            return []

        return []

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
        _ = (tool_choice, session_id, callback)
        selected = (model or self.default_model or "").strip()
        if not selected:
            return LLMResponse(content="Error calling LLM: no model configured", finish_reason="error")

        try:
            if tools:
                env = await self._run_tool_selection(
                    selected_model=selected,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )

                schema = self._tool_parameters_schema(tools, env.name)
                validation_errors = self._validate_tool_arguments(env.arguments, schema)
                if validation_errors:
                    logger.warning(
                        "PydanticAI produced invalid tool args for "
                        f"'{env.name}': {validation_errors}"
                    )
                    # For orchestrator's core respond-tool, fall back to a plain
                    # text generation and wrap it as a no-op respond intent.
                    if env.name == "respond":
                        fallback_text = (
                            await self._run_text(
                                selected_model=selected,
                                messages=messages,
                                max_tokens=max_tokens,
                                temperature=temperature,
                            )
                        ).strip()
                        if not fallback_text:
                            fallback_text = (
                                "Sorry, I had trouble generating a response just now. "
                                "Please try again."
                            )
                        env = _ToolEnvelope(
                            name="respond",
                            arguments={
                                "message": fallback_text,
                                "intent": {"action": "none"},
                            },
                        )
                    else:
                        raise RuntimeError(
                            f"Model returned invalid arguments for tool '{env.name}': "
                            f"{'; '.join(validation_errors)}"
                        )

                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(
                            id=f"call_{uuid.uuid4().hex[:10]}",
                            name=env.name,
                            arguments=env.arguments,
                        )
                    ],
                    finish_reason="tool_calls",
                )

            text = await self._run_text(
                selected_model=selected,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return LLMResponse(content=text, finish_reason="stop")
        except Exception as e:
            logger.error(
                f"PydanticAI chat failed | provider={self.provider_name} | model={selected} | error={e}"
            )
            return LLMResponse(content=f"Error calling LLM: {str(e)}", finish_reason="error")

    def _resolve_path(self, raw_path: str | None, workspace: Path) -> str:
        if not raw_path:
            return str(workspace)
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        return str(candidate.resolve())

    def _build_task_tools(self, workspace: Path, callback: Any | None) -> list[Any]:
        read_tool = ReadFileTool()
        write_tool = WriteFileTool()
        edit_tool = EditFileTool()
        list_tool = ListDirTool()
        exec_tool = ExecTool(
            timeout=self.exec_timeout,
            working_dir=str(workspace),
            restrict_to_workspace=False,
        )
        web_fetch_tool = WebFetchTool()
        web_search_tool = WebSearchTool(api_key=self.brave_api_key or "")

        async def read_file(path: str) -> str:
            """Read a UTF-8 text file from the workspace."""
            await self._emit_callback(callback, "Using tool: `read_file`...")
            return await read_tool.execute(path=self._resolve_path(path, workspace))

        async def write_file(path: str, content: str) -> str:
            """Write text content to a file. Creates directories when needed."""
            await self._emit_callback(callback, "Using tool: `write_file`...")
            return await write_tool.execute(path=self._resolve_path(path, workspace), content=content)

        async def edit_file(path: str, old_text: str, new_text: str) -> str:
            """Replace exact text in a file. old_text must match exactly once."""
            await self._emit_callback(callback, "Using tool: `edit_file`...")
            return await edit_tool.execute(
                path=self._resolve_path(path, workspace),
                old_text=old_text,
                new_text=new_text,
            )

        async def list_dir(path: str | None = None) -> str:
            """List directory contents. Defaults to the workspace root."""
            await self._emit_callback(callback, "Using tool: `list_dir`...")
            return await list_tool.execute(path=self._resolve_path(path, workspace))

        async def exec(command: str, working_dir: str | None = None) -> str:
            """Execute a shell command and return stdout/stderr and exit details."""
            await self._emit_callback(callback, "Using tool: `exec`...")
            wd = self._resolve_path(working_dir, workspace) if working_dir else str(workspace)
            return await exec_tool.execute(command=command, working_dir=wd)

        async def web_fetch(url: str, extractMode: str = "markdown", maxChars: int | None = None) -> str:
            """Fetch and extract content from a URL."""
            await self._emit_callback(callback, "Using tool: `web_fetch`...")
            return await web_fetch_tool.execute(url=url, extractMode=extractMode, maxChars=maxChars)

        tools: list[Any] = [read_file, write_file, edit_file, list_dir, exec, web_fetch]

        if self.brave_api_key:

            async def web_search(query: str, count: int | None = None) -> str:
                """Search the web and return concise results."""
                await self._emit_callback(callback, "Using tool: `web_search`...")
                return await web_search_tool.execute(query=query, count=count)

            tools.append(web_search)

        return tools

    async def _run_task_agent(
        self,
        *,
        selected_model: str,
        task_description: str,
        system_prompt: str,
        workspace: Path,
        callback: Any | None = None,
    ) -> str:
        from pydantic_ai import Agent

        model = self._build_model(selected_model)
        agent = Agent(
            model,
            instructions=system_prompt,
            tools=self._build_task_tools(workspace, callback),
        )
        result = await self._run_with_retries(
            lambda: agent.run(
                task_description,
                model_settings=self._model_settings(max_tokens=None, temperature=0.2),
            )
        )
        text = self._extract_content(result.output).strip()
        if not text:
            raise RuntimeError("PydanticAI task run returned empty output")
        return text

    async def execute_task(
        self,
        *,
        task_description: str,
        persona_prompt: str,
        timezone: str | None = None,
        workspace: Path | None = None,
        callback: Any | None = None,
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

        system = (
            f"{persona_prompt}\n\n"
            "You are executing a user-requested task directly.\n"
            f"Current time: {current_datetime_str(timezone)}\n"
            f"Workspace: {ws}\n"
            "Do the work using tools, then return a concise natural-language summary.\n"
            "Do not ask for permission.\n"
            "Use relative paths inside the workspace when possible."
        )
        return await self._run_task_agent(
            selected_model=selected,
            task_description=task_description,
            system_prompt=system,
            workspace=ws,
            callback=callback,
        )
