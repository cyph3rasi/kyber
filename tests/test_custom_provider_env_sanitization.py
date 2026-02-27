from __future__ import annotations

from kyber.config.loader import (
    _apply_env_secrets,
    _load_dotenv,
    _write_secrets_to_env,
    custom_provider_env_key,
)
from kyber.config.schema import Config, CustomProviderConfig


def test_custom_provider_env_key_normalizes_name() -> None:
    assert (
        custom_provider_env_key("z.ai coding plan")
        == "KYBER_CUSTOM_PROVIDER_Z_AI_CODING_PLAN_API_KEY"
    )


def test_write_secrets_uses_canonical_custom_provider_key(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "KEEP_ME=1",
                "KYBER_CUSTOM_PROVIDER_Z.AI_CODING_PLAN_API_KEY=old-value",
                "",
            ]
        ),
        encoding="utf-8",
    )

    data = {
        "providers": {
            "custom": [
                {
                    "name": "z.ai coding plan",
                    "apiKey": "new-value",
                }
            ]
        }
    }

    _write_secrets_to_env(data, env_path)
    values = _load_dotenv(env_path)

    assert values["KEEP_ME"] == "1"
    assert values["KYBER_CUSTOM_PROVIDER_Z_AI_CODING_PLAN_API_KEY"] == "new-value"
    assert "KYBER_CUSTOM_PROVIDER_Z.AI_CODING_PLAN_API_KEY" not in values


def test_apply_env_secrets_reads_canonical_custom_provider_key(monkeypatch) -> None:
    config = Config()
    config.providers.custom = [CustomProviderConfig(name="z.ai coding plan", api_base="https://api.test")]
    monkeypatch.setenv("KYBER_CUSTOM_PROVIDER_Z_AI_CODING_PLAN_API_KEY", "env-key")

    _apply_env_secrets(config)

    assert config.providers.custom[0].api_key == "env-key"
