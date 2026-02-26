"""Kyber web dashboard server."""

from __future__ import annotations

import asyncio
import os
import json
import platform
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_413_REQUEST_ENTITY_TOO_LARGE

from kyber.config.loader import convert_keys, convert_to_camel, load_config, save_config
from kyber.config.schema import Config
from kyber.skillhub.manager import (
    install_from_source,
    remove_skill,
    list_managed_installs,
    update_all,
    preview_source,
    fetch_skill_md,
)
from kyber.skillhub.skills_sh import search_skills_sh
from kyber.agent.skills import SkillsLoader

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Known API base URLs for built-in providers
PROVIDER_BASES: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
}


def _restart_gateway_service() -> tuple[bool, str]:
    """Restart the gateway service via the platform's service manager."""
    system = platform.system()
    try:
        if system == "Darwin":
            plist = Path.home() / "Library" / "LaunchAgents" / "chat.kyber.gateway.plist"
            if not plist.exists():
                return False, "Gateway launchd plist not found"
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, timeout=10)
            subprocess.run(["launchctl", "load", str(plist)], capture_output=True, timeout=10, check=True)
        elif system == "Linux":
            subprocess.run(
                ["systemctl", "--user", "restart", "kyber-gateway.service"],
                capture_output=True, timeout=15, check=True,
    )

        else:
            return False, f"Unsupported platform: {system}"
    except subprocess.CalledProcessError as e:
        return False, f"Service restart failed: {e.stderr.decode().strip() if e.stderr else str(e)}"
    except Exception as e:
        return False, str(e)
    return True, "Gateway service restarted"


_CHATGPT_SUBSCRIPTION_LOGIN_LOCK = asyncio.Lock()
_CHATGPT_SUBSCRIPTION_LOGIN_STATE = {
    "task": None,
    "last_error": None,
    "auth_url": None,
}


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with Path("/proc/version").open(encoding="utf-8", errors="ignore") as fh:
            return "microsoft" in fh.read().lower()
    except Exception:
        return False


