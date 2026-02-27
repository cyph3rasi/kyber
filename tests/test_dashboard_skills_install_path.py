from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kyber.config.schema import Config
from kyber.dashboard import server as dashboard_server


def _make_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    cfg.dashboard.auth_token = "test-token"
    return cfg


def test_dashboard_install_uses_workspace_skills_dir(monkeypatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    captured: dict[str, Path] = {}

    def fake_load_config() -> Config:
        return cfg

    def fake_install_from_source(
        source: str,
        skill: str | None = None,
        replace: bool = False,
        skills_dir: Path | None = None,
    ) -> dict[str, object]:
        del source, skill, replace
        assert skills_dir is not None
        captured["skills_dir"] = Path(skills_dir)
        return {"ok": True, "installed": ["dummy-skill"], "revision": "abc123"}

    monkeypatch.setattr(dashboard_server, "load_config", fake_load_config)
    monkeypatch.setattr(dashboard_server, "install_from_source", fake_install_from_source)

    app = dashboard_server.create_dashboard_app(cfg)
    client = TestClient(app, base_url="http://localhost")
    response = client.post(
        "/api/skills/install",
        headers={"Authorization": "Bearer test-token"},
        json={"source": "owner/repo", "skill": "dummy-skill", "replace": False},
    )

    assert response.status_code == 200
    assert captured["skills_dir"] == cfg.workspace_path / "skills"
    assert response.json()["install_dir"] == str(cfg.workspace_path / "skills")


def test_dashboard_skills_endpoint_reports_workspace_install_dir(monkeypatch, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)

    class _FakeLoader:
        def __init__(self, workspace: Path) -> None:
            self.workspace = workspace

        def list_skills(self, filter_unavailable: bool = False) -> list[dict[str, str]]:
            del filter_unavailable
            return []

    def fake_load_config() -> Config:
        return cfg

    def fake_list_managed_installs(skills_dir: Path | None = None) -> dict[str, object]:
        assert skills_dir == cfg.workspace_path / "skills"
        return {"installed": {}}

    monkeypatch.setattr(dashboard_server, "load_config", fake_load_config)
    monkeypatch.setattr(dashboard_server, "SkillsLoader", _FakeLoader)
    monkeypatch.setattr(dashboard_server, "list_managed_installs", fake_list_managed_installs)

    app = dashboard_server.create_dashboard_app(cfg)
    client = TestClient(app, base_url="http://localhost")
    response = client.get(
        "/api/skills",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["install_dir"] == str(cfg.workspace_path / "skills")
