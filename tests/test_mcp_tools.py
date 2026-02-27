from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kyber.agent.tools import mcp as mcp_tool
from kyber.config.schema import Config, MCPServerConfig


def _config_with_mcp_server(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    cfg.tools.mcp.servers = [
        MCPServerConfig(
            name="filesystem",
            enabled=True,
            command="uvx",
            args=["mcp-server-filesystem", str(tmp_path)],
            env={"A": "B"},
            cwd=str(tmp_path),
            timeout_seconds=15,
        )
    ]
    return cfg


def test_mcp_list_servers_tool_reads_config(monkeypatch, tmp_path: Path) -> None:
    cfg = _config_with_mcp_server(tmp_path)
    monkeypatch.setattr(mcp_tool, "load_config", lambda: cfg)

    tool = mcp_tool.MCPListServersTool()
    out = asyncio.run(tool.execute())
    data = json.loads(out)

    assert data["count"] == 1
    assert data["servers"][0]["name"] == "filesystem"
    assert data["servers"][0]["command"] == "uvx"
    assert data["servers"][0]["timeout_seconds"] == 15
    assert data["servers"][0]["env_keys"] == ["A"]


def test_mcp_list_tools_tool_uses_list_mcp_tools(monkeypatch, tmp_path: Path) -> None:
    cfg = _config_with_mcp_server(tmp_path)
    monkeypatch.setattr(mcp_tool, "load_config", lambda: cfg)

    async def _fake_list(server: MCPServerConfig):
        assert server.name == "filesystem"
        return [{"name": "read_file", "description": "Read file", "input_schema": {"type": "object"}}]

    monkeypatch.setattr(mcp_tool, "list_mcp_tools", _fake_list)

    tool = mcp_tool.MCPListToolsTool()
    out = asyncio.run(tool.execute(server_name="filesystem"))
    data = json.loads(out)

    assert data["server"] == "filesystem"
    assert data["count"] == 1
    assert data["tools"][0]["name"] == "read_file"


def test_mcp_call_tool_passes_arguments(monkeypatch, tmp_path: Path) -> None:
    cfg = _config_with_mcp_server(tmp_path)
    monkeypatch.setattr(mcp_tool, "load_config", lambda: cfg)

    async def _fake_call(server: MCPServerConfig, tool_name: str, arguments: dict | None):
        assert server.name == "filesystem"
        assert tool_name == "read_file"
        assert arguments == {"path": "/tmp/a.txt"}
        return {"server": server.name, "tool": tool_name, "is_error": False, "content": [], "text": "ok"}

    monkeypatch.setattr(mcp_tool, "call_mcp_tool", _fake_call)

    tool = mcp_tool.MCPCallTool()
    out = asyncio.run(
        tool.execute(
            server_name="filesystem",
            tool_name="read_file",
            arguments={"path": "/tmp/a.txt"},
        )
    )
    data = json.loads(out)

    assert data["server"] == "filesystem"
    assert data["tool"] == "read_file"
    assert data["text"] == "ok"


def test_mcp_list_servers_http_transport_fields(monkeypatch, tmp_path: Path) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    cfg.tools.mcp.servers = [
        MCPServerConfig(
            name="stripe",
            enabled=True,
            transport="http",
            url="https://mcp.stripe.com",
            headers={"Authorization": "Bearer sk_test_x"},
            timeout_seconds=20,
        )
    ]
    monkeypatch.setattr(mcp_tool, "load_config", lambda: cfg)

    tool = mcp_tool.MCPListServersTool()
    out = asyncio.run(tool.execute())
    data = json.loads(out)

    assert data["count"] == 1
    assert data["servers"][0]["transport"] == "http"
    assert data["servers"][0]["url"] == "https://mcp.stripe.com"
    assert data["servers"][0]["header_keys"] == ["Authorization"]


def test_list_mcp_tools_http_requires_url() -> None:
    server = MCPServerConfig(
        name="stripe",
        enabled=True,
        transport="http",
        url="",
    )
    with pytest.raises(ValueError, match="has no URL configured"):
        asyncio.run(mcp_tool.list_mcp_tools(server))
