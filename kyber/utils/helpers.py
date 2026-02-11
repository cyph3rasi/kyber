"""Utility functions for kyber."""

from pathlib import Path
from datetime import datetime


def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_path() -> Path:
    """Get the kyber data directory (~/.kyber)."""
    return ensure_dir(Path.home() / ".kyber")


def get_workspace_path(workspace: str | None = None) -> Path:
    """
    Get the workspace path.
    
    Args:
        workspace: Optional workspace path. Defaults to ~/.kyber/workspace.
    
    Returns:
        Expanded and ensured workspace path.
    """
    if workspace:
        path = Path(workspace).expanduser()
    else:
        path = Path.home() / ".kyber" / "workspace"
    return ensure_dir(path)


def get_sessions_path() -> Path:
    """Get the sessions storage directory."""
    return ensure_dir(get_data_path() / "sessions")


def get_memory_path(workspace: Path | None = None) -> Path:
    """Get the memory directory within the workspace."""
    ws = workspace or get_workspace_path()
    return ensure_dir(ws / "memory")


def get_skills_path(workspace: Path | None = None) -> Path:
    """Get the skills directory within the workspace."""
    ws = workspace or get_workspace_path()
    return ensure_dir(ws / "skills")


def today_date() -> str:
    """Get today's date in YYYY-MM-DD format."""
    return datetime.now().strftime("%Y-%m-%d")


def timestamp() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now().isoformat()


def current_datetime_str(timezone: str | None = None) -> str:
    """Get a human-readable current date/time string, optionally in a specific timezone.

    Returns something like: "2026-02-08 14:35 (Sunday) — America/New_York"
    Falls back to local system time if the timezone is invalid or not provided.
    """
    from zoneinfo import ZoneInfo

    try:
        if timezone:
            tz = ZoneInfo(timezone)
            now = datetime.now(tz)
            return now.strftime(f"%Y-%m-%d %H:%M (%A) — {timezone}")
    except Exception:
        pass

    now = datetime.now().astimezone()
    tz_name = now.strftime("%Z") or "local"
    return now.strftime(f"%Y-%m-%d %H:%M (%A) — {tz_name}")


def truncate_string(s: str, max_len: int = 100, suffix: str = "...") -> str:
    """Truncate a string to max length, adding suffix if truncated."""
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix
def redact_secrets(s: str) -> str:
    """Redact strings that look like API keys, tokens, passwords, or credentials.

    Applied to tool outputs before they enter the LLM conversation and to
    final user-facing messages. Intentionally aggressive — false positives
    (redacting a non-secret) are far less costly than leaking a real key.
    """
    import re

    # Key=value patterns: api_key="...", token: "...", password=..., etc.
    # Minimum 6 chars for the value to catch short passwords too.
    s = re.sub(
        r'(?i)(api[_-]?key|api[_-]?secret|consumer[_-]?key|consumer[_-]?secret'
        r'|access[_-]?token[_-]?secret|access[_-]?token|token|secret[_-]?key'
        r'|secret|password|passwd|bearer|authorization|credential'
        r')\s*[=:"\']\s*["\']?(\S{6,})["\']?',
        r'\1=***REDACTED***',
        s,
    )
    # Standalone key-like strings: sk-..., xai-..., ghp_..., AKIA..., etc.
    # Match both - and _ separators (GitHub uses ghp_, AWS uses AKIA directly)
    s = re.sub(r'\b(sk|key|xai|gsk|pk|rk|ghp|gho|glpat|xoxb|xoxp|AKIA)[_-][A-Za-z0-9_-]{16,}\b', '***REDACTED***', s)
    # AWS access key IDs (AKIA followed by 16 alphanumeric chars)
    s = re.sub(r'\bAKIA[A-Z0-9]{16}\b', '***REDACTED***', s)
    # Long hex strings (40+ chars) that look like tokens/hashes
    s = re.sub(r'\b[0-9a-fA-F]{40,}\b', '***REDACTED***', s)
    # Long base64-ish strings (30+ chars) after a key-like label
    s = re.sub(
        r'(?i)(?:key|token|secret|password|credential|bearer)\s*[=:"\']\s*["\']?([A-Za-z0-9+/=_-]{30,})["\']?',
        lambda m: m.group(0).split('=')[0] + '=***REDACTED***' if '=' in m.group(0) else '***REDACTED***',
        s,
    )
    return s


def safe_filename(name: str) -> str:
    """Convert a string to a safe filename."""
    # Replace unsafe characters
    unsafe = '<>:"/\\|?*'
    for char in unsafe:
        name = name.replace(char, "_")
    return name.strip()


def parse_session_key(key: str) -> tuple[str, str]:
    """
    Parse a session key into channel and chat_id.
    
    Args:
        key: Session key in format "channel:chat_id"
    
    Returns:
        Tuple of (channel, chat_id)
    """
    parts = key.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid session key: {key}")
    return parts[0], parts[1]
