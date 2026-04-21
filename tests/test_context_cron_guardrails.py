from pathlib import Path

from kyber.agent.context import ContextBuilder


def test_system_instructions_include_cron_guardrails(tmp_path: Path) -> None:
    """The cron guardrails need to survive prompt-trim passes.

    These were added to stop the agent from writing standalone Python
    scripts for scheduled work or demanding its own API keys. The exact
    wording can change, but the two rules and the "re-run discovery =
    waste" efficiency rule must remain.
    """
    cb = ContextBuilder(tmp_path)
    text = cb._get_system_instructions()

    assert "standalone Python" in text
    assert "separate API keys" in text
    assert "## Cron" in text or "Cron" in text
    # Efficiency rule that stopped the agent from re-ls'ing the same dir.
    assert "discovery" in text.lower()
