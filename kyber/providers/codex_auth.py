"""ChatGPT OAuth credential handling for the Codex provider.

Kyber does not run its own OAuth flow. Instead, it shares credentials with
the official ``codex`` CLI by reading (and refreshing in place) the file at
``~/.codex/auth.json``. When the user installs/logs in via ``codex login``,
Kyber picks up the tokens transparently.

Token refresh uses the same endpoint and client_id as the Codex CLI:

    POST https://auth.openai.com/oauth/token
    Content-Type: application/x-www-form-urlencoded
    body: grant_type=refresh_token&refresh_token=...&client_id=...

On success the new tokens are written back to ``~/.codex/auth.json`` so the
Codex CLI stays in sync.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# Refresh if the access token expires within this many seconds.
REFRESH_SKEW_SECONDS = 120


class CodexAuthError(RuntimeError):
    """Raised when Codex credentials cannot be loaded or refreshed."""

    def __init__(self, message: str, *, relogin_required: bool = False) -> None:
        super().__init__(message)
        self.relogin_required = relogin_required


@dataclass
class CodexTokens:
    access_token: str
    refresh_token: str
    id_token: str
    account_id: str | None


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without signature verification.

    Used only to read the ``exp`` claim for refresh timing. We never trust
    claims for authorization decisions.
    """
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _access_token_is_expiring(access_token: str, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
    claims = _decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        # No exp claim — assume valid; let the API return 401 if not.
        return False
    return float(exp) <= (time.time() + max(0, int(skew_seconds)))


def find_codex_auth() -> Path | None:
    """Return the path to an existing ChatGPT-mode codex auth file, or None."""
    if not CODEX_AUTH_PATH.is_file():
        return None
    try:
        data = json.loads(CODEX_AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("auth_mode") != "chatgpt":
        return None
    tokens = data.get("tokens") or {}
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        return None
    return CODEX_AUTH_PATH


def load_tokens() -> CodexTokens:
    """Load tokens from ``~/.codex/auth.json``.

    Raises CodexAuthError if the file is missing, malformed, or uses a
    non-ChatGPT auth mode.
    """
    if not CODEX_AUTH_PATH.is_file():
        raise CodexAuthError(
            "No Codex credentials found at ~/.codex/auth.json. "
            "Run `codex login` first.",
            relogin_required=True,
        )
    try:
        data = json.loads(CODEX_AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CodexAuthError(f"Could not read ~/.codex/auth.json: {e}") from e

    if data.get("auth_mode") != "chatgpt":
        raise CodexAuthError(
            "~/.codex/auth.json is not in ChatGPT OAuth mode. "
            "Run `codex login` and choose 'Sign in with ChatGPT'.",
            relogin_required=True,
        )

    tokens = data.get("tokens") or {}
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    id_token = tokens.get("id_token") or ""
    account_id = tokens.get("account_id")

    if not isinstance(access_token, str) or not access_token:
        raise CodexAuthError(
            "Codex auth.json is missing access_token. Run `codex login` again.",
            relogin_required=True,
        )
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CodexAuthError(
            "Codex auth.json is missing refresh_token. Run `codex login` again.",
            relogin_required=True,
        )

    return CodexTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token if isinstance(id_token, str) else "",
        account_id=account_id if isinstance(account_id, str) else None,
    )


def _save_tokens(new_tokens: dict[str, Any]) -> None:
    """Write refreshed tokens back to ~/.codex/auth.json.

    Preserves unrelated fields and keeps the file 0600.
    """
    existing: dict[str, Any] = {}
    if CODEX_AUTH_PATH.is_file():
        try:
            existing = json.loads(CODEX_AUTH_PATH.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            existing = {}

    merged_tokens = dict(existing.get("tokens") or {})
    for key in ("access_token", "refresh_token", "id_token"):
        value = new_tokens.get(key)
        if isinstance(value, str) and value:
            merged_tokens[key] = value

    existing["auth_mode"] = "chatgpt"
    existing["tokens"] = merged_tokens
    existing["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"

    CODEX_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CODEX_AUTH_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, CODEX_AUTH_PATH)


async def refresh_tokens(tokens: CodexTokens, *, timeout_seconds: float = 20.0) -> CodexTokens:
    """Exchange the refresh token for a new access token.

    Writes the result back to ~/.codex/auth.json and returns updated tokens.
    """
    timeout = httpx.Timeout(max(5.0, float(timeout_seconds)))
    async with httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json"}) as client:
        resp = await client.post(
            CODEX_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": CODEX_OAUTH_CLIENT_ID,
            },
        )

    if resp.status_code != 200:
        body_snippet = resp.text[:300] if resp.text else ""
        if resp.status_code in (400, 401):
            raise CodexAuthError(
                f"Codex refresh token rejected (HTTP {resp.status_code}). "
                "Run `codex login` to re-authenticate.",
                relogin_required=True,
            )
        raise CodexAuthError(
            f"Codex token refresh failed (HTTP {resp.status_code}): {body_snippet}"
        )

    try:
        payload = resp.json()
    except ValueError as e:
        raise CodexAuthError(f"Codex token refresh returned non-JSON response: {e}") from e

    new_access = payload.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        raise CodexAuthError("Codex token refresh response missing access_token.")

    # OpenAI may or may not rotate the refresh_token / id_token. Preserve whatever
    # the server returned and fall back to the previous values.
    new_refresh = payload.get("refresh_token") or tokens.refresh_token
    new_id = payload.get("id_token") or tokens.id_token

    try:
        _save_tokens(
            {
                "access_token": new_access,
                "refresh_token": new_refresh,
                "id_token": new_id,
            }
        )
    except OSError as e:
        # Saving is a nice-to-have; proceed with in-memory tokens on failure.
        logger.warning("Could not persist refreshed Codex tokens: %s", e)

    return CodexTokens(
        access_token=new_access,
        refresh_token=new_refresh,
        id_token=new_id,
        account_id=tokens.account_id,
    )


async def ensure_fresh_tokens(tokens: CodexTokens | None = None) -> CodexTokens:
    """Return a token bundle whose access_token has not yet expired.

    Loads from disk if ``tokens`` is not provided. Refreshes if the current
    access token is within REFRESH_SKEW_SECONDS of expiry.
    """
    current = tokens or load_tokens()
    if _access_token_is_expiring(current.access_token):
        return await refresh_tokens(current)
    return current
