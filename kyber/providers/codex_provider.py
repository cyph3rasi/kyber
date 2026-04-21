"""OpenAI Codex provider using the Responses API + ChatGPT OAuth.

This provider lets Kyber drive Codex models (e.g. ``gpt-5.2-codex``,
``gpt-5.3-codex``) using the user's ChatGPT Plus/Pro/Business subscription
rather than a metered OpenAI API key.

Auth is shared with the official ``codex`` CLI via ``~/.codex/auth.json``.
See :mod:`kyber.providers.codex_auth` for details.

The wire format is the OpenAI Responses API
(``POST https://api.openai.com/v1/responses``), which differs from chat
completions. This module adapts chat-completions-style messages and tool
definitions into Responses items, and adapts the response back into the
generic ``LLMResponse`` / ``ToolCallRequest`` shapes Kyber already uses.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from kyber.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from kyber.providers.codex_auth import (
    CodexAuthError,
    CodexTokens,
    ensure_fresh_tokens,
    load_tokens,
    refresh_tokens,
)

logger = logging.getLogger(__name__)

# ChatGPT OAuth tokens are only authorized to hit the `chatgpt.com`
# backend-api host — not api.openai.com, which needs platform-API scopes.
# This is the same base URL the official `codex` CLI uses.
CODEX_API_BASE = "https://chatgpt.com/backend-api/codex"
# Same host exposes the model catalog for the authenticated user.
CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
CODEX_MODELS_CLIENT_VERSION = "1.0.0"

# Cloudflare sits in front of chatgpt.com/backend-api/codex and whitelists
# a small set of first-party originators. Requests without these headers
# get a 403 challenge page. User-Agent intentionally mimics the codex CLI.
CODEX_CLOUDFLARE_HEADERS = {
    "User-Agent": "codex_cli_rs/0.0.0 (Kyber)",
    "originator": "codex_cli_rs",
}
DEFAULT_MODEL = "gpt-5.3-codex"
# Conservative fallback when the backend-api is unreachable (e.g. during
# installer runs on a flaky network). Ordered newest-first.
FALLBACK_MODELS = ("gpt-5.3-codex", "gpt-5.2-codex", "codex-mini-latest")
DEFAULT_TIMEOUT_SECONDS = 600.0


async def fetch_available_models(timeout_seconds: float = 15.0) -> list[str]:
    """Return the Codex model catalog for the current ChatGPT login.

    Reads tokens from ~/.codex/auth.json, refreshes if needed, and hits the
    ChatGPT backend-api. Falls back to a small hardcoded list on any error
    so the installer can still make progress offline.
    """
    try:
        tokens = await ensure_fresh_tokens()
    except CodexAuthError as e:
        logger.warning("Cannot fetch Codex models — no valid login: %s", e)
        return list(FALLBACK_MODELS)

    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "Accept": "application/json",
        **CODEX_CLOUDFLARE_HEADERS,
    }
    if tokens.account_id:
        headers["ChatGPT-Account-ID"] = tokens.account_id

    timeout = httpx.Timeout(max(5.0, float(timeout_seconds)))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                CODEX_MODELS_URL,
                params={"client_version": CODEX_MODELS_CLIENT_VERSION},
                headers=headers,
            )
    except httpx.HTTPError as e:
        logger.warning("Codex models fetch failed: %s", e)
        return list(FALLBACK_MODELS)

    if resp.status_code != 200:
        logger.warning(
            "Codex models fetch returned HTTP %s: %s",
            resp.status_code,
            resp.text[:200],
        )
        return list(FALLBACK_MODELS)

    try:
        payload = resp.json()
    except ValueError:
        return list(FALLBACK_MODELS)

    models = _extract_model_ids(payload)
    return models or list(FALLBACK_MODELS)


def _extract_model_ids(payload: Any) -> list[str]:
    """Pull model IDs out of the backend-api response, regardless of envelope."""
    candidates: list[Any] = []
    if isinstance(payload, dict):
        for key in ("models", "data", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                candidates = val
                break
    elif isinstance(payload, list):
        candidates = payload

    out: list[str] = []
    seen: set[str] = set()
    for entry in candidates:
        if isinstance(entry, str):
            mid = entry
        elif isinstance(entry, dict):
            mid = entry.get("id") or entry.get("slug") or entry.get("name") or ""
        else:
            mid = ""
        if isinstance(mid, str) and mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def _chat_messages_to_responses_input(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert chat-completions-style messages to Responses API input.

    Returns ``(instructions, input_items)``. System messages are merged into
    a single ``instructions`` string (the Responses API's dedicated slot for
    them). Everything else becomes an input item.
    """
    instructions_parts: list[str] = []
    items: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str) and content.strip():
                instructions_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str) and text.strip():
                            instructions_parts.append(text)
            continue

        if role == "user":
            items.append({"role": "user", "content": _normalize_user_content(content)})
            continue

        if role == "assistant":
            text = _coerce_text(content)
            if text:
                items.append({"role": "assistant", "content": text})
            for tc in msg.get("tool_calls") or []:
                try:
                    fn = tc.get("function") or {}
                    call_id = tc.get("id") or ""
                    name = fn.get("name") or ""
                    arguments = fn.get("arguments")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments or {})
                    if not call_id or not name:
                        continue
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": arguments,
                        }
                    )
                except Exception:
                    logger.warning("Skipping malformed tool_call in assistant message")
            continue

        if role == "tool":
            call_id = msg.get("tool_call_id") or ""
            output = _coerce_text(content) or ""
            if not call_id:
                continue
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                }
            )
            continue

        # Unknown role: best-effort pass-through as user text.
        text = _coerce_text(content)
        if text:
            items.append({"role": "user", "content": text})

    instructions = "\n\n".join(p for p in instructions_parts if p.strip()) or None
    return instructions, items


