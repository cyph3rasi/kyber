"""CLI presentation -- spinner, kawaii faces, tool preview formatting.

Pure display functions and classes with no AIAgent dependency.
Used by AIAgent._execute_tool_calls for CLI feedback.
"""

import json
import os
import random
import shlex
import sys
import threading
import time

# ANSI escape codes for coloring tool failure indicators
_RED = "\033[31m"
_RESET = "\033[0m"


# =========================================================================
# Tool preview (one-line summary of a tool call's primary argument)
# =========================================================================

def build_tool_preview(tool_name: str, args: dict, max_len: int = 40) -> str:
    """Build a short preview of a tool call's primary argument for display."""
    primary_args = {
        "terminal": "command", "web_search": "query", "web_extract": "urls",
        "read_file": "path", "write_file": "path", "patch": "path",
        "search_files": "pattern", "browser_navigate": "url",
        "browser_click": "ref", "browser_type": "text",
        "image_generate": "prompt", "text_to_speech": "text",
        "vision_analyze": "question", "mixture_of_agents": "user_prompt",
        "skill_view": "name", "skills_list": "category",
        "schedule_cronjob": "name",
    }

    if tool_name == "process":
        action = args.get("action", "")
        sid = args.get("session_id", "")
        data = args.get("data", "")
        timeout_val = args.get("timeout")
        parts = [action]
        if sid:
            parts.append(sid[:16])
        if data:
            parts.append(f'"{data[:20]}"')
        if timeout_val and action == "wait":
            parts.append(f"{timeout_val}s")
        return " ".join(parts) if parts else None

    if tool_name == "todo":
        todos_arg = args.get("todos")
        merge = args.get("merge", False)
        if todos_arg is None:
            return "reading task list"
        elif merge:
            return f"updating {len(todos_arg)} task(s)"
        else:
            return f"planning {len(todos_arg)} task(s)"

    if tool_name == "session_search":
        query = args.get("query", "")
        return f"recall: \"{query[:25]}{'...' if len(query) > 25 else ''}\""

    if tool_name == "memory":
        action = args.get("action", "")
        target = args.get("target", "")
        if action == "add":
            content = args.get("content", "")
            return f"+{target}: \"{content[:25]}{'...' if len(content) > 25 else ''}\""
        elif action == "replace":
            return f"~{target}: \"{args.get('old_text', '')[:20]}\""
        elif action == "remove":
            return f"-{target}: \"{args.get('old_text', '')[:20]}\""
        return action

    if tool_name == "send_message":
        target = args.get("target", "?")
        msg = args.get("message", "")
        if len(msg) > 20:
            msg = msg[:17] + "..."
        return f"to {target}: \"{msg}\""

    if tool_name.startswith("rl_"):
        rl_previews = {
            "rl_list_environments": "listing envs",
            "rl_select_environment": args.get("name", ""),
            "rl_get_current_config": "reading config",
            "rl_edit_config": f"{args.get('field', '')}={args.get('value', '')}",
            "rl_start_training": "starting",
            "rl_check_status": args.get("run_id", "")[:16],
            "rl_stop_training": f"stopping {args.get('run_id', '')[:16]}",
            "rl_get_results": args.get("run_id", "")[:16],
            "rl_list_runs": "listing runs",
            "rl_test_inference": f"{args.get('num_steps', 3)} steps",
        }
        return rl_previews.get(tool_name)

    key = primary_args.get(tool_name)
    if not key:
        for fallback_key in ("query", "text", "command", "path", "name", "prompt"):
            if fallback_key in args:
                key = fallback_key
                break

    if not key or key not in args:
        return None

    value = args[key]
    if isinstance(value, list):
        value = value[0] if value else ""

    preview = str(value).strip()
    if not preview:
        return None
    if len(preview) > max_len:
        preview = preview[:max_len - 3] + "..."
    return preview


