"""Shell execution tool."""

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Any

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry


class ExecTool(Tool):
    """Tool to execute shell commands."""
    
    toolset = "terminal"
    
    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?<![-\w])\b(format|mkfs|diskpart)\b",  # disk operations (not --format flags)
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
            r"\bcrontab\s+-[re]\b",          # crontab -r (remove all), crontab -e (edit)
            r">\s*.*cron/jobs\.json\b",      # redirect overwrite cron/jobs.json
            r"\brm\s+.*cron/jobs\.json\b",   # rm cron/jobs.json
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
    
    @property
    def name(self) -> str:
        return "exec"
    
    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }
    
    # Commands that need extended timeouts (e.g. virus scanners, large builds).
    # Maps a regex pattern to a timeout in seconds (0 = no limit).
    EXTENDED_TIMEOUT_COMMANDS: dict[str, int] = {
        r"\bclamscan\b": 0,           # No limit — scan duration depends on disk size
        r"\bclamdscan\b": 0,          # No limit — daemon scan, depends on disk size
        r"\bfreshclam\b": 300,        # 5 minutes — signature download
        r"\bskill-scanner\b": 600,    # 10 minutes — depends on number of skills + LLM calls
    }

    INTERACTIVE_COMMAND_ERROR = (
        "Error: Command blocked because it may require interactive input/password and can stall the agent. "
        "Use a non-interactive form (for example: `sudo -n ...`, "
        "`apt-get install -y ...`, `ssh -o BatchMode=yes ...`)."
    )

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        # Check if this command needs an extended timeout
        timeout = self.timeout
        for pattern, ext_timeout in self.EXTENDED_TIMEOUT_COMMANDS.items():
            if re.search(pattern, command):
                # 0 means no limit — always honour that; otherwise take the larger value
                timeout = ext_timeout if ext_timeout == 0 else max(timeout, ext_timeout)
                break
        
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            
            try:
                if timeout > 0:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(),
                        timeout=timeout
                    )
                else:
                    # No timeout — let the command run to completion
                    stdout, stderr = await process.communicate()
            except asyncio.TimeoutError:
                process.kill()
                return f"Error: Command timed out after {timeout} seconds"
            
            output_parts = []
            
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            
            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")
            
            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")
            
            result = "\n".join(output_parts) if output_parts else "(no output)"
            
            # Truncate very long output
            max_len = 10000
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
            
            return result
            
        except Exception as e:
            return f"Error executing command: {str(e)}"

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        interactive_error = self._guard_interactive_command(cmd)
        if interactive_error:
            return interactive_error

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"\\']+", cmd)
            posix_paths = re.findall(r"/[^\s\"\\']+", cmd)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw).resolve()
                except Exception:
                    continue
                if cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    def _guard_interactive_command(self, command: str) -> str | None:
        """Block shell commands that commonly wait for user input."""
        lower = command.lower()

        if self._requires_sudo_password_prompt(command):
            return (
                "Error: Command blocked. `sudo` without `-n/--non-interactive` may prompt for a password "
                "and hang this task. Use `sudo -n ...` or run a non-sudo alternative."
            )

        if re.search(r"\b(passwd|su)\b", lower):
            return self.INTERACTIVE_COMMAND_ERROR

        if re.search(r"\b(gh|docker|npm|pip)\s+login\b", lower):
            return self.INTERACTIVE_COMMAND_ERROR

        if re.search(r"\bssh\b", lower):
            if not re.search(r"-o\s*batchmode\s*=\s*yes|-obatchmode=yes", lower):
                return (
                    "Error: Command blocked. `ssh` can prompt for passwords/host verification. "
                    "Use `ssh -o BatchMode=yes ...` for non-interactive execution."
                )

        if re.search(r"\b(apt|apt-get|yum|dnf|zypper|pacman)\b", lower) and re.search(r"\binstall\b", lower):
            if not re.search(r"\s(-y|--yes|--assume-yes|--noconfirm)\b", lower):
                return (
                    "Error: Command blocked. Package installation may prompt for confirmation. "
                    "Use non-interactive flags (for example `-y`/`--yes`/`--noconfirm`)."
                )

        return None

    def _requires_sudo_password_prompt(self, command: str) -> bool:
        """Return True when any sudo invocation lacks non-interactive flags."""
        try:
            tokens = shlex.split(command, posix=True)
        except Exception:
            # Conservative fallback for malformed shell strings.
            return bool(re.search(r"\bsudo\b", command.lower())) and not bool(
                re.search(r"\bsudo\b[^\n;&|]*\s(-n|--non-interactive)\b", command.lower())
            )

        separators = {"&&", "||", ";", "|"}
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok != "sudo":
                i += 1
                continue

            has_non_interactive = False
            j = i + 1
            while j < len(tokens):
                t = tokens[j]
                if t in separators:
                    break
                if t == "--":
                    break
                if t.startswith("-"):
                    if t == "-n" or t == "--non-interactive":
                        has_non_interactive = True
                    elif len(t) > 2 and t.startswith("-") and not t.startswith("--") and "n" in t[1:]:
                        # Combined short flags, e.g. sudo -kn true.
                        has_non_interactive = True
                    j += 1
                    continue
                break

            if not has_non_interactive:
                return True
            i = j + 1

        return False


# ── Self-register on import ─────────────────────────────────────────
registry.register(ExecTool())
