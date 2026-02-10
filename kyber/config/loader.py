"""Configuration loading utilities."""

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from kyber.config.schema import Config


# Keys in the camelCase config that are considered secrets.
# Paths are dot-separated relative to the root JSON object.
SECRET_PATHS: list[tuple[str, ...]] = [
    # Built-in providers
    ("providers", "openrouter", "apiKey"),
    ("providers", "anthropic", "apiKey"),
    ("providers", "openai", "apiKey"),
    ("providers", "deepseek", "apiKey"),
    ("providers", "groq", "apiKey"),
    ("providers", "gemini", "apiKey"),
    # Tools
    ("tools", "web", "search", "apiKey"),
    # Channels
    ("channels", "telegram", "token"),
    ("channels", "discord", "token"),
    # Dashboard
    ("dashboard", "authToken"),
]

# Mapping from config path → env var name written to .env
_ENV_MAP: dict[tuple[str, ...], str] = {
    ("providers", "openrouter", "apiKey"): "KYBER_PROVIDERS__OPENROUTER__API_KEY",
    ("providers", "anthropic", "apiKey"): "KYBER_PROVIDERS__ANTHROPIC__API_KEY",
    ("providers", "openai", "apiKey"): "KYBER_PROVIDERS__OPENAI__API_KEY",
    ("providers", "deepseek", "apiKey"): "KYBER_PROVIDERS__DEEPSEEK__API_KEY",
    ("providers", "groq", "apiKey"): "KYBER_PROVIDERS__GROQ__API_KEY",
    ("providers", "gemini", "apiKey"): "KYBER_PROVIDERS__GEMINI__API_KEY",
    ("tools", "web", "search", "apiKey"): "KYBER_TOOLS__WEB__SEARCH__API_KEY",
    ("channels", "telegram", "token"): "KYBER_CHANNELS__TELEGRAM__TOKEN",
    ("channels", "discord", "token"): "KYBER_CHANNELS__DISCORD__TOKEN",
    ("dashboard", "authToken"): "KYBER_DASHBOARD__AUTH_TOKEN",
}


def get_config_path() -> Path:
    """Get the default configuration file path."""
    return Path.home() / ".kyber" / "config.json"


def get_env_path() -> Path:
    """Get the default secrets .env file path."""
    return Path.home() / ".kyber" / ".env"


def get_data_dir() -> Path:
    """Get the kyber data directory."""
    from kyber.utils.helpers import get_data_path
    return get_data_path()