def format_duration(seconds: float) -> str:
    """Format elapsed time in a readable way without misleading 0.0s rounding."""
    try:
        s = max(0.0, float(seconds))
    except Exception:
        return "0s"

    if s < 1.0:
        ms = int(round(s * 1000.0))
        ms = max(1, ms)
        return f"{ms}ms"
    if s < 10.0:
        return f"{s:.1f}s"
    if s < 60.0:
        return f"{int(round(s))}s"

    mins = int(s // 60)
    secs = int(s % 60)
    if mins < 60:
        return f"{mins}m {secs}s" if secs else f"{mins}m"

    hours = mins // 60
    rem_mins = mins % 60
    if rem_mins:
        return f"{hours}h {rem_mins}m"
    return f"{hours}h"


# =========================================================================
# KawaiiSpinner
# =========================================================================

class KawaiiSpinner:
    """Animated spinner with kawaii faces for CLI feedback during tool execution."""

    SPINNERS = {
        'dots': ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'],
        'bounce': ['⠁', '⠂', '⠄', '⡀', '⢀', '⠠', '⠐', '⠈'],
        'grow': ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█', '▇', '▆', '▅', '▄', '▃', '▂'],
        'arrows': ['←', '↖', '↑', '↗', '→', '↘', '↓', '↙'],
        'star': ['✶', '✷', '✸', '✹', '✺', '✹', '✸', '✷'],
        'moon': ['🌑', '🌒', '🌓', '🌔', '🌕', '🌖', '🌗', '🌘'],
        'pulse': ['◜', '◠', '◝', '◞', '◡', '◟'],
        'brain': ['🧠', '💭', '💡', '✨', '💫', '🌟', '💡', '💭'],
        'sparkle': ['⁺', '˚', '*', '✧', '✦', '✧', '*', '˚'],
    }

    KAWAII_WAITING = ["", "", "", "", "", "", "", "", "", ""]

    KAWAII_THINKING = ["", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]

    THINKING_VERBS = [
        "pondering", "contemplating", "musing", "cogitating", "ruminating",
        "deliberating", "mulling", "reflecting", "processing", "reasoning",
        "analyzing", "computing", "synthesizing", "formulating", "brainstorming",
    ]

    def __init__(self, message: str = "", spinner_type: str = 'dots'):
        self.message = message
        self.spinner_frames = self.SPINNERS.get(spinner_type, self.SPINNERS['dots'])
        self.running = False
        self.thread = None
        self.frame_idx = 0
        self.start_time = None
        self.last_line_len = 0
        # Capture stdout NOW, before any redirect_stdout(devnull) from
        # child agents can replace sys.stdout with a black hole.
        self._out = sys.stdout

    def _write(self, text: str, end: str = '\n', flush: bool = False):
        """Write to the stdout captured at spinner creation time."""
        try:
            self._out.write(text + end)
            if flush:
                self._out.flush()
        except (ValueError, OSError):
            pass

    def _animate(self):
        while self.running:
            if os.getenv("HERMES_SPINNER_PAUSE"):
                time.sleep(0.1)
                continue
            frame = self.spinner_frames[self.frame_idx % len(self.spinner_frames)]
            elapsed = time.time() - self.start_time
            line = f"  {frame} {self.message} ({format_duration(elapsed)})"
            clear = '\r' + ' ' * self.last_line_len + '\r'
            self._write(clear + line, end='', flush=True)
            self.last_line_len = len(line)
            self.frame_idx += 1
            time.sleep(0.12)

    def start(self):
        if self.running:
            return
        self.running = True
        self.start_time = time.time()
        self.thread = threading.Thread(target=self._animate, daemon=True)
        self.thread.start()

    def update_text(self, new_message: str):
        self.message = new_message

    def stop(self, final_message: str = None):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        self._write('\r' + ' ' * (self.last_line_len + 5) + '\r', end='', flush=True)
        if final_message:
            self._write(f"  {final_message}", flush=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


# =========================================================================
# Kawaii face arrays (used by AIAgent._execute_tool_calls for spinner text)
# =========================================================================

KAWAII_SEARCH = [
    "♪(´ε` )", "(｡◕‿◕｡)", "ヾ(＾∇＾)", "(◕ᴗ◕✿)", "( ˘▽˘)っ",
    "٩(◕‿◕｡)۶", "(✿◠‿◠)", "♪～(´ε｀ )", "(ノ´ヮ`)ノ*:・゚✧", "＼(◎o◎)／",
]
KAWAII_READ = [
    "φ(゜▽゜*)♪", "( ˘▽˘)っ", "(⌐■_■)", "٩(｡•́‿•̀｡)۶", "(◕‿◕✿)",
    "ヾ(＠⌒ー⌒＠)ノ", "(✧ω✧)", "♪(๑ᴖ◡ᴖ๑)♪", "(≧◡≦)", "( ´ ▽ ` )ノ",
]
KAWAII_TERMINAL = [
    "ヽ(>∀<☆)ノ", "(ノ°∀°)ノ", "٩(^ᴗ^)۶", "ヾ(⌐■_■)ノ♪", "(•̀ᴗ•́)و",
    "┗(＾0＾)┓", "(｀・ω・´)", "＼(￣▽￣)／", "(ง •̀_•́)ง", "ヽ(´▽`)/",
]
KAWAII_BROWSER = [
    "(ノ°∀°)ノ", "(☞゚ヮ゚)☞", "( ͡° ͜ʖ ͡°)", "┌( ಠ_ಠ)┘", "(⊙_⊙)？",
    "ヾ(•ω•`)o", "(￣ω￣)", "( ˇωˇ )", "(ᵔᴥᵔ)", "＼(◎o◎)／",
]
KAWAII_CREATE = [
    "✧*。٩(ˊᗜˋ*)و✧", "(ﾉ◕ヮ◕)ﾉ*:・ﾟ✧", "ヽ(>∀<☆)ノ", "٩(♡ε♡)۶", "(◕‿◕)♡",
    "✿◕ ‿ ◕✿", "(*≧▽≦)", "ヾ(＾-＾)ノ", "(☆▽☆)", "°˖✧◝(⁰▿⁰)◜✧˖°",
]
KAWAII_SKILL = [
    "ヾ(＠⌒ー⌒＠)ノ", "(๑˃ᴗ˂)ﻭ", "٩(◕‿◕｡)۶", "(✿╹◡╹)", "ヽ(・∀・)ノ",
    "(ノ´ヮ`)ノ*:・ﾟ✧", "♪(๑ᴖ◡ᴖ๑)♪", "(◠‿◠)", "٩(ˊᗜˋ*)و", "(＾▽＾)",
    "ヾ(＾∇＾)", "(★ω★)/", "٩(｡•́‿•̀｡)۶", "(◕ᴗ◕✿)", "＼(◎o◎)／",
    "(✧ω✧)", "ヽ(>∀<☆)ノ", "( ˘▽˘)っ", "(≧◡≦) ♡", "ヾ(￣▽￣)",
]
KAWAII_THINK = [
    "(っ°Д°;)っ", "(；′⌒`)", "(・_・ヾ", "( ´_ゝ`)", "(￣ヘ￣)",
    "(。-`ω´-)", "( ˘︹˘ )", "(¬_¬)", "ヽ(ー_ー )ノ", "(；一_一)",
]
KAWAII_GENERIC = [
    "♪(´ε` )", "(◕‿◕✿)", "ヾ(＾∇＾)", "٩(◕‿◕｡)۶", "(✿◠‿◠)",
    "(ノ´ヮ`)ノ*:・ﾟ✧", "ヽ(>∀<☆)ノ", "(☆▽☆)", "( ˘▽˘)っ", "(≧◡≦)",
]


# =========================================================================
# Cute tool message (completion line that replaces the spinner)
# =========================================================================

def _detect_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Inspect a tool result string for signs of failure.

    Returns ``(is_failure, suffix)`` where *suffix* is an informational tag
    like ``" [exit 1]"`` for terminal failures, or ``" [error]"`` for generic
    failures.  On success, returns ``(False, "")``.
    """
    if result is None:
        return False, ""

    if tool_name == "terminal":
        try:
            data = json.loads(result)
            exit_code = data.get("exit_code")
            if exit_code is not None and exit_code != 0:
                return True, f" [exit {exit_code}]"
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        return False, ""

    # Generic heuristic for non-terminal tools
    lower = result[:500].lower()
    if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
        return True, " [error]"

    return False, ""


def get_cute_tool_message(
    tool_name: str, args: dict, duration: float, result: str | None = None,
) -> str:
    """Generate a formatted tool completion line for CLI quiet mode.

    Format: ``| {emoji} {verb:9} {detail}``

    When *result* is provided the line is checked for failure indicators.
    Failed tool calls get a red prefix and an informational suffix.
    """
    del duration
    is_failure, failure_suffix = _detect_tool_failure(tool_name, result)

    def _trunc(s, n=40):
        s = str(s)
        return (s[:n-3] + "...") if len(s) > n else s

    def _path(p, n=35):
        p = str(p)
        return ("..." + p[-(n-3):]) if len(p) > n else p

    def _wrap(line: str) -> str:
        """Normalize status line and append failure suffix when needed."""
        line = line.rstrip()
        if line.startswith("┊ "):
            line = line[2:]
        if not is_failure:
            return line
        return f"{line}{failure_suffix}"

    def _shell_style(command: str) -> tuple[str, str, str]:
        """Infer emoji/verb/detail from a shell command for richer status lines."""
        raw = " ".join(str(command or "").strip().split())
        if not raw:
            return "💻", "shell", "command"

        try:
            tokens = shlex.split(raw, posix=True)
        except Exception:
            tokens = raw.split()

        if not tokens:
            return "💻", "shell", _trunc(raw, 42)

        def _unwrap(parts: list[str]) -> list[str]:
            idx = 0
            while idx < len(parts):
                tok = parts[idx]
                if tok in {"sudo", "command", "nohup", "time"}:
                    idx += 1
                    continue
                if tok == "env":
                    idx += 1
                    while idx < len(parts) and "=" in parts[idx] and not parts[idx].startswith("-"):
                        idx += 1
                    continue
                if tok in {"bash", "sh", "zsh", "fish"} and idx + 2 < len(parts):
                    if parts[idx + 1] in {"-c", "-lc"}:
                        inner = parts[idx + 2]
                        try:
                            return _unwrap(shlex.split(inner, posix=True))
                        except Exception:
                            return [inner]
                break
            return parts[idx:] if idx < len(parts) else []

        tokens = _unwrap(tokens)
        if not tokens:
            return "💻", "shell", _trunc(raw, 42)

        cmd = tokens[0].lower()
        rest = " ".join(tokens[1:]).strip()

        styles = {
            "git": ("🌿", "git"),
            "gh": ("🌿", "git"),
            "rg": ("🔎", "search"),
            "grep": ("🔎", "search"),
            "find": ("🔎", "search"),
            "ls": ("📂", "list"),
            "tree": ("📂", "list"),
            "cat": ("📖", "read"),
            "head": ("📖", "read"),
            "tail": ("📖", "read"),
            "sed": ("🛠️", "edit"),
            "awk": ("🛠️", "edit"),
            "curl": ("🌐", "fetch"),
            "wget": ("🌐", "fetch"),
            "pytest": ("🧪", "test"),
            "npm": ("📦", "pkg"),
            "pnpm": ("📦", "pkg"),
            "yarn": ("📦", "pkg"),
            "pip": ("📦", "deps"),
            "pip3": ("📦", "deps"),
            "python": ("🐍", "python"),
            "python3": ("🐍", "python"),
            "node": ("🟢", "node"),
            "npx": ("🟢", "node"),
            "docker": ("🐳", "docker"),
            "podman": ("🐳", "docker"),
            "kubectl": ("☸️", "k8s"),
            "make": ("🏗️", "build"),
            "cargo": ("🦀", "rust"),
            "go": ("🔵", "go"),
            "uv": ("⚡", "uv"),
            "just": ("⚙️", "run"),
        }
        emoji, verb = styles.get(cmd, ("💻", "shell"))

        if cmd in {"bash", "sh", "zsh", "fish"}:
            detail = _trunc(raw, 42)
        elif rest:
            detail = f"{cmd} {rest}"
        else:
            detail = cmd
        return emoji, verb, detail

    if tool_name == "web_search":
        return _wrap(f"┊ 🔍 search    {_trunc(args.get('query', ''), 42)}")
    if tool_name == "web_extract":
        urls = args.get("urls", [])
        if urls:
            url = urls[0] if isinstance(urls, list) else str(urls)
            domain = url.replace("https://", "").replace("http://", "").split("/")[0]
            extra = f" +{len(urls)-1}" if len(urls) > 1 else ""
            return _wrap(f"┊ 📄 fetch     {_trunc(domain, 35)}{extra}")
        return _wrap(f"┊ 📄 fetch     pages")
    if tool_name == "web_crawl":
        url = args.get("url", "")
        domain = url.replace("https://", "").replace("http://", "").split("/")[0]
        return _wrap(f"┊ 🕸️  crawl     {_trunc(domain, 35)}")
    if tool_name in {"terminal", "exec"}:
        emoji, verb, detail = _shell_style(args.get("command", ""))
        return _wrap(f"┊ {emoji} {verb:9} {_trunc(detail, 42)}")
    if tool_name == "process":
        action = args.get("action", "?")
        sid = args.get("session_id", "")[:12]
        labels = {"list": "ls processes", "poll": f"poll {sid}", "log": f"log {sid}",
                  "wait": f"wait {sid}", "kill": f"kill {sid}", "write": f"write {sid}", "submit": f"submit {sid}"}
        return _wrap(f"┊ ⚙️  proc      {labels.get(action, f'{action} {sid}')}")
    if tool_name == "read_file":
        return _wrap(f"┊ 📖 read      {_path(args.get('path', ''))}")
    if tool_name == "write_file":
        return _wrap(f"┊ ✍️  write     {_path(args.get('path', ''))}")
    if tool_name == "patch":
        return _wrap(f"┊ 🔧 patch     {_path(args.get('path', ''))}")
    if tool_name == "search_files":
        pattern = _trunc(args.get("pattern", ""), 35)
        target = args.get("target", "content")
        verb = "find" if target == "files" else "grep"
        return _wrap(f"┊ 🔎 {verb:9} {pattern}")
    if tool_name == "browser_navigate":
        url = args.get("url", "")
        domain = url.replace("https://", "").replace("http://", "").split("/")[0]
        return _wrap(f"┊ 🌐 navigate  {_trunc(domain, 35)}")
    if tool_name == "browser_snapshot":
        mode = "full" if args.get("full") else "compact"
        return _wrap(f"┊ 📸 snapshot  {mode}")
    if tool_name == "browser_click":
        return _wrap(f"┊ 👆 click     {args.get('ref', '?')}")
    if tool_name == "browser_type":
        return _wrap(f"┊ ⌨️  type      \"{_trunc(args.get('text', ''), 30)}\"")
    if tool_name == "browser_scroll":
        d = args.get("direction", "down")
        arrow = {"down": "↓", "up": "↑", "right": "→", "left": "←"}.get(d, "↓")
        return _wrap(f"┊ {arrow}  scroll    {d}")
    if tool_name == "browser_back":
        return _wrap(f"┊ ◀️  back    ")
    if tool_name == "browser_press":
        return _wrap(f"┊ ⌨️  press     {args.get('key', '?')}")
    if tool_name == "browser_close":
        return _wrap(f"┊ 🚪 close     browser")
    if tool_name == "browser_get_images":
        return _wrap(f"┊ 🖼️  images    extracting")
    if tool_name == "browser_vision":
        return _wrap(f"┊ 👁️  vision    analyzing page")
    if tool_name == "todo":
        todos_arg = args.get("todos")
        merge = args.get("merge", False)
        if todos_arg is None:
            return _wrap(f"┊ 📋 plan      reading tasks")
        elif merge:
            return _wrap(f"┊ 📋 plan      update {len(todos_arg)} task(s)")
        else:
            return _wrap(f"┊ 📋 plan      {len(todos_arg)} task(s)")
    if tool_name == "session_search":
        return _wrap(f"┊ 🔍 recall    \"{_trunc(args.get('query', ''), 35)}\"")
    if tool_name == "memory":
        action = args.get("action", "?")
        target = args.get("target", "")
        if action == "add":
            return _wrap(f"┊ 🧠 memory    +{target}: \"{_trunc(args.get('content', ''), 30)}\"")
        elif action == "replace":
            return _wrap(f"┊ 🧠 memory    ~{target}: \"{_trunc(args.get('old_text', ''), 20)}\"")
        elif action == "remove":
            return _wrap(f"┊ 🧠 memory    -{target}: \"{_trunc(args.get('old_text', ''), 20)}\"")
        return _wrap(f"┊ 🧠 memory    {action}")
    if tool_name == "skills_list":
        return _wrap(f"┊ 📚 skills    list {args.get('category', 'all')}")
    if tool_name == "skill_view":
        return _wrap(f"┊ 📚 skill     {_trunc(args.get('name', ''), 30)}")
    if tool_name == "image_generate":
        return _wrap(f"┊ 🎨 create    {_trunc(args.get('prompt', ''), 35)}")
    if tool_name == "text_to_speech":
        return _wrap(f"┊ 🔊 speak     {_trunc(args.get('text', ''), 30)}")
    if tool_name == "vision_analyze":
        return _wrap(f"┊ 👁️  vision    {_trunc(args.get('question', ''), 30)}")
    if tool_name == "mixture_of_agents":
        return _wrap(f"┊ 🧠 reason    {_trunc(args.get('user_prompt', ''), 30)}")
    if tool_name == "send_message":
        return _wrap(f"┊ 📨 send      {args.get('target', '?')}: \"{_trunc(args.get('message', ''), 25)}\"")
    if tool_name == "schedule_cronjob":
        return _wrap(f"┊ ⏰ schedule  {_trunc(args.get('name', args.get('prompt', 'task')), 30)}")
    if tool_name == "list_cronjobs":
        return _wrap(f"┊ ⏰ jobs      listing")
    if tool_name == "remove_cronjob":
        return _wrap(f"┊ ⏰ remove    job {args.get('job_id', '?')}")
    if tool_name.startswith("rl_"):
        rl = {
            "rl_list_environments": "list envs", "rl_select_environment": f"select {args.get('name', '')}",
            "rl_get_current_config": "get config", "rl_edit_config": f"set {args.get('field', '?')}",
            "rl_start_training": "start training", "rl_check_status": f"status {args.get('run_id', '?')[:12]}",
            "rl_stop_training": f"stop {args.get('run_id', '?')[:12]}", "rl_get_results": f"results {args.get('run_id', '?')[:12]}",
            "rl_list_runs": "list runs", "rl_test_inference": "test inference",
        }
        return _wrap(f"┊ 🧪 rl        {rl.get(tool_name, tool_name.replace('rl_', ''))}")
    if tool_name == "execute_code":
        code = args.get("code", "")
        first_line = code.strip().split("\n")[0] if code.strip() else ""
        return _wrap(f"┊ 🐍 exec      {_trunc(first_line, 35)}")
    if tool_name == "delegate_task":
        tasks = args.get("tasks")
        if tasks and isinstance(tasks, list):
            return _wrap(f"┊ 🔀 delegate  {len(tasks)} parallel tasks")
        return _wrap(f"┊ 🔀 delegate  {_trunc(args.get('goal', ''), 35)}")

    # ── Kyber Network (remote tool invocation across paired machines) ──
    # The generic fallback would show things like "remote_in" and no
    # useful detail. These handlers show the target peer + what's
    # actually being done there so the tool stream reads naturally.
    if tool_name == "list_network_peers":
        return _wrap("┊ 🌐 peers     list paired machines")
    if tool_name == "exec_on":
        peer = args.get("peer_name", "?")
        cmd = args.get("command", "")
        emoji, _verb, detail = _shell_style(cmd)
        return _wrap(f"┊ {emoji} on {peer}: {_trunc(detail, 36 - len(str(peer)))}")
    if tool_name == "read_file_on":
        peer = args.get("peer_name", "?")
        return _wrap(f"┊ 📖 on {peer}: read {_path(args.get('path', ''), 30)}")
    if tool_name == "list_dir_on":
        peer = args.get("peer_name", "?")
        return _wrap(f"┊ 📂 on {peer}: ls {_path(args.get('path', ''), 30)}")
    if tool_name == "write_file_on":
        peer = args.get("peer_name", "?")
        return _wrap(f"┊ ✍️  on {peer}: write {_path(args.get('path', ''), 28)}")
    if tool_name == "edit_file_on":
        peer = args.get("peer_name", "?")
        return _wrap(f"┊ 🔧 on {peer}: edit {_path(args.get('path', ''), 28)}")
    if tool_name == "remote_invoke":
        peer = args.get("peer_name", "?")
        inner = args.get("tool_name", "?")
        return _wrap(f"┊ 🌐 on {peer}: {_trunc(inner, 36 - len(str(peer)))}")

    # ── Shared notebook (cross-machine key/value store) ──
    if tool_name == "notebook_write":
        return _wrap(f"┊ 📝 note      write {_trunc(args.get('key', ''), 32)}")
    if tool_name == "notebook_read":
        return _wrap(f"┊ 📖 note      read {_trunc(args.get('key', ''), 32)}")
    if tool_name == "notebook_list":
        tag = args.get("tag")
        return _wrap(f"┊ 📒 note      list" + (f" #{tag}" if tag else ""))
    if tool_name == "notebook_search":
        return _wrap(f"┊ 🔎 note      search {_trunc(args.get('query', ''), 30)}")

    preview = build_tool_preview(tool_name, args) or ""
    # Widen the tool-name column from 9 to 14 chars — long names like
    # ``list_network_peers`` and ``remote_invoke`` were getting truncated
    # to unreadable stubs like ``list_netw``.
    return _wrap(f"┊ ⚡ {_trunc(tool_name, 14):14} {_trunc(preview, 30)}")
