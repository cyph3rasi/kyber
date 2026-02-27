"""MCP tool integration (stdio servers).

Provides generic MCP tools so the agent can discover and call external MCP
server tools configured in kyber config.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry
from kyber.config.loader import load_config
from kyber.config.schema import MCPServerConfig


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", (value or "").strip().lower()).strip("-")


def get_mcp_servers(include_disabled: bool = False) -> list[MCPServerConfig]:
    """Return configured MCP servers from config."""
    cfg = load_config()
    servers = list(getattr(cfg.tools.mcp, "servers", []) or [])
    if include_disabled:
        return servers
    return [s for s in servers if s.enabled]


def find_mcp_server(server_name: str, include_disabled: bool = False) -> MCPServerConfig | None:
    """Find a configured server by exact/lower/sanitized name."""
    wanted = (server_name or "").strip()
    if not wanted:
        return None
    wanted_norm = _norm_name(wanted)
    for server in get_mcp_servers(include_disabled=include_disabled):
        if server.name == wanted:
            return server
        if server.name.lower() == wanted.lower():
            return server
        if _norm_name(server.name) == wanted_norm:
            return server
    return None


def _build_stdio_params(server: MCPServerConfig) -> StdioServerParameters:
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (server.env or {}).items()})
    cwd = (server.cwd or "").strip()
    return StdioServerParameters(
        command=server.command,
        args=[str(a) for a in (server.args or [])],
        env=env,
        cwd=cwd or None,
    )


def _transport(server: MCPServerConfig) -> str:
    value = (getattr(server, "transport", "") or "stdio").strip().lower()
    if value not in {"stdio", "http"}:
        return "stdio"
    return value


def _build_http_headers(server: MCPServerConfig) -> dict[str, str]:
    headers = {}
    for k, v in (getattr(server, "headers", {}) or {}).items():
        key = str(k or "").strip()
        if not key:
            continue
        headers[key] = str(v or "")
    return headers


def _server_timeout(server: MCPServerConfig) -> int:
    try:
        return max(1, int(server.timeout_seconds))
    except Exception:
        return 30


@asynccontextmanager
async def _open_mcp_streams(server: MCPServerConfig):
    transport = _transport(server)
    timeout = _server_timeout(server)

    if transport == "http":
        url = str(getattr(server, "url", "") or "").strip()
        if not url:
            raise ValueError(f"MCP server '{server.name}' has no URL configured")
        headers = _build_http_headers(server)
        async with streamablehttp_client(
            url=url,
            headers=headers or None,
            timeout=timeout,
            sse_read_timeout=max(timeout, 300),
        ) as (read, write, _):
            yield read, write
        return

    command = str(server.command or "").strip()
    if not command:
        raise ValueError(f"MCP server '{server.name}' has no command configured")

    params = _build_stdio_params(server)
    async with stdio_client(params) as (read, write):
        yield read, write


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    raw = tool.model_dump(mode="json", exclude_none=True) if hasattr(tool, "model_dump") else {}
    return {
        "name": raw.get("name", ""),
        "title": raw.get("title", ""),
        "description": raw.get("description", ""),
        "input_schema": raw.get("inputSchema") or {},
    }


def _content_item_to_dict(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", exclude_none=True)
    if isinstance(item, dict):
        return item
    return {"type": "unknown", "value": str(item)}


def _extract_text(item: Any) -> str:
    text = getattr(item, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(item, dict) and isinstance(item.get("text"), str):
        return str(item["text"])
    return ""


async def list_mcp_tools(server: MCPServerConfig) -> list[dict[str, Any]]:
    """Connect to an MCP server and list available tools."""
    if not server.enabled:
        raise ValueError(f"MCP server '{server.name}' is disabled")
    timeout = _server_timeout(server)
    async with _open_mcp_streams(server) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=timeout)
            result = await asyncio.wait_for(session.list_tools(), timeout=timeout)
    return [_tool_to_dict(t) for t in result.tools]


async def call_mcp_tool(server: MCPServerConfig, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call one tool on an MCP server and return structured output."""
    if not server.enabled:
        raise ValueError(f"MCP server '{server.name}' is disabled")
    if not (tool_name or "").strip():
        raise ValueError("tool_name is required")

    timeout = _server_timeout(server)
    args = arguments or {}

    async with _open_mcp_streams(server) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=timeout)
            result = await asyncio.wait_for(
                session.call_tool(name=tool_name, arguments=args),
                timeout=timeout,
            )

    content = [_content_item_to_dict(item) for item in (result.content or [])]
    text_parts = [_extract_text(item) for item in (result.content or [])]
    text = "\n".join([t for t in text_parts if t]).strip()
    payload: dict[str, Any] = {
        "server": server.name,
        "tool": tool_name,
        "is_error": bool(getattr(result, "isError", False)),
        "content": content,
    }
    if text:
        payload["text"] = text
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        payload["structured_content"] = structured
    return payload


