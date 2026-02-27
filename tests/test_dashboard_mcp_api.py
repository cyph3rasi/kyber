from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kyber.config.schema import Config
from kyber.dashboard import server as dashboard_server
from kyber.agent.tools import mcp as mcp_tool


def _make_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    cfg.dashboard.auth_token = "test-token"
    return cfg


def test_dashboard_mcp_test_endpoint(monkeypatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)

    def fake_load_config() -> Config:
        return cfg

    async def fake_test_mcp_server(server_name: str) -> dict:
        assert server_name == "filesystem"
        return {
            "ok": True,
            "server": server_name,
            "tools": [{"name": "read_file"}],
            "count": 1,
        }

    monkeypatch.setattr(dashboard_server, "load_config", fake_load_config)
    monkeypatch.setattr(mcp_tool, "test_mcp_server", fake_test_mcp_server)

    app = dashboard_server.create_dashboard_app(cfg)
    client = TestClient(app, base_url="http://localhost")
    response = client.post(
        "/api/mcp/servers/test",
        headers={"Authorization": "Bearer test-token"},
        json={"name": "filesystem"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["server"] == "filesystem"
    assert body["count"] == 1