def _open_url_in_wsl(url: str) -> bool:
    try:
        subprocess.Popen(
            ["cmd.exe", "/C", "start", "", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        logger.warning(f"WSL browser launch failed: {e}")
        return False


def _restart_dashboard_service() -> tuple[bool, str]:
    """Restart the dashboard service via the platform's service manager.
    
    On macOS, uses a detached shell to unload/load the plist so the
    dashboard process can survive long enough to send the HTTP response.
    """
    system = platform.system()
    try:
        if system == "Darwin":
            plist = Path.home() / "Library" / "LaunchAgents" / "chat.kyber.dashboard.plist"
            if not plist.exists():
                return False, "Dashboard launchd plist not found"
            # Run unload+load in a detached shell with a small delay so the
            # HTTP response can be sent before the process is killed.
            subprocess.Popen(
                f'sleep 1 && launchctl unload "{plist}" && launchctl load "{plist}"',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif system == "Linux":
            subprocess.Popen(
                "sleep 1 && systemctl --user restart kyber-dashboard.service",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            return False, f"Unsupported platform: {system}"
    except Exception as e:
        return False, str(e)
    return True, "Dashboard service restarting..."


async def _fetch_models_openai_compat(api_base: str, api_key: str) -> list[str]:
    """Fetch models from an OpenAI-compatible /v1/models endpoint."""
    url = f"{api_base.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    models = []
    for m in data.get("data", []):
        model_id = m.get("id", "")
        if model_id:
            models.append(model_id)
    models.sort()
    return models


async def _fetch_models_anthropic(api_key: str) -> list[str]:
    """Fetch models from Anthropic's API."""
    url = "https://api.anthropic.com/v1/models"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    models = []
    for m in data.get("data", []):
        model_id = m.get("id", "")
        if model_id:
            models.append(model_id)
    models.sort()
    return models


async def _fetch_models_gemini(api_key: str) -> list[str]:
    """Fetch models from Google's Gemini API."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    models = []
    for m in data.get("models", []):
        # name is like "models/gemini-2.5-flash" — strip the prefix
        name = m.get("name", "")
        if name.startswith("models/"):
            name = name[len("models/"):]
        if name:
            models.append(name)
    models.sort()
    return models


async def fetch_provider_models(provider: str, api_key: str, api_base: str | None = None) -> list[str]:
    """Fetch available models for a provider."""
    provider = provider.strip().lower()

    if provider == "anthropic":
        return await _fetch_models_anthropic(api_key)

    if provider == "gemini":
        return await _fetch_models_gemini(api_key)

    # Everything else is OpenAI-compatible
    if provider == "custom":
        if not api_base:
            raise ValueError("api_base is required for custom providers")
        base = api_base
    elif provider in PROVIDER_BASES:
        base = api_base or PROVIDER_BASES[provider]
    else:
        # Unknown built-in — try OpenAI-compat with provided base
        if not api_base:
            raise ValueError(f"No known API base for provider '{provider}'")
        base = api_base

    return await _fetch_models_openai_compat(base, api_key)


def _chatgpt_subscription_models() -> list[str]:
    """Return OpenHands-supported ChatGPT subscription models."""
    try:
        from openhands.sdk.llm.auth.openai import OPENAI_CODEX_MODELS
        return sorted(str(m) for m in OPENAI_CODEX_MODELS)
    except Exception:
        # Safe fallback based on OpenHands docs.
        return sorted(
            [
                "gpt-5.1-codex-max",
                "gpt-5.1-codex-mini",
                "gpt-5.2",
                "gpt-5.2-codex",
            ]
        )


async def _run_chatgpt_subscription_login_task(_model: str, force_login: bool) -> None:
    """Run ChatGPT subscription login in a background task."""
    error: str | None = None
    try:
        import openhands.sdk.llm.auth.openai as openai_auth
        from openhands.sdk.llm.auth.openai import OpenAISubscriptionAuth

        # First check for valid credentials when not forcing a fresh login.
        if not force_login:
            existing_auth = OpenAISubscriptionAuth()
            existing_creds = await existing_auth.refresh_if_needed()
            if existing_creds is not None and not existing_creds.is_expired():
                logger.info("Using existing ChatGPT subscription credentials.")
                error = None
                async with _CHATGPT_SUBSCRIPTION_LOGIN_LOCK:
                    _CHATGPT_SUBSCRIPTION_LOGIN_STATE["last_error"] = None
                return

        preferred_ports: list[int] = []
        env_ports = os.getenv("OPENAI_SUBSCRIPTION_OAUTH_PORTS", "")
        if env_ports.strip():
            for raw in env_ports.split(","):
                value = raw.strip()
                try:
                    preferred_ports.append(int(value))
                except ValueError:
                    logger.warning(
                        "Ignoring invalid ChatGPT OAuth port value in OPENAI_SUBSCRIPTION_OAUTH_PORTS: "
                        f"{value!r}"
                    )
        if not preferred_ports:
            preferred_ports = list(range(1455, 1466))

        last_port_error: str | None = None
        wsl_env = _is_wsl()

        for port in sorted(set(preferred_ports)):
            auth = OpenAISubscriptionAuth(oauth_port=port)
            if force_login:
                auth.logout()

            original_open = openai_auth.webbrowser.open
            auth_url = None

            def _open(url, *args, **kwargs):  # type: ignore[override]
                nonlocal auth_url
                auth_url = str(url)
                _CHATGPT_SUBSCRIPTION_LOGIN_STATE["auth_url"] = auth_url
                return _open_with_fallback(url, *args, **kwargs)

            def _open_with_fallback(url, *args, **kwargs):
                if wsl_env:
                    return _open_url_in_wsl(url)
                return bool(original_open(url, *args, **kwargs))

            openai_auth.webbrowser.open = _open  # type: ignore[assignment]
            try:
                await auth.login(open_browser=True)
                async with _CHATGPT_SUBSCRIPTION_LOGIN_LOCK:
                    if auth_url:
                        _CHATGPT_SUBSCRIPTION_LOGIN_STATE["auth_url"] = auth_url
                error = None
                async with _CHATGPT_SUBSCRIPTION_LOGIN_LOCK:
                    _CHATGPT_SUBSCRIPTION_LOGIN_STATE["last_error"] = None
                return
            except RuntimeError as login_error:
                message = str(login_error)
                last_port_error = message
                if "address already in use" in message.lower():
                    logger.warning(
                        f"ChatGPT OAuth callback port {port} is unavailable, trying next port."
                    )
                    continue
                raise
            finally:
                openai_auth.webbrowser.open = original_open  # type: ignore[assignment]
        error = (
            "Unable to start ChatGPT OAuth callback server. "
            f"{last_port_error or 'All candidate ports were unavailable.'}"
        )
    except Exception as e:
        logger.warning(f"ChatGPT subscription login worker failed: {e}")
        error = str(e)
    async with _CHATGPT_SUBSCRIPTION_LOGIN_LOCK:
        if error is not None:
            _CHATGPT_SUBSCRIPTION_LOGIN_STATE["last_error"] = error
        else:
            _CHATGPT_SUBSCRIPTION_LOGIN_STATE["last_error"] = None
        _CHATGPT_SUBSCRIPTION_LOGIN_STATE["task"] = None


async def _get_chatgpt_subscription_login_state() -> dict[str, Any]:
    async with _CHATGPT_SUBSCRIPTION_LOGIN_LOCK:
        task = _CHATGPT_SUBSCRIPTION_LOGIN_STATE["task"]
        if task is not None and task.done():
            _CHATGPT_SUBSCRIPTION_LOGIN_STATE["task"] = None
            task = None
        return {
            "in_progress": bool(task),
            "error": _CHATGPT_SUBSCRIPTION_LOGIN_STATE["last_error"],
            "authUrl": _CHATGPT_SUBSCRIPTION_LOGIN_STATE["auth_url"],
        }


async def _start_chatgpt_subscription_login_task(model: str, force_login: bool) -> bool:
    """Start a login task if one is not already running.

    Returns:
        True if a new task started, False if login is already in progress.
    """
    async with _CHATGPT_SUBSCRIPTION_LOGIN_LOCK:
        existing = _CHATGPT_SUBSCRIPTION_LOGIN_STATE["task"]
        if existing is not None and not existing.done():
            return False

        _CHATGPT_SUBSCRIPTION_LOGIN_STATE["last_error"] = None
        _CHATGPT_SUBSCRIPTION_LOGIN_STATE["auth_url"] = None
        _CHATGPT_SUBSCRIPTION_LOGIN_STATE["task"] = asyncio.create_task(
            _run_chatgpt_subscription_login_task(_model=model, force_login=force_login),
            name="chatgpt-subscription-login",
        )
        return True


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        response.headers[
            "Content-Security-Policy"
        ] = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self'; "
            "connect-src 'self'"
        )
        return response


class BodyLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if request.method in {"POST", "PUT", "PATCH"}:
            length = request.headers.get("content-length")
            if length:
                try:
                    if int(length) > MAX_BODY_BYTES:
                        return JSONResponse(
                            {"error": "Payload too large"},
                            status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        )
                except ValueError:
                    pass
        return await call_next(request)


def _require_token(request: Request) -> None:
    token = request.app.state.auth_token
    if not token:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    provided = auth_header[len("Bearer "):].strip()
    if not secrets.compare_digest(provided, token):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def _ensure_auth_token(config: Config) -> str:
    token = config.dashboard.auth_token.strip()
    if not token:
        token = secrets.token_urlsafe(32)
        config.dashboard.auth_token = token
        save_config(config)
    return token


def _build_allowed_hosts(config: Config) -> list[str]:
    allowed = sorted(LOCAL_HOSTS | set(config.dashboard.allowed_hosts))
    return allowed


def create_dashboard_app(config: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    app.state.auth_token = _ensure_auth_token(config)
    allowed_hosts = _build_allowed_hosts(config)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(BodyLimitMiddleware)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config", dependencies=[Depends(_require_token)])
    async def get_config() -> JSONResponse:
        config = load_config()
        payload = convert_to_camel(config.model_dump())
        return JSONResponse(payload)

    def _gateway_base() -> str:
        cfg = load_config()
        # Always talk to localhost; gateway binds based on its own config.
        return f"http://127.0.0.1:{cfg.gateway.port}"

    async def _proxy_gateway(
        request: Request,
        method: str,
        path: str,
        json_body: Any | None = None,
        timeout: float = 10.0,
    ) -> JSONResponse:
        token = request.app.state.auth_token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = _gateway_base() + path
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, headers=headers, json=json_body)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return JSONResponse(
                {"error": "Gateway is not running. Start it with: kyber gateway"},
                status_code=502,
            )
        except httpx.ReadTimeout:
            return JSONResponse(
                {"error": f"Gateway request timed out after {int(timeout)}s"},
                status_code=504,
            )
        if path.startswith("/chat/") and resp.status_code == 404:
            return JSONResponse(
                {
                    "error": (
                        "Gateway is running without dashboard chat endpoints. "
                        "Restart the gateway service and try again."
                    )
                },
                status_code=502,
            )
        # Pass through status and body
        try:
            data = resp.json()
        except Exception:
            data = {"error": resp.text}
        return JSONResponse(data, status_code=resp.status_code)

    @app.get("/api/tasks", dependencies=[Depends(_require_token)])
    async def get_tasks(request: Request) -> JSONResponse:
        return await _proxy_gateway(request, "GET", "/tasks")

    @app.post("/api/tasks/{ref}/cancel", dependencies=[Depends(_require_token)])
    async def cancel_task(request: Request, ref: str) -> JSONResponse:
        return await _proxy_gateway(request, "POST", f"/tasks/{ref}/cancel")

    @app.post("/api/tasks/{ref}/progress-updates", dependencies=[Depends(_require_token)])
    async def toggle_task_progress_updates(request: Request, ref: str, body: dict[str, Any]) -> JSONResponse:
        return await _proxy_gateway(request, "POST", f"/tasks/{ref}/progress-updates", json_body=body)

    @app.post("/api/tasks/{ref}/redeliver", dependencies=[Depends(_require_token)])
    async def redeliver_task(request: Request, ref: str) -> JSONResponse:
        return await _proxy_gateway(request, "POST", f"/tasks/{ref}/redeliver")

    @app.post("/api/chat/turn", dependencies=[Depends(_require_token)])
    async def chat_turn(request: Request, body: dict[str, Any]) -> JSONResponse:
        return await _proxy_gateway(
            request,
            "POST",
            "/chat/turn",
            json_body=body,
            timeout=180.0,
        )

    @app.post("/api/chat/reset", dependencies=[Depends(_require_token)])
    async def chat_reset(request: Request, body: dict[str, Any] | None = None) -> JSONResponse:
        return await _proxy_gateway(
            request,
            "POST",
            "/chat/reset",
            json_body=body or {},
            timeout=30.0,
        )

    @app.get("/api/skills", dependencies=[Depends(_require_token)])
    async def get_skills() -> JSONResponse:
        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        skills = loader.list_skills(filter_unavailable=False)
        manifest = list_managed_installs()
        return JSONResponse({"skills": skills, "managed": manifest.get("installed", {})})

    @app.get("/api/skills/search", dependencies=[Depends(_require_token)])
    async def search_skills(q: str, limit: int = 10) -> JSONResponse:
        results = await search_skills_sh(q, limit=limit)
        return JSONResponse({"results": results})

    @app.post("/api/skills/install", dependencies=[Depends(_require_token)])
    async def install_skill(body: dict[str, Any]) -> JSONResponse:
        source = str(body.get("source", "") or "").strip()
        skill = (str(body.get("skill", "") or "").strip() or None)
        replace = bool(body.get("replace", False))
        if not source:
            raise HTTPException(status_code=400, detail="source is required")
        try:
            res = install_from_source(source, skill=skill, replace=replace)
            return JSONResponse(res)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/skills/remove/{name}", dependencies=[Depends(_require_token)])
    async def remove_skill_api(name: str) -> JSONResponse:
        try:
            res = remove_skill(name)
            return JSONResponse(res)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/skills/update-all", dependencies=[Depends(_require_token)])
    async def update_all_skills(body: dict[str, Any] | None = None) -> JSONResponse:
        replace = True
        if body and "replace" in body:
            replace = bool(body.get("replace"))
        res = update_all(replace=replace)
        return JSONResponse(res)

    @app.post("/api/skills/preview", dependencies=[Depends(_require_token)])
    async def preview_skill(body: dict[str, Any]) -> JSONResponse:
        source = str(body.get("source", "") or "").strip()
        if not source:
            raise HTTPException(status_code=400, detail="source is required")

        # Simple in-memory TTL cache (per dashboard process).
        cache: dict[str, tuple[float, dict[str, Any]]] = getattr(app.state, "skill_preview_cache", None)
        if cache is None:
            cache = {}
            app.state.skill_preview_cache = cache
        now = time.time()

        key = f"preview:{source}"
        if key in cache:
            ts, payload = cache[key]
            if now - ts < 60.0:
                return JSONResponse(payload)
            cache.pop(key, None)

        try:
            res = preview_source(source)
            cache[key] = (now, res)
            # Keep cache bounded
            if len(cache) > 40:
                # Drop an arbitrary entry
                cache.pop(next(iter(cache.keys())), None)
            return JSONResponse(res)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/skills/skillmd", dependencies=[Depends(_require_token)])
    async def skill_md(body: dict[str, Any]) -> JSONResponse:
        source = str(body.get("source", "") or "").strip()
        skill = str(body.get("skill", "") or "").strip()
        if not source:
            raise HTTPException(status_code=400, detail="source is required")
        if not skill:
            raise HTTPException(status_code=400, detail="skill is required")

        cache: dict[str, tuple[float, dict[str, Any]]] = getattr(app.state, "skill_md_cache", None)
        if cache is None:
            cache = {}
            app.state.skill_md_cache = cache
        now = time.time()

        key = f"skillmd:{source}:{skill}"
        if key in cache:
            ts, payload = cache[key]
            if now - ts < 120.0:
                return JSONResponse(payload)
            cache.pop(key, None)

        try:
            res = fetch_skill_md(source, skill)
            cache[key] = (now, res)
            if len(cache) > 40:
                cache.pop(next(iter(cache.keys())), None)
            return JSONResponse(res)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.put("/api/config", dependencies=[Depends(_require_token)])
    async def update_config(body: dict[str, Any]) -> JSONResponse:
        data = convert_keys(body)
        config = Config.model_validate(data)

        # Ensure token is not emptied accidentally
        if not config.dashboard.auth_token.strip():
            current = load_config()
            config.dashboard.auth_token = current.dashboard.auth_token.strip() or secrets.token_urlsafe(32)

        save_config(config)

        # Restart gateway so it picks up the new config
        gw_ok, gw_msg = _restart_gateway_service()

        payload = convert_to_camel(config.model_dump())
        payload["_gatewayRestarted"] = gw_ok
        payload["_gatewayMessage"] = gw_msg
        return JSONResponse(payload)

    @app.post("/api/restart-gateway", dependencies=[Depends(_require_token)])
    async def restart_gateway() -> JSONResponse:
        ok, msg = _restart_gateway_service()
        status = 200 if ok else 502
        return JSONResponse({"ok": ok, "message": msg}, status_code=status)

    @app.post("/api/restart-dashboard", dependencies=[Depends(_require_token)])
    async def restart_dashboard() -> JSONResponse:
        ok, msg = _restart_dashboard_service()
        status = 200 if ok else 502
        return JSONResponse({"ok": ok, "message": msg}, status_code=status)

    @app.get("/api/providers/chatgpt-subscription/status", dependencies=[Depends(_require_token)])
    @app.get("/api/providers/chatgpt_subscription/status", dependencies=[Depends(_require_token)])
    async def chatgpt_subscription_status() -> JSONResponse:
        """Return OAuth auth state for ChatGPT Plus/Pro subscription login."""
        try:
            from openhands.sdk.llm.auth.openai import OpenAISubscriptionAuth

            auth = OpenAISubscriptionAuth()
            creds = await auth.refresh_if_needed()
            authenticated = bool(creds is not None and not creds.is_expired())
            login_state = await _get_chatgpt_subscription_login_state()
            return JSONResponse(
                {
                    "authenticated": authenticated,
                    "models": _chatgpt_subscription_models(),
                    "loginInProgress": login_state["in_progress"],
                    "loginError": login_state["error"],
                    "authUrl": login_state.get("authUrl"),
                }
            )
        except Exception as e:
            logger.warning(f"Failed to read ChatGPT subscription status: {e}")
            return JSONResponse(
                {
                    "authenticated": False,
                    "models": _chatgpt_subscription_models(),
                    "loginInProgress": False,
                    "loginError": str(e),
                    "authUrl": None,
                }
            )

    @app.post("/api/providers/chatgpt-subscription/login", dependencies=[Depends(_require_token)])
    @app.post("/api/providers/chatgpt_subscription/login", dependencies=[Depends(_require_token)])
    async def chatgpt_subscription_login(body: dict[str, Any] | None = None) -> JSONResponse:
        """Run OpenHands OAuth login for ChatGPT Plus/Pro subscription access."""
        body = body or {}
        model = str(body.get("model", "") or "gpt-5.2-codex").strip()
        force_login = bool(body.get("forceLogin", False))
        allowed = set(_chatgpt_subscription_models())
        if model not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported subscription model: {model}",
            )

        try:
            started = await _start_chatgpt_subscription_login_task(
                model=model, force_login=force_login
            )
            if not started:
                login_state = await _get_chatgpt_subscription_login_state()
                return JSONResponse(
                    {
                        "authenticated": False,
                        "models": sorted(allowed),
                        "loginInProgress": True,
                        "loginError": login_state["error"],
                        "authUrl": login_state.get("authUrl"),
                        "message": "ChatGPT login is already in progress.",
                    },
                    status_code=409,
                )

            status = await _get_chatgpt_subscription_login_state()
            return JSONResponse(
                {
                    "authenticated": False,
                    "model": model,
                    "models": sorted(allowed),
                    "loginInProgress": status["in_progress"],
                    "loginError": status["error"],
                    "authUrl": status.get("authUrl"),
                    "message": "ChatGPT login started. Complete authorization in your browser.",
                },
                status_code=202,
            )
        except Exception as e:
            logger.warning(f"ChatGPT subscription login failed: {e}")
            return JSONResponse(
                {
                    "authenticated": False,
                    "error": str(e),
                    "models": sorted(allowed),
                    "loginInProgress": False,
                    "loginError": str(e),
                    "authUrl": None,
                },
                status_code=500,
            )

    @app.post("/api/providers/chatgpt-subscription/logout", dependencies=[Depends(_require_token)])
    @app.post("/api/providers/chatgpt_subscription/logout", dependencies=[Depends(_require_token)])
    async def chatgpt_subscription_logout() -> JSONResponse:
        """Remove cached OAuth credentials for ChatGPT subscription login."""
        try:
            from openhands.sdk.llm.auth.openai import OpenAISubscriptionAuth

            auth = OpenAISubscriptionAuth()
            removed = bool(auth.logout())
            return JSONResponse({"ok": True, "removed": removed})
        except Exception as e:
            logger.warning(f"Failed to clear ChatGPT subscription credentials: {e}")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.get("/api/providers/{provider_name}/models", dependencies=[Depends(_require_token)])
    async def get_provider_models(
        provider_name: str,
        api_key: str | None = Query(None, alias="apiKey"),
        api_base: str | None = Query(None, alias="apiBase"),
    ) -> JSONResponse:
        """Fetch available models for a provider."""
        try:
            if provider_name.strip().lower() in {"chatgpt-subscription", "chatgpt_subscription"}:
                return JSONResponse({"models": _chatgpt_subscription_models()})
            if not (api_key or "").strip():
                raise ValueError("apiKey is required")
            # Normalize empty string to None
            if api_base is not None:
                api_base = api_base.strip() or None
            logger.info(f"Fetching models for {provider_name}, api_base={api_base!r}")
            models = await fetch_provider_models(provider_name, api_key or "", api_base)
            return JSONResponse({"models": models, "modelListingUnsupported": False})
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code == 404:
                # Some OpenAI-compatible providers (e.g. MiniMax) do not expose
                # a /models endpoint. Keep setup usable by allowing manual model entry.
                logger.info(
                    f"Provider {provider_name} does not expose /models (api_base={api_base!r}); "
                    "falling back to manual model selection."
                )
                return JSONResponse(
                    {
                        "models": [],
                        "modelListingUnsupported": True,
                        "error": "This provider does not expose a models listing endpoint.",
                    }
                )
            logger.warning(f"Failed to fetch models for {provider_name}: {e}")
            return JSONResponse(
                {"error": str(e), "models": [], "modelListingUnsupported": False},
                status_code=502,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch models for {provider_name}: {e}")
            return JSONResponse(
                {"error": str(e), "models": [], "modelListingUnsupported": False},
                status_code=502,
            )

    # ── Cron Jobs API ──

    def _cron_service():
        from kyber.cron.paths import get_cron_store_path
        from kyber.cron.service import CronService
        cfg = load_config()
        store_path = get_cron_store_path()
        user_tz = cfg.agents.defaults.timezone or None
        return CronService(store_path, timezone=user_tz)

    def _job_to_dict(j) -> dict:
        import time as _time
        return {
            "id": j.id,
            "name": j.name,
            "enabled": j.enabled,
            "schedule": {
                "kind": j.schedule.kind,
                "atMs": j.schedule.at_ms,
                "everyMs": j.schedule.every_ms,
                "expr": j.schedule.expr,
                "tz": j.schedule.tz,
            },
            "payload": {
                "kind": j.payload.kind,
                "message": j.payload.message,
                "deliver": j.payload.deliver,
                "channel": j.payload.channel,
                "to": j.payload.to,
            },
            "state": {
                "nextRunAtMs": j.state.next_run_at_ms,
                "lastRunAtMs": j.state.last_run_at_ms,
                "lastStatus": j.state.last_status,
                "lastError": j.state.last_error,
            },
            "createdAtMs": j.created_at_ms,
            "updatedAtMs": j.updated_at_ms,
            "deleteAfterRun": j.delete_after_run,
        }

    @app.get("/api/cron/jobs", dependencies=[Depends(_require_token)])
    async def list_cron_jobs() -> JSONResponse:
        svc = _cron_service()
        jobs = svc.list_jobs(include_disabled=True)
        return JSONResponse({"jobs": [_job_to_dict(j) for j in jobs]})

    @app.post("/api/cron/jobs", dependencies=[Depends(_require_token)])
    async def create_cron_job(body: dict[str, Any]) -> JSONResponse:
        from kyber.cron.types import CronSchedule
        svc = _cron_service()
        name = str(body.get("name", "") or "").strip()
        message = str(body.get("message", "") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        sched_data = body.get("schedule", {})
        kind = sched_data.get("kind", "every")
        schedule = CronSchedule(
            kind=kind,
            at_ms=sched_data.get("atMs"),
            every_ms=sched_data.get("everyMs"),
            expr=sched_data.get("expr"),
            tz=sched_data.get("tz"),
        )

        job = svc.add_job(
            name=name,
            schedule=schedule,
            message=message,
            deliver=bool(body.get("deliver", False)),
            channel=body.get("channel") or None,
            to=body.get("to") or None,
            delete_after_run=bool(body.get("deleteAfterRun", False)),
        )
        return JSONResponse(_job_to_dict(job))

    @app.put("/api/cron/jobs/{job_id}", dependencies=[Depends(_require_token)])
    async def update_cron_job(job_id: str, body: dict[str, Any]) -> JSONResponse:
        from kyber.cron.types import CronSchedule
        svc = _cron_service()

        kwargs: dict[str, Any] = {}
        if "name" in body:
            kwargs["name"] = str(body["name"]).strip()
        if "message" in body:
            kwargs["message"] = str(body["message"]).strip()
        if "enabled" in body:
            kwargs["enabled"] = bool(body["enabled"])
        if "deliver" in body:
            kwargs["deliver"] = bool(body["deliver"])
        if "channel" in body:
            kwargs["channel"] = body["channel"] or None
        if "to" in body:
            kwargs["to"] = body["to"] or None
        if "deleteAfterRun" in body:
            kwargs["delete_after_run"] = bool(body["deleteAfterRun"])
        if "schedule" in body:
            sd = body["schedule"]
            kwargs["schedule"] = CronSchedule(
                kind=sd.get("kind", "every"),
                at_ms=sd.get("atMs"),
                every_ms=sd.get("everyMs"),
                expr=sd.get("expr"),
                tz=sd.get("tz"),
            )

        job = svc.update_job(job_id, **kwargs)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return JSONResponse(_job_to_dict(job))

    @app.delete("/api/cron/jobs/{job_id}", dependencies=[Depends(_require_token)])
    async def delete_cron_job(job_id: str) -> JSONResponse:
        svc = _cron_service()
        if svc.remove_job(job_id):
            return JSONResponse({"ok": True})
        raise HTTPException(status_code=404, detail="Job not found")

    @app.post("/api/cron/jobs/{job_id}/toggle", dependencies=[Depends(_require_token)])
    async def toggle_cron_job(job_id: str, body: dict[str, Any]) -> JSONResponse:
        svc = _cron_service()
        enabled = bool(body.get("enabled", True))
        job = svc.enable_job(job_id, enabled=enabled)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return JSONResponse(_job_to_dict(job))

    # ── Security Center API ──

    def _security_reports_dir() -> Path:
        return Path.home() / ".kyber" / "security" / "reports"

    def _load_security_report(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Derive timestamp from the filename (real system time) rather
            # than trusting whatever the agent wrote into the JSON.
            # Filename format: report_YYYY-MM-DDTHH-MM-SS.json
            stem = path.stem
            if stem.startswith("report_"):
                ts_part = stem[len("report_"):]
                parts = ts_part.split("T", 1)
                if len(parts) == 2:
                    data["timestamp"] = parts[0] + "T" + parts[1].replace("-", ":") + "Z"
            return data
        except Exception:
            return None

    def _filter_dismissed(data: dict[str, Any]) -> dict[str, Any]:
        """Remove dismissed findings from a report and recalculate the summary."""
        from kyber.security.tracker import _fingerprint, _load_tracker

        tracker = _load_tracker()
        dismissed_fps = {
            fp for fp, issue in tracker.get("issues", {}).items()
            if issue.get("status") == "dismissed"
        }
        if dismissed_fps and data.get("findings"):
            data["findings"] = [
                f for f in data["findings"]
                if _fingerprint(f) not in dismissed_fps
            ]
            remaining = data["findings"]
            sev_weights = {"critical": 20, "high": 10, "medium": 5, "low": 2}
            sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
            for f in remaining:
                sev = f.get("severity", "low").lower()
                if sev in sev_counts:
                    sev_counts[sev] += 1
            deductions = sum(sev_counts[s] * w for s, w in sev_weights.items())
            data["summary"] = {
                "score": max(0, 100 - deductions),
                "total_findings": len(remaining),
                **sev_counts,
            }
            # Recalculate per-category finding_count so dismissed findings
            # are no longer reflected in the category table.
            if data.get("categories"):
                cat_counts: dict[str, int] = {}
                for f in remaining:
                    cat = f.get("category", "")
                    cat_counts[cat] = cat_counts.get(cat, 0) + 1
                for key, info in data["categories"].items():
                    new_count = cat_counts.get(key, 0)
                    info["finding_count"] = new_count
                    if new_count == 0 and info.get("status") in ("warn", "fail"):
                        info["status"] = "pass"
        return data

    @app.get("/api/security/reports", dependencies=[Depends(_require_token)])
    async def list_security_reports(limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
        """List available security reports, newest first."""
        from kyber.security.tracker import update_tracker, get_tracker_summary, _load_tracker

        reports_dir = _security_reports_dir()
        if not reports_dir.exists():
            return JSONResponse({"reports": [], "latest": None, "tracker": get_tracker_summary()})

        files = sorted(reports_dir.glob("report_*.json"), reverse=True)[:limit]
        reports = []
        for f in files:
            data = _load_security_report(f)
            if data:
                reports.append({
                    "filename": f.name,
                    "timestamp": data.get("timestamp", ""),
                    "summary": data.get("summary", {}),
                })

        latest = None
        if files:
            latest = _load_security_report(files[0])
            if latest:
                # Only run update_tracker if this report hasn't been processed yet.
                # Re-processing the same report would flip "new" findings to "recurring".
                tracker = _load_tracker()
                if tracker.get("last_processed_report") != files[0].name:
                    latest["_report_filename"] = files[0].name
                    update_tracker(latest)
                    latest.pop("_report_filename", None)

                # Filter out dismissed findings
                _filter_dismissed(latest)

        return JSONResponse({
            "reports": reports,
            "latest": latest,
            "tracker": get_tracker_summary(),
        })

    @app.get("/api/security/reports/{filename}", dependencies=[Depends(_require_token)])
    async def get_security_report(filename: str) -> JSONResponse:
        """Get a specific security report by filename."""
        # Sanitize filename to prevent path traversal
        safe_name = Path(filename).name
        if not safe_name.startswith("report_") or not safe_name.endswith(".json"):
            raise HTTPException(status_code=400, detail="Invalid report filename")

        report_path = _security_reports_dir() / safe_name
        if not report_path.exists():
            raise HTTPException(status_code=404, detail="Report not found")

        data = _load_security_report(report_path)
        if not data:
            raise HTTPException(status_code=500, detail="Failed to parse report")
        _filter_dismissed(data)
        return JSONResponse(data)

    @app.post("/api/security/scan", dependencies=[Depends(_require_token)])
    async def trigger_security_scan(request: Request) -> JSONResponse:
        """Trigger an immediate security scan via the gateway's direct spawn endpoint."""
        return await _proxy_gateway(request, "POST", "/security/scan")

    @app.get("/api/security/issues", dependencies=[Depends(_require_token)])
    async def list_security_issues() -> JSONResponse:
        """Return all tracked security issues with their status."""
        from kyber.security.tracker import _load_tracker
        tracker = _load_tracker()
        issues = list(tracker.get("issues", {}).values())
        # Sort: open issues first (new/recurring), then resolved; within each group by severity
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        status_order = {"new": 0, "recurring": 1, "resolved": 2}
        issues.sort(key=lambda i: (
            status_order.get(i.get("status", "new"), 9),
            sev_order.get(i.get("severity", "low"), 9),
        ))
        return JSONResponse({"issues": issues, "last_updated": tracker.get("last_updated")})

    @app.post("/api/security/dismiss", dependencies=[Depends(_require_token)])
    async def dismiss_finding(request: Request) -> JSONResponse:
        """Dismiss a security finding so it won't appear in future scans."""
        from kyber.security.tracker import dismiss_issue
        body = await request.json()
        fp = body.get("fingerprint", "")
        if not fp:
            return JSONResponse({"ok": False, "error": "Missing fingerprint"}, status_code=400)
        ok = dismiss_issue(fp)
        if not ok:
            return JSONResponse({"ok": False, "error": "Finding not found"}, status_code=404)
        return JSONResponse({"ok": True})

    @app.post("/api/security/undismiss", dependencies=[Depends(_require_token)])
    async def undismiss_finding(request: Request) -> JSONResponse:
        """Restore a dismissed security finding."""
        from kyber.security.tracker import undismiss_issue
        body = await request.json()
        fp = body.get("fingerprint", "")
        if not fp:
            return JSONResponse({"ok": False, "error": "Missing fingerprint"}, status_code=400)
        ok = undismiss_issue(fp)
        if not ok:
            return JSONResponse({"ok": False, "error": "Finding not found or not dismissed"}, status_code=404)
        return JSONResponse({"ok": True})

    # ── ClamAV Background Scan API ──

    @app.get("/api/security/clamscan", dependencies=[Depends(_require_token)])
    async def get_clamscan_report() -> JSONResponse:
        """Return the latest background ClamAV scan results and next scheduled run."""
        from kyber.security.clamscan import get_latest_report, get_scan_history, get_running_state

        report = get_latest_report()
        history = get_scan_history(limit=5)
        running = get_running_state()

        # Find the next scheduled clamscan run from cron
        next_run = None
        try:
            cron_svc = _cron_service()
            for job in cron_svc.list_jobs():
                if job.id == "kyber-clamscan" or "clamscan" in job.name.lower() or "clamav" in job.name.lower():
                    if job.state.next_run_at_ms:
                        from datetime import datetime, timezone
                        next_run = datetime.fromtimestamp(
                            job.state.next_run_at_ms / 1000, tz=timezone.utc
                        ).isoformat()
                    break
        except Exception:
            pass

        # Check if ClamAV is installed
        import shutil
        installed = bool(shutil.which("clamdscan") or shutil.which("clamscan"))

        return JSONResponse({
            "latest": report,
            "history": history,
            "next_run": next_run,
            "running": running,
            "installed": installed,
        })

    @app.post("/api/security/clamscan/run", dependencies=[Depends(_require_token)])
    async def trigger_clamscan() -> JSONResponse:
        """Trigger an immediate background ClamAV scan."""
        import asyncio
        from kyber.security.clamscan import run_clamscan, get_running_state

        if get_running_state():
            return JSONResponse({"ok": False, "message": "A ClamAV scan is already running"}, status_code=409)

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, run_clamscan)

        return JSONResponse({"ok": True, "message": "ClamAV scan started in background"})

    return app
