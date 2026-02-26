from kyber.meta_messages import describe_tool_action


def test_describe_tool_action_present_and_past() -> None:
    assert "shell" in describe_tool_action("exec", "present")
    assert describe_tool_action("exec", "past").startswith("ran")
    assert describe_tool_action("web_search", "present").startswith("search")
    assert describe_tool_action("web_search", "past").startswith("searched")


def test_describe_tool_action_unknown_tool() -> None:
    assert describe_tool_action("some_new_tool", "present")
    assert describe_tool_action("some_new_tool", "past")

