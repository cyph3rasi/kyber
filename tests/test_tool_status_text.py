import unittest

from kyber.meta_messages import build_tool_status_text


def _is_single_sentence(s: str) -> bool:
    s = s.strip()
    return bool(s) and ("\n" not in s) and s[-1] in ".!?"


class TestToolStatusText(unittest.TestCase):
    def test_build_tool_status_text_known_tools(self) -> None:
        tools = [
            "read_file",
            "list_dir",
            "write_file",
            "edit_file",
            "exec",
            "web_search",
            "web_fetch",
            "message",
            "spawn",
            "task_status",
        ]
        for name in tools:
            text = build_tool_status_text(name)
            self.assertTrue(_is_single_sentence(text))
            self.assertLessEqual(len(text), 120)
            self.assertGreaterEqual(len(text.split()), 4)

    def test_build_tool_status_text_unknown_tool_fallback(self) -> None:
        text = build_tool_status_text("some_new_tool")
        self.assertTrue(_is_single_sentence(text))
        self.assertLessEqual(len(text), 120)
        self.assertGreaterEqual(len(text.split()), 4)
