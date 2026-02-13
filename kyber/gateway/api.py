"""Gateway HTTP API (local control plane).

Exposes running/completed tasks for the dashboard:
- list tasks
- cancel running tasks

This runs in the gateway process so it can access the in-memory registry.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_404_NOT_FOUND

from kyber.agent.orchestrator import Orchestrator
from kyber.agent.task_registry import Task, TaskStatus
from kyber.logging.error_store import clear_errors, get_errors
from kyber.bus.events import OutboundMessage


def _require_token(token: str):
    def _dep(request: Request) -> None:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        provided = auth_header[len("Bearer ") :].strip()
        if not provided or provided != token:
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    return _dep


def _redact_secrets(s: str) -> str:
    """Redact strings that look like API keys, tokens, or passwords."""
    import re
    s = re.sub(
        r'(?i)(api[_-]?key|token|secret|password|bearer)\s*[=:]\s*\S+',
        r'\1=***',
        s,
    )
    s = re.sub(r'\b(sk|key|xai|gsk|pk|rk)-[A-Za-z0-9_-]{20,}\b', '***', s)
    return s


def _task_to_dict(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "reference": t.reference,
        "completion_reference": t.completion_reference,
        "label": t.label,
        "description": t.description,
        "status": t.status.value,
        "origin_channel": t.origin_channel,
        "origin_chat_id": t.origin_chat_id,
        "created_at": t.created_at.isoformat(),
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "iteration": t.iteration,
        "max_iterations": t.max_iterations,
        "current_action": _redact_secrets(t.current_action),
        "actions_completed": [_redact_secrets(a) for a in t.actions_completed[-10:]],
        "result": _redact_secrets(t.result) if t.result else t.result,
        "error": t.error,
    }


def _is_dashboard_visible_task(t: Task) -> bool:
    """Filter internal/system maintenance tasks from dashboard task views."""
    ch = (t.origin_channel or "").strip().lower()
    chat = (t.origin_chat_id or "").strip().lower()
    label = (t.label or "").strip().lower()
    desc = (t.description or "").strip().lower()

    if ch in {"internal", "system"}:
        return False
    # Backward-compatible filtering for older heartbeat entries.
    if "heartbeat" in label:
        return False
    if "heartbeat.md" in desc and "heartbeat_ok" in desc:
        return False
    if chat == "heartbeat" and ch in {"cli", "internal", "system"}:
        return False
    return True


def create_gateway_app(agent: Orchestrator, token: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    require = _require_token(token)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/tasks", dependencies=[Depends(require)])
    async def list_tasks() -> JSONResponse:
        active = [t for t in agent.registry.get_active_tasks() if _is_dashboard_visible_task(t)]
        history = [t for t in agent.registry.get_history(limit=100) if _is_dashboard_visible_task(t)]
        # Only include completed-ish statuses in history response.
        hist_filtered = [
            t for t in history if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        ]
        return JSONResponse(
            {
                "active": [_task_to_dict(t) for t in active],
                "history": [_task_to_dict(t) for t in hist_filtered[::-1]],
                "background_progress_updates": bool(agent.background_progress_updates),
            }
        )

    @app.get("/errors", dependencies=[Depends(require)])
    async def list_errors(limit: int = 200) -> JSONResponse:
        return JSONResponse({"errors": get_errors(limit=limit)})

    @app.post("/errors/clear", dependencies=[Depends(require)])
    async def clear_error_log() -> JSONResponse:
        clear_errors()
        return JSONResponse({"ok": True})

    @app.post("/tasks/{ref}/cancel", dependencies=[Depends(require)])
    async def cancel_task(ref: str) -> JSONResponse:
        task = agent.registry.get_by_ref(ref)
        if not task:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="Task not found")
        if task.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            return JSONResponse({
                "ok": True,
                "status": task.status.value,
                "message": f"Task already {task.status.value}.",
            })
        ok = agent._cancel_task(task.id)
        if ok:
            return JSONResponse({
                "ok": True,
                "status": "cancelling",
                "message": "Cancel requested.",
            })

        refreshed = agent.registry.get(task.id)
        if refreshed and refreshed.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            # If a runner handle is missing but state is still active, force-cancel
            # to avoid leaving the dashboard in a broken "can't cancel" state.
            agent.registry.mark_cancelled(task.id, "Cancelled by user")
            return JSONResponse({
                "ok": True,
                "status": TaskStatus.CANCELLED.value,
                "message": "Task marked cancelled.",
            })

        final_status = refreshed.status.value if refreshed else task.status.value
        return JSONResponse({
            "ok": True,
            "status": final_status,
            "message": f"Task already {final_status}.",
        })

    @app.post("/tasks/{ref}/redeliver", dependencies=[Depends(require)])
    async def redeliver_task(ref: str) -> JSONResponse:
        """
        Re-send the final task output to the original chat.

        Useful if the channel had a transient failure at completion time.
        """
        task = agent.registry.get_by_ref(ref)
        if not task:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="Task not found")

        payload = (task.result or task.error or "").strip()
        if not payload:
            return JSONResponse({"ok": False, "detail": "No output to deliver"})

        # Keep this lightweight: deliver the stored in-character output verbatim.

        await agent.bus.publish_outbound(
            OutboundMessage(
                channel=task.origin_channel,
                chat_id=task.origin_chat_id,
                content=payload,
                is_background=True,
                metadata={"source": "redeliver", "task_id": task.id},
            )
        )
        return JSONResponse({"ok": True})

    @app.post("/agent/turn", dependencies=[Depends(require)])
    async def agent_turn(body: dict[str, Any]) -> JSONResponse:
        """Inject a message into the agent as if from an internal source.

        Used by the dashboard to trigger on-demand operations like security scans.
        """
        from kyber.bus.events import InboundMessage
        from datetime import datetime

        message = str(body.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        msg = InboundMessage(
            channel="dashboard",
            sender_id="dashboard",
            chat_id="dashboard",
            content=message,
            timestamp=datetime.now(),
        )
        await agent.bus.publish_inbound(msg)
        return JSONResponse({"ok": True, "message": "Message queued for agent"})

    @app.post("/security/scan", dependencies=[Depends(require)])
    async def direct_security_scan() -> JSONResponse:
        """Spawn a security scan worker directly, bypassing the LLM intent system.

        This is faster and more reliable than routing through /agent/turn because
        it doesn't depend on the chat model being available or responsive.
        The task description is built from a shared module so dashboard and
        chat-triggered scans are always identical.
        """
        from kyber.security.scan import build_scan_description

        description, _report_path = build_scan_description()

        task = agent.registry.create(
            description=description,
            label="Full Security Scan",
            origin_channel="dashboard",
            origin_chat_id="dashboard",
            complexity="complex",
        )
        agent._spawn_task(task)

        return JSONResponse({
            "ok": True,
            "task_id": task.id,
            "reference": task.reference,
        })

    @app.post("/security/dismiss", dependencies=[Depends(require)])
    async def dismiss_finding(body: dict[str, Any]) -> JSONResponse:
        """Dismiss a security finding so it no longer appears in future scans."""
        from kyber.security.tracker import dismiss_issue

        fingerprint = str(body.get("fingerprint", "")).strip()
        if not fingerprint:
            raise HTTPException(status_code=400, detail="fingerprint is required")

        if dismiss_issue(fingerprint):
            return JSONResponse({"ok": True})
        raise HTTPException(status_code=404, detail="Finding not found")

    @app.post("/security/undismiss", dependencies=[Depends(require)])
    async def undismiss_finding(body: dict[str, Any]) -> JSONResponse:
        """Restore a previously dismissed finding."""
        from kyber.security.tracker import undismiss_issue

        fingerprint = str(body.get("fingerprint", "")).strip()
        if not fingerprint:
            raise HTTPException(status_code=400, detail="fingerprint is required")

        if undismiss_issue(fingerprint):
            return JSONResponse({"ok": True})
        raise HTTPException(status_code=404, detail="Finding not found or not dismissed")

    @app.get("/security/dismissed", dependencies=[Depends(require)])
    async def list_dismissed() -> JSONResponse:
        """List all dismissed findings."""
        from kyber.security.tracker import get_dismissed_issues
        return JSONResponse({"dismissed": get_dismissed_issues()})

    return app
