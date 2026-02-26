from __future__ import annotations

from fastapi.testclient import TestClient

from kyber.gateway.api import _normalize_session_id, create_gateway_app


class _DummySessions:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return True


class _DummyAgent:
    def __init__(self) -> None:
        self.sessions = _DummySessions()
        self.calls: list[dict[str, str]] = []

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:default",
        channel: str = "cli",
        chat_id: str = "default",
        tracked_task_id: str | None = None,
    ) -> str:
        del tracked_task_id
        self.calls.append(
            {
                "content": content,
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
            }
        )
        return f"echo:{content}"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_normalize_session_id() -> None:
    assert _normalize_session_id("my session") == "my-session"
    assert _normalize_session_id("a/b?c") == "a-b-c"
    assert _normalize_session_id("") == "default"


def test_chat_turn_returns_response_and_uses_dashboard_context() -> None:
    token = "test-token"
    agent = _DummyAgent()
    app = create_gateway_app(agent, token)
    client = TestClient(app)

    res = client.post(
        "/chat/turn",
        headers=_auth(token),
        json={"message": "hello", "sessionId": "my session"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["session_id"] == "my-session"
    assert body["response"] == "echo:hello"

    assert len(agent.calls) == 1
    call = agent.calls[0]
    assert call["session_key"] == "dashboard:my-session"
    assert call["channel"] == "dashboard"
    assert call["chat_id"] == "my-session"


def test_chat_turn_requires_message() -> None:
    token = "test-token"
    app = create_gateway_app(_DummyAgent(), token)
    client = TestClient(app)

    res = client.post("/chat/turn", headers=_auth(token), json={"sessionId": "abc"})
    assert res.status_code == 400
    assert res.json()["detail"] == "message is required"


def test_chat_reset_deletes_session() -> None:
    token = "test-token"
    agent = _DummyAgent()
    app = create_gateway_app(agent, token)
    client = TestClient(app)

    res = client.post(
        "/chat/reset",
        headers=_auth(token),
        json={"sessionId": "group/1"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["session_id"] == "group-1"
    assert body["deleted"] is True
    assert agent.sessions.deleted == ["dashboard:group-1"]