def _lock_file(path: Path) -> None:
    """Set file permissions to 600 (owner read/write only)."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Best-effort; Windows or restricted FS may not support this


def _load_dotenv(env_path: Path) -> dict[str, str]:
    """Parse a simple .env file into a dict (no shell expansion)."""
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Strip optional surrounding quotes
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key] = value
    return values


def _inject_env(env_path: Path) -> None:
    """Load .env values into os.environ (existing vars take precedence)."""
    for key, value in _load_dotenv(env_path).items():
        os.environ.setdefault(key, value)


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file + .env secrets.

    Resolution order (highest priority wins):
      1. Real environment variables (e.g. export KYBER_PROVIDERS__OPENROUTER__API_KEY=…)
      2. ~/.kyber/.env file
      3. ~/.kyber/config.json

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()
    env_path = get_env_path()

    # Inject .env into os.environ before Pydantic reads env vars
    _inject_env(env_path)

    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            config = Config.model_validate(convert_keys(data))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")
            config = Config()
    else:
        config = Config()

    # Overlay secrets from env vars — these take priority over empty
    # values left in config.json after migration.
    _apply_env_secrets(config)

    return config


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Secrets are written to ~/.kyber/.env (mode 600) and stripped from
    config.json so that the JSON file contains no credentials.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Convert to camelCase format
    data = config.model_dump()
    data = convert_to_camel(data)

    # Extract secrets → .env, blank them in the JSON payload
    env_path = get_env_path()
    _write_secrets_to_env(data, env_path)
    _strip_secrets(data)

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    # Lock down both files
    _lock_file(path)
    _lock_file(env_path)


# ── Secret extraction helpers ──


def _get_nested(data: dict, keys: tuple[str, ...]) -> str:
    """Retrieve a nested value from a dict by key path, returning '' on miss."""
    current: Any = data
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k, "")
        else:
            return ""
    return current if isinstance(current, str) else ""


def _set_nested(data: dict, keys: tuple[str, ...], value: str) -> None:
    """Set a nested value in a dict by key path."""
    current = data
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]
    current[keys[-1]] = value


def _write_secrets_to_env(data: dict, env_path: Path) -> None:
    """Extract secrets from camelCase config data and write to .env file."""
    # Load existing .env to preserve values not managed by us
    existing = _load_dotenv(env_path) if env_path.exists() else {}

    for config_keys, env_var in _ENV_MAP.items():
        value = _get_nested(data, config_keys)
        if value:
            existing[env_var] = value
        # Don't remove existing env vars if the config value is empty —
        # the user may have set it directly in .env

    # Also handle custom providers (dynamic list)
    custom_list = data.get("providers", {}).get("custom", [])
    for i, cp in enumerate(custom_list):
        name = (cp.get("name") or "").strip()
        api_key = cp.get("apiKey", "")
        if name and api_key:
            env_var = f"KYBER_CUSTOM_PROVIDER_{name.upper().replace('-', '_').replace(' ', '_')}_API_KEY"
            existing[env_var] = api_key

    # Write .env
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Kyber secrets — managed automatically. Do not commit this file.",
        "# Permissions should be 600 (owner read/write only).",
        "",
    ]
    for key in sorted(existing):
        val = existing[key]
        # Quote values that contain spaces or special chars
        if " " in val or '"' in val or "'" in val or "#" in val:
            val = '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines.append(f"{key}={val}")
    lines.append("")  # trailing newline
    env_path.write_text("\n".join(lines), encoding="utf-8")
    _lock_file(env_path)


def _strip_secrets(data: dict) -> None:
    """Remove secret values from camelCase config data (in-place)."""
    for config_keys in SECRET_PATHS:
        _set_nested(data, config_keys, "")

    # Strip custom provider API keys
    for cp in data.get("providers", {}).get("custom", []):
        cp["apiKey"] = ""


# Mapping from env var → attribute path on the Config object (snake_case)
_ENV_TO_ATTR: dict[str, tuple[str, ...]] = {
    "KYBER_PROVIDERS__OPENROUTER__API_KEY": ("providers", "openrouter", "api_key"),
    "KYBER_PROVIDERS__ANTHROPIC__API_KEY": ("providers", "anthropic", "api_key"),
    "KYBER_PROVIDERS__OPENAI__API_KEY": ("providers", "openai", "api_key"),
    "KYBER_PROVIDERS__DEEPSEEK__API_KEY": ("providers", "deepseek", "api_key"),
    "KYBER_PROVIDERS__GROQ__API_KEY": ("providers", "groq", "api_key"),
    "KYBER_PROVIDERS__GEMINI__API_KEY": ("providers", "gemini", "api_key"),
    "KYBER_TOOLS__WEB__SEARCH__API_KEY": ("providers",),  # handled specially below
    "KYBER_TOOLS__SKILL_SCANNER__LLM_API_KEY": ("tools", "skill_scanner", "llm_api_key"),
    "KYBER_TOOLS__SKILL_SCANNER__VIRUSTOTAL_API_KEY": ("tools", "skill_scanner", "virustotal_api_key"),
    "KYBER_TOOLS__SKILL_SCANNER__AI_DEFENSE_API_KEY": ("tools", "skill_scanner", "ai_defense_api_key"),
    "KYBER_CHANNELS__TELEGRAM__TOKEN": ("channels", "telegram", "token"),
    "KYBER_CHANNELS__DISCORD__TOKEN": ("channels", "discord", "token"),
    "KYBER_DASHBOARD__AUTH_TOKEN": ("dashboard", "auth_token"),
}


def _apply_env_secrets(config: Config) -> None:
    """Overlay secret values from environment variables onto the config object.

    This ensures that env vars (from .env or the real environment) take
    priority over empty strings left in config.json after migration.
    """
    for env_var, attr_path in _ENV_TO_ATTR.items():
        value = os.environ.get(env_var, "")
        if not value:
            continue

        # Special-case: tools.web.search.api_key
        if env_var == "KYBER_TOOLS__WEB__SEARCH__API_KEY":
            config.tools.web.search.api_key = value
            continue

        # Walk the attribute path on the config object
        obj: Any = config
        for part in attr_path[:-1]:
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if obj is not None:
            setattr(obj, attr_path[-1], value)

    # Handle custom provider keys
    for cp in config.providers.custom:
        name = (cp.name or "").strip()
        if not name:
            continue
        env_var = f"KYBER_CUSTOM_PROVIDER_{name.upper().replace('-', '_').replace(' ', '_')}_API_KEY"
        value = os.environ.get(env_var, "")
        if value:
            cp.api_key = value


def _config_has_secrets(config_path: Path | None = None) -> bool:
    """Check if config.json still contains non-empty secret values."""
    path = config_path or get_config_path()
    if not path.exists():
        return False
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False

    for config_keys in SECRET_PATHS:
        if _get_nested(data, config_keys):
            return True

    for cp in data.get("providers", {}).get("custom", []):
        if cp.get("apiKey"):
            return True

    return False


def migrate_secrets(config_path: Path | None = None) -> dict[str, int]:
    """
    Migrate secrets from config.json → .env.

    Returns a summary dict with counts of migrated and already-clean keys.
    """
    path = config_path or get_config_path()
    env_path = get_env_path()

    if not path.exists():
        return {"migrated": 0, "skipped": 0, "error": "config.json not found"}

    with open(path) as f:
        data = json.load(f)

    migrated = 0
    skipped = 0

    # Extract secrets to .env
    _write_secrets_to_env(data, env_path)

    for config_keys in SECRET_PATHS:
        val = _get_nested(data, config_keys)
        if val:
            migrated += 1
        else:
            skipped += 1

    for cp in data.get("providers", {}).get("custom", []):
        if cp.get("apiKey"):
            migrated += 1
        else:
            skipped += 1

    # Strip secrets from config.json and re-save
    _strip_secrets(data)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    # Lock down both files
    _lock_file(path)
    _lock_file(env_path)

    return {"migrated": migrated, "skipped": skipped}


# ── Key conversion helpers ──


def convert_keys(data: Any) -> Any:
    """Convert camelCase keys to snake_case for Pydantic."""
    if isinstance(data, dict):
        return {camel_to_snake(k): convert_keys(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_keys(item) for item in data]
    return data


def convert_to_camel(data: Any) -> Any:
    """Convert snake_case keys to camelCase."""
    if isinstance(data, dict):
        return {snake_to_camel(k): convert_to_camel(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_to_camel(item) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])
