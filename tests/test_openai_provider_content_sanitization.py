from kyber.providers.openai_provider import _strip_leading_think_blocks


def test_strip_leading_think_block_with_answer() -> None:
    raw = "<think>internal reasoning</think>\nFinal answer."
    assert _strip_leading_think_blocks(raw) == "Final answer."


def test_strip_multiple_leading_think_blocks_with_answer() -> None:
    raw = "<think>a</think>\n<think>b</think>\nFinal answer."
    assert _strip_leading_think_blocks(raw) == "Final answer."


def test_unwrap_when_only_think_block_exists() -> None:
    raw = "<think>Only visible text</think>"
    assert _strip_leading_think_blocks(raw) == "Only visible text"


def test_preserve_non_think_content() -> None:
    raw = "No think tags here."
    assert _strip_leading_think_blocks(raw) == raw

