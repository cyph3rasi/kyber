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

    @app.post("/security/scan", dependencies=[Depends(require)])
    async def direct_security_scan() -> JSONResponse:
        """Spawn a security scan worker directly, bypassing the LLM intent system.

        This is faster and more reliable than routing through /agent/turn because
        it doesn't depend on the chat model being available or responsive.
        The task description includes all commands inline so the worker doesn't
        need to read the SKILL.md, saving many LLM turns.
        """
        from datetime import datetime as _dt, timezone as _tz

        ts = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H-%M-%S")
        report_path = f"~/.kyber/security/reports/report_{ts}.json"

        description = f"""Perform a security scan. Follow these steps EXACTLY.

## Step 1: Run all checks in ONE exec call

Run this combined script in a single `exec` tool call:

```
echo "===NETWORK==="
lsof -i -P -n 2>/dev/null | grep LISTEN || netstat -an 2>/dev/null | grep LISTEN
echo "===SSH==="
ls -la ~/.ssh/ 2>/dev/null
cat ~/.ssh/config 2>/dev/null | head -50
echo "===PERMISSIONS==="
ls -la ~/.ssh/id_* ~/.kyber/config.json ~/.kyber/.env 2>/dev/null
find ~ -maxdepth 3 -name ".env" -type f 2>/dev/null | head -10
echo "===SECRETS==="
grep -i "api_key\\|secret\\|token\\|password" ~/.bashrc ~/.zshrc ~/.bash_profile ~/.zprofile 2>/dev/null | head -20
echo "===SOFTWARE==="
brew outdated 2>/dev/null | head -10
echo "===PROCESSES==="
ps aux --sort=-%cpu 2>/dev/null | head -10 || ps aux -r 2>/dev/null | head -10
crontab -l 2>/dev/null
echo "===FIREWALL==="
/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null || sudo ufw status 2>/dev/null
echo "===DOCKER==="
docker ps 2>/dev/null || echo "Docker not running"
echo "===GIT==="
cat ~/.gitignore 2>/dev/null | head -10
echo "===KYBER==="
ls -la ~/.kyber/config.json ~/.kyber/.env 2>/dev/null
cat ~/.kyber/config.json 2>/dev/null | python3 -c "import sys,json; c=json.load(sys.stdin); [print(f'{{p}}: key set') if v.get('api_key') else print(f'{{p}}: no key') for p,v in c.get('providers',{{}}).items() if isinstance(v,dict)]" 2>/dev/null
echo "===MALWARE==="
which clamscan 2>/dev/null && echo "ClamAV installed" || echo "ClamAV not installed"
```

## Step 2: If ClamAV is installed, run malware scan

If the output shows "ClamAV installed", run TWO exec calls:
1. `freshclam 2>&1 || sudo freshclam 2>&1` (update signatures)
2. `clamscan -r --infected ~/ 2>&1` (full home directory scan)

If ClamAV is NOT installed, skip this and add a finding with id "MAL-000", category "malware", severity "medium", title "ClamAV not installed — malware scanning disabled", remediation "Run `kyber setup-clamav`".

## Step 3: Write the report

Create the directory and write the JSON report in ONE write_file call:

First: `exec` with `mkdir -p ~/.kyber/security/reports`

Then: `write_file` to `{report_path}` with this EXACT JSON structure:

```json
{{
  "version": 1,
  "timestamp": "<current ISO timestamp>",
  "duration_seconds": <elapsed seconds>,
  "summary": {{
    "total_findings": <count>,
    "critical": <count>,
    "high": <count>,
    "medium": <count>,
    "low": <count>,
    "score": <0-100, start at 100, deduct: critical=-20, high=-10, medium=-5, low=-2>
  }},
  "findings": [
    {{
      "id": "<CAT-NNN>",
      "category": "<network|ssh|permissions|secrets|software|processes|firewall|docker|git|kyber|malware>",
      "severity": "<critical|high|medium|low>",
      "title": "<short title>",
      "description": "<what's wrong>",
      "remediation": "<how to fix>",
      "evidence": "<sanitized command output>"
    }}
  ],
  "categories": {{
    "network": {{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}},
    "ssh": {{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}},
    "permissions": {{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}},
    "secrets": {{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}},
    "software": {{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}},
    "processes": {{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}},
    "firewall": {{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}},
    "docker": {{"checked": <true|false>, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}},
    "git": {{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}},
    "kyber": {{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}},
    "malware": {{"checked": <true if scanned>, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}
  }},
  "notes": "<conversational summary of findings and recommendations>"
}}
```

Category status: pass (no issues), warn (medium/low), fail (critical/high), skip (not applicable).
Never include actual secret values — only note presence/absence.

## Step 4: Clean up old reports

`exec`: `ls -t ~/.kyber/security/reports/report_*.json | tail -n +21 | xargs rm -f 2>/dev/null`

## Step 5: Deliver results

Your final message should summarize the key findings conversationally. Do NOT end on a tool call."""

        task = agent.registry.create(
            description=description,
            label="Full Security Scan",
            origin_channel="dashboard",
            origin_chat_id="dashboard",
            complexity="complex",
        )
        agent.workers.spawn(task)

        return JSONResponse({
            "ok": True,
            "task_id": task.id,
            "reference": task.reference,
        })

    return app
