from pathlib import Path

from kyber.agent.context import ContextBuilder


def test_system_instructions_include_cron_guardrails(tmp_path: Path) -> None:
    cb = ContextBuilder(tmp_path)
    text = cb._get_system_instructions()

    assert "For scheduled/cron tasks: execute as kyber directly using built-in tools and the configured provider/model." in text
    assert "Do NOT create standalone Python scripts for LLM reasoning." in text
    assert "Do NOT request separate API keys." in text
    assert "### Strict Efficiency Rules" in text
    assert "Treat repeated discovery calls as a bug unless a refresh is actually needed." in text
    assert "Do NOT re-run the same `list_dir`/`read_file` on the same path in the same session" in text
