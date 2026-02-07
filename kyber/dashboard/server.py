"""Kyber web dashboard server."""

from __future__ import annotations

import platform
import secrets
import subprocess
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

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Known API base URLs for built-in providers
PROVIDER_BASES: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
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


def _restart_dashboard_service() -> tuple[bool, str]:
    """Restart the dashboard service via the platform's service manager."""
    system = platform.system()
    try:
        if system == "Darwin":
            plist = Path.home() / "Library" / "LaunchAgents" / "chat.kyber.dashboard.plist"
            if not plist.exists():
                return False, "Dashboard launchd plist not found"
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, timeout=10)
            subprocess.run(["launchctl", "load", str(plist)], capture_output=True, timeout=10, check=True)
        elif system == "Linux":
            subprocess.run(
                ["systemctl", "--user", "restart", "kyber-dashboard.service"],
                capture_output=True, timeout=15, check=True,
            )
        else:
            return False, f"Unsupported platform: {system}"
    except subprocess.CalledProcessError as e:
        return False, f"Service restart failed: {e.stderr.decode().strip() if e.stderr else str(e)}"
    except Exception as e:
        return False, str(e)
    return True, "Dashboard service restarted"


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

    @app.get("/api/providers/{provider_name}/models", dependencies=[Depends(_require_token)])
    async def get_provider_models(
        provider_name: str,
        api_key: str = Query(..., alias="apiKey"),
        api_base: str | None = Query(None, alias="apiBase"),
    ) -> JSONResponse:
        """Fetch available models for a provider."""
        try:
            # Normalize empty string to None
            if api_base is not None:
                api_base = api_base.strip() or None
            logger.info(f"Fetching models for {provider_name}, api_base={api_base!r}")
            models = await fetch_provider_models(provider_name, api_key, api_base)
            return JSONResponse({"models": models})
        except Exception as e:
            logger.warning(f"Failed to fetch models for {provider_name}: {e}")
            return JSONResponse(
                {"error": str(e), "models": []},
                status_code=502,
            )

    return app