def _normalize_user_content(content: Any) -> Any:
    """Pass strings through; pass lists through as-is; coerce other types to str."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return content
    if content is None:
        return ""
    return str(content)


def _coerce_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _tools_to_responses_format(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten OpenAI chat-completions tool defs to Responses-API tool defs.

    chat: ``{"type": "function", "function": {"name", "description", "parameters"}}``
    resp: ``{"type": "function", "name", "description", "parameters"}``
    """
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            out.append(
                {
                    "type": "function",
                    "name": fn.get("name") or "",
                    "description": fn.get("description") or "",
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        else:
            # Already in Responses shape, or something custom — pass through.
            out.append(t)
    return out


def _parse_responses_output(payload: dict[str, Any]) -> LLMResponse:
    """Map a Responses API payload back into our generic LLMResponse."""
    text_parts: list[str] = []
    tool_calls: list[ToolCallRequest] = []

    output_items = payload.get("output") or []
    if not isinstance(output_items, list):
        output_items = []

    for item in output_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")

        if item_type == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                # Responses API emits "output_text" for assistant text.
                if part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
            continue

        if item_type in ("function_call", "custom_tool_call"):
            call_id = item.get("call_id") or item.get("id") or ""
            name = item.get("name") or ""
            arguments_raw = item.get("arguments")
            if isinstance(arguments_raw, str):
                try:
                    arguments = json.loads(arguments_raw) if arguments_raw.strip() else {}
                except json.JSONDecodeError:
                    logger.warning("Codex tool call %s returned non-JSON arguments", name)
                    arguments = {}
            elif isinstance(arguments_raw, dict):
                arguments = arguments_raw
            else:
                arguments = {}

            if not call_id or not name:
                continue
            tool_calls.append(ToolCallRequest(id=call_id, name=name, arguments=arguments))
            continue

        # "reasoning" items are ignored for now. They carry encrypted CoT
        # content used for multi-turn continuity; Kyber doesn't feed them
        # back in yet.

    # Usage
    usage = {}
    usage_raw = payload.get("usage") or {}
    if isinstance(usage_raw, dict):
        usage = {
            "prompt_tokens": usage_raw.get("input_tokens", 0) or 0,
            "completion_tokens": usage_raw.get("output_tokens", 0) or 0,
            "total_tokens": usage_raw.get("total_tokens", 0) or 0,
        }

    # Finish reason heuristic: if any tool calls, treat as tool_calls; else stop.
    finish_reason = "tool_calls" if tool_calls else "stop"

    return LLMResponse(
        content="".join(text_parts) if text_parts else None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
    )


async def _consume_responses_stream(resp: httpx.Response) -> dict[str, Any]:
    """Consume an OpenAI Responses SSE stream and return a completed payload.

    The Codex backend's ``response.completed`` event ships with an empty
    ``output`` array. Real content arrives as per-item events:

      * ``response.output_item.done`` — carries the fully-assembled item
        (message with text parts, or function_call with resolved arguments).
      * ``response.completed`` — carries usage/status metadata.

    We stitch the two together: assembled items from ``output_item.done``
    replace the (empty) ``output`` field of the completed response.
    """
    final_response: dict[str, Any] | None = None
    last_error_text: str | None = None
    items_by_index: dict[int, dict[str, Any]] = {}
    data_buffer: list[str] = []

    async for line in resp.aiter_lines():
        if not line:
            if not data_buffer:
                continue
            data_str = "\n".join(data_buffer)
            data_buffer.clear()
            if data_str == "[DONE]":
                continue
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                logger.debug("Skipping malformed SSE data chunk: %r", data_str[:120])
                continue

            event_type = event.get("type")

            if event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    idx = event.get("output_index")
                    if not isinstance(idx, int):
                        idx = len(items_by_index)
                    items_by_index[idx] = item
            elif event_type == "response.completed":
                r = event.get("response")
                if isinstance(r, dict):
                    final_response = r
            elif event_type in ("response.failed", "error"):
                err = event.get("error") or event.get("response", {}).get("error") or {}
                last_error_text = (
                    err.get("message") if isinstance(err, dict) else str(err)
                ) or json.dumps(event)[:300]
            continue

        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_buffer.append(line[5:].lstrip())

    if final_response is None:
        if last_error_text:
            raise RuntimeError(f"Codex stream ended with error: {last_error_text}")
        raise RuntimeError("Codex stream ended without a response.completed event")

    if items_by_index:
        final_response["output"] = [items_by_index[i] for i in sorted(items_by_index)]
    return final_response


class CodexProvider(LLMProvider):
    """LLM provider talking to OpenAI's Responses API using ChatGPT OAuth.

    Credentials live in ``~/.codex/auth.json`` and are managed by the
    ``codex`` CLI. This provider refreshes tokens transparently and retries
    once on 401 in case the cached access_token was already stale.
    """

    def __init__(
        self,
        default_model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        api_base: str | None = None,
    ):
        super().__init__(api_key=None, api_base=api_base or CODEX_API_BASE)
        self._default_model = default_model or DEFAULT_MODEL
        self._timeout = httpx.Timeout(max(10.0, float(timeout)))
        self._tokens: CodexTokens | None = None

    def get_default_model(self) -> str:
        return self._default_model

    async def _get_tokens(self) -> CodexTokens:
        if self._tokens is None:
            self._tokens = load_tokens()
        self._tokens = await ensure_fresh_tokens(self._tokens)
        return self._tokens

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        tool_choice: Any | None = None,
        max_tokens: int = 16384,
        temperature: float = 0.7,
    ) -> LLMResponse:
        model_name = model or self._default_model

        instructions, input_items = _chat_messages_to_responses_input(messages)

        body: dict[str, Any] = {
            "model": model_name,
            "input": input_items,
            # The Codex backend refuses requests with server-side storage
            # enabled and requires SSE streaming. max_output_tokens and
            # temperature are rejected outright by the ChatGPT backend —
            # the server controls both.
            "store": False,
            "stream": True,
        }
        if instructions:
            body["instructions"] = instructions
        # max_tokens/temperature are intentionally dropped. Kept as parameters
        # for API compatibility with the LLMProvider ABC.
        _ = (max_tokens, temperature)

        if tools:
            body["tools"] = _tools_to_responses_format(tools)
            if tool_choice is not None:
                body["tool_choice"] = tool_choice

        payload = await self._post_with_retry(body)
        return _parse_responses_output(payload)

    async def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST to /responses and consume the SSE stream.

        The Codex backend only supports streaming responses. We read events
        until we see ``response.completed`` (which carries the full final
        response object) and return it in non-streaming shape. Refreshes
        tokens once on 401.
        """
        tokens = await self._get_tokens()
        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {tokens.access_token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                **CODEX_CLOUDFLARE_HEADERS,
            }
            if tokens.account_id:
                headers["ChatGPT-Account-ID"] = tokens.account_id

            url = f"{self.api_base.rstrip('/')}/responses"
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", url, headers=headers, content=json.dumps(body)
                ) as resp:
                    if resp.status_code == 401 and attempt == 1:
                        await resp.aclose()
                        logger.info("Codex returned 401; refreshing token and retrying")
                        tokens = await refresh_tokens(tokens)
                        self._tokens = tokens
                        break_for_retry = True
                    else:
                        break_for_retry = False

                    if break_for_retry:
                        continue

                    if resp.status_code != 200:
                        err_text = ""
                        try:
                            err_text = (await resp.aread()).decode("utf-8", errors="replace")
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"Codex API error (HTTP {resp.status_code}): {err_text[:500]}"
                        )

                    return await _consume_responses_stream(resp)

        raise RuntimeError("Codex API call failed after retry")
