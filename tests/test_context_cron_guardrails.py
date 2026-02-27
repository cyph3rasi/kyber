from pathlib import Path

from kyber.agent.context import ContextBuilder


def test_system_instructions_include_cron_guardrails(tmp_path: Path) -> None:
    cb = ContextBuilder(tmp_path)
    text = cb._get_system_instructions()

    assert "For scheduled/cron tasks: execute as kyber directly using built-in tools and the configured provider/model." in text
    assert "Do NOT create standalone Python scripts for LLM reasoning." in text
    assert "Do NOT request separate API keys." in text
