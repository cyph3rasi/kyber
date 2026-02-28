import os

from kyber.agent.tools.shell import ExecTool


def test_blocks_sudo_without_non_interactive_flag() -> None:
    tool = ExecTool()
    err = tool._guard_command("sudo ls /root", os.getcwd())
    assert err is not None
    assert "sudo" in err.lower()
    assert "non-interactive" in err.lower()


def test_allows_sudo_with_non_interactive_flag() -> None:
    tool = ExecTool()
    err = tool._guard_command("sudo -n ls /root", os.getcwd())
    assert err is None


def test_blocks_ssh_without_batchmode() -> None:
    tool = ExecTool()
    err = tool._guard_command("ssh user@example.com", os.getcwd())
    assert err is not None
    assert "batchmode" in err.lower()


def test_allows_ssh_with_batchmode() -> None:
    tool = ExecTool()
    err = tool._guard_command("ssh -o BatchMode=yes user@example.com", os.getcwd())
    assert err is None


def test_blocks_install_without_yes_flag() -> None:
    tool = ExecTool()
    err = tool._guard_command("apt-get install vim", os.getcwd())
    assert err is not None
    assert "non-interactive flags" in err.lower()


def test_allows_install_with_yes_flag() -> None:
    tool = ExecTool()
    err = tool._guard_command("apt-get install -y vim", os.getcwd())
    assert err is None
