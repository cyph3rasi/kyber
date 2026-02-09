"""Gateway HTTP API (local control plane).

Exposes running/completed tasks for the dashboard:
- list tasks
- cancel running tasks
- toggle per-task progress updates

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
        "current_action": t.current_action,
        "actions_completed": t.actions_completed[-10:],
        "progress_updates_enabled": t.progress_updates_enabled,
        "result": t.result,
        "error": t.error,
    }


def create_gateway_app(agent: Orchestrator, token: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    require = _require_token(token)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/tasks", dependencies=[Depends(require)])
    async def list_tasks() -> JSONResponse:
        active = agent.registry.get_active_tasks()
        history = agent.registry.get_history(limit=100)
        # Only include completed-ish statuses in history response.
        hist_filtered = [
            t for t in history if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        ]
        return JSONResponse(
            {
                "active": [_task_to_dict(t) for t in active],
                "history": [_task_to_dict(t) for t in hist_filtered[::-1]],
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
            return JSONResponse({"ok": True, "status": task.status.value})
        ok = agent.workers.cancel(task.id)
        if not ok:
            # Might have just finished.
            return JSONResponse({"ok": False, "status": task.status.value})
        return JSONResponse({"ok": True})

    @app.post("/tasks/{ref}/progress-updates", dependencies=[Depends(require)])
    async def toggle_progress_updates(ref: str, body: dict[str, Any]) -> JSONResponse:
        task = agent.registry.get_by_ref(ref)
        if not task:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="Task not found")
        enabled = bool(body.get("enabled", True))
        task.progress_updates_enabled = enabled
        return JSONResponse({"ok": True, "progress_updates_enabled": enabled})

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
        # Add the lightning prefix for consistency with completion pings.
        if not payload.startswith("⚡️"):
            payload = "⚡️ " + payload

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

    return app