async def test_mcp_server(server_name: str) -> dict[str, Any]:
    """Used by dashboard API to validate connectivity and list tools."""
    server = find_mcp_server(server_name, include_disabled=True)
    if not server:
        raise ValueError(f"MCP server '{server_name}' not found")
    if not server.enabled:
        raise ValueError(f"MCP server '{server_name}' is disabled")
    tools = await list_mcp_tools(server)
    return {"ok": True, "server": server.name, "tools": tools, "count": len(tools)}


class MCPListServersTool(Tool):
    toolset = "mcp"

    @property
    def name(self) -> str:
        return "mcp_list_servers"

    @property
    def description(self) -> str:
        return "List configured MCP servers and whether they are enabled."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "include_disabled": {
                    "type": "boolean",
                    "description": "Include disabled servers in the output (default false).",
                },
            },
            "required": [],
        }

    async def execute(self, include_disabled: bool = False, **kwargs: Any) -> str:
        del kwargs
        servers = get_mcp_servers(include_disabled=include_disabled)
        items = [
            {
                "name": s.name,
                "enabled": bool(s.enabled),
                "transport": _transport(s),
                "command": s.command,
                "args": list(s.args or []),
                "cwd": s.cwd or "",
                "url": str(getattr(s, "url", "") or ""),
                "header_keys": sorted((getattr(s, "headers", {}) or {}).keys()),
                "timeout_seconds": _server_timeout(s),
                "env_keys": sorted((s.env or {}).keys()),
            }
            for s in servers
        ]
        return json.dumps({"servers": items, "count": len(items)}, ensure_ascii=False)


class MCPListToolsTool(Tool):
    toolset = "mcp"

    @property
    def name(self) -> str:
        return "mcp_list_tools"

    @property
    def description(self) -> str:
        return "Connect to a configured MCP server and list available tools."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "server_name": {
                    "type": "string",
                    "description": "Configured MCP server name.",
                },
            },
            "required": ["server_name"],
        }

    async def execute(self, server_name: str, **kwargs: Any) -> str:
        del kwargs
        server = find_mcp_server(server_name, include_disabled=True)
        if not server:
            available = [s.name for s in get_mcp_servers(include_disabled=True)]
            return json.dumps(
                {"error": f"MCP server '{server_name}' not found", "available_servers": available},
                ensure_ascii=False,
            )
        try:
            tools = await list_mcp_tools(server)
            return json.dumps(
                {"server": server.name, "tools": tools, "count": len(tools)},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps(
                {"error": str(e), "server": server.name},
                ensure_ascii=False,
            )


class MCPCallTool(Tool):
    toolset = "mcp"

    @property
    def name(self) -> str:
        return "mcp_call_tool"

    @property
    def description(self) -> str:
        return "Call a specific tool on a configured MCP server."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "server_name": {
                    "type": "string",
                    "description": "Configured MCP server name.",
                },
                "tool_name": {
                    "type": "string",
                    "description": "Tool name exposed by the MCP server.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments object passed to the MCP tool.",
                },
            },
            "required": ["server_name", "tool_name"],
        }

    async def execute(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs
        server = find_mcp_server(server_name, include_disabled=True)
        if not server:
            available = [s.name for s in get_mcp_servers(include_disabled=True)]
            return json.dumps(
                {"error": f"MCP server '{server_name}' not found", "available_servers": available},
                ensure_ascii=False,
            )
        try:
            payload = await call_mcp_tool(server, tool_name=tool_name, arguments=arguments)
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            return json.dumps(
                {
                    "error": str(e),
                    "server": server.name,
                    "tool": tool_name,
                },
                ensure_ascii=False,
            )


registry.register(MCPListServersTool())
registry.register(MCPListToolsTool())
registry.register(MCPCallTool())
