"""Install/uninstall/status for the Kyber gateway and dashboard services.

Mirrors the service-setup block in the web installer (kyber.chat/install.sh)
so users who skipped service mode during onboarding can enable it later with
``kyber service install``. Also used for repair and removal.

Two platforms are supported:

* **macOS** — launchd user agents under ``~/Library/LaunchAgents/`` with
  labels ``chat.kyber.gateway`` and ``chat.kyber.dashboard``. Logs go to
  ``~/.kyber/logs/{gateway,dashboard}.{log,err.log}``.
* **Linux / WSL** — systemd user units at
  ``~/.config/systemd/user/kyber-{gateway,dashboard}.service``, with
  ``loginctl enable-linger`` so they survive logout.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

GATEWAY_LABEL = "chat.kyber.gateway"
DASHBOARD_LABEL = "chat.kyber.dashboard"
GATEWAY_UNIT = "kyber-gateway.service"
DASHBOARD_UNIT = "kyber-dashboard.service"


def running_under_service_manager() -> bool:
    """Return True when this process was launched by launchd or systemd.

    * systemd sets ``INVOCATION_ID`` in every service's environment.
    * launchd sets ``XPC_SERVICE_NAME`` matching the job label.

    We use these to gate the ``kyber gateway`` / ``kyber dashboard``
    entrypoints so they refuse to run standalone — running those
    daemons outside of a supervisor was the source of most of the
    recent port-conflict, orphan, and restart-loop pain.
    """
    if os.environ.get("INVOCATION_ID"):
        return True
    xpc = os.environ.get("XPC_SERVICE_NAME") or ""
    if xpc.startswith("chat.kyber."):
        return True
    # Explicit escape hatch for developers who genuinely want to run in
    # foreground (e.g. `KYBER_FORCE_FOREGROUND=1 kyber gateway`).
    if os.environ.get("KYBER_FORCE_FOREGROUND"):
        return True
    return False


def _pid_cmdline(pid: int) -> str:
    """Return the command line of a PID as a single string (empty on error)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return (result.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _pids_bound_to_port(port: int) -> list[int]:
    """Return PIDs currently listening on ``port``.

    Tries ``lsof`` first (available on macOS + most Linux), then ``ss``
    (Linux). Returns empty list if neither tool is available — callers
    fall back to trusting the OS.
    """
    pids: list[int] = []
    # lsof path (macOS + Linux). Single ``-iTCP:<port>`` filters to that
    # specific port; combining with a bare ``-iTCP`` would OR them and
    # return every listener on the box, which is exactly the bug we
    # shipped in 2026.4.21.30 that made every node think 18890 was busy.
    # ``-sTCP:LISTEN`` limits to listening sockets, ``-P -n`` keep the
    # output numeric, ``-t`` prints just PIDs.
    lsof = shutil.which("lsof")
    if lsof:
        try:
            result = subprocess.run(
                [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in (result.stdout or "").splitlines():
                try:
                    pids.append(int(line.strip()))
                except ValueError:
                    pass
            if pids:
                return pids
        except (OSError, subprocess.SubprocessError):
            pass

    ss = shutil.which("ss")
    if ss:
        try:
            result = subprocess.run(
                [ss, "-tlnp", f"sport = :{port}"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            # Output format:
            #   State  Recv-Q  Send-Q  Local ...  Process
            #   LISTEN 0       128     0.0.0.0:18790  users:(("kyber",pid=12345,fd=7))
            for line in (result.stdout or "").splitlines():
                if "pid=" not in line:
                    continue
                # Naive parse — grab every pid=N in the row
                import re as _re

                for m in _re.finditer(r"pid=(\d+)", line):
                    try:
                        pids.append(int(m.group(1)))
                    except ValueError:
                        pass
        except (OSError, subprocess.SubprocessError):
            pass
    return pids


def ensure_port_free(port: int, *, wait_seconds: float = 5.0) -> tuple[bool, str]:
    """Make sure ``port`` is free — killing squatting kyber processes if any.

    Returns ``(ok, message)``. On success message is empty; on failure it
    explains what's holding the port so the caller can show the user.
    Non-kyber processes are left alone (we just report them).

    Called from gateway / dashboard startup before binding so a stale
    process left over from a previous run can't force a crash-loop.
    """
    pids = _pids_bound_to_port(port)
    if not pids:
        return True, ""

    non_kyber: list[tuple[int, str]] = []
    kyber_killed: list[int] = []
    for pid in pids:
        if pid == os.getpid():
            continue
        cmdline = _pid_cmdline(pid)
        if "kyber" in cmdline:
            try:
                os.kill(pid, 15)  # SIGTERM
                kyber_killed.append(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                non_kyber.append((pid, cmdline + " (permission denied)"))
        else:
            non_kyber.append((pid, cmdline))

    if kyber_killed:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if not _pids_bound_to_port(port):
                break
            time.sleep(0.2)
        # Final SIGKILL pass for anything still alive.
        for pid in kyber_killed:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
        time.sleep(0.2)

    remaining = _pids_bound_to_port(port)
    if not remaining:
        return True, ""

    if non_kyber:
        details = ", ".join(f"PID {pid} ({cmd[:80]})" for pid, cmd in non_kyber)
        return False, f"port {port} held by non-kyber process(es): {details}"
    return False, f"port {port} still in use after SIGTERM+SIGKILL (pids: {remaining})"


def kill_orphan_kyber_processes(
    keep_pids: set[int] | None = None,
) -> list[int]:
    """Kill any ``kyber gateway`` / ``kyber dashboard`` processes not owned
    by the service manager.

    These show up when a user has run ``kyber gateway`` or ``kyber
    dashboard`` manually (from a shell) and never stopped it — the process
    then squats the TCP port and blocks launchd/systemd from starting its
    own managed copy, which crash-loops with "address already in use".

    Returns the PIDs that were signalled. Silently no-ops on platforms
    without ``pgrep`` (only some minimal Linux containers).
    """
    keep = keep_pids or set()
    killed: list[int] = []
    for cmd_fragment in ("kyber gateway", "kyber dashboard"):
        try:
            result = subprocess.run(
                ["pgrep", "-f", cmd_fragment],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode not in (0, 1):
            continue
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid == os.getpid() or pid in keep:
                continue
            try:
                os.kill(pid, 15)  # SIGTERM
                killed.append(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Someone else's process; nothing we can do.
                pass
    if killed:
        # Give them a moment to release their sockets before systemd/launchd
        # tries to bind. A hard SIGKILL fallback handles stuck cases.
        time.sleep(1.5)
        for pid in killed:
            try:
                os.kill(pid, 9)  # SIGKILL for anything that didn't exit
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
    return killed


class UnsupportedPlatformError(RuntimeError):
    """Raised when the current OS has no service backend we support."""


@dataclass
class UnitStatus:
    name: str  # user-facing service name, e.g. "gateway"
    installed: bool  # file on disk
    active: bool  # currently running
    identifier: str  # plist label or systemd unit name
    file_path: Path


@dataclass
class ServiceInfo:
    backend: str  # "launchd" | "systemd"
    gateway: UnitStatus
    dashboard: UnitStatus


def _detect_backend() -> str:
    system = platform.system()
    if system == "Darwin":
        return "launchd"
    if system == "Linux":
        if shutil.which("systemctl") is None:
            raise UnsupportedPlatformError(
                "systemctl is not available on this system. "
                "Kyber service mode on Linux requires systemd --user."
            )
        return "systemd"
    raise UnsupportedPlatformError(
        f"Kyber service mode is not supported on {system}. "
        "Run `kyber gateway` and `kyber dashboard` manually."
    )


def _resolve_kyber_bin() -> Path:
    """Return the absolute path to the ``kyber`` executable.

    Checks ``sys.argv[0]``, the current Python's scripts dir, and PATH.
    Falls back to ``~/.local/bin/kyber`` since that is where ``uv tool
    install kyber-chat`` drops it on both macOS and Linux.
    """
    candidates: list[Path] = []
    if sys.argv and sys.argv[0]:
        try:
            candidates.append(Path(sys.argv[0]).resolve())
        except OSError:
            pass
    from_path = shutil.which("kyber")
    if from_path:
        candidates.append(Path(from_path).resolve())
    candidates.append(Path.home() / ".local" / "bin" / "kyber")

    for c in candidates:
        try:
            if c.is_file() and os.access(c, os.X_OK) and c.name == "kyber":
                return c
        except OSError:
            continue
    # Last resort: trust the default path even if it doesn't exist yet.
    return Path.home() / ".local" / "bin" / "kyber"


# ── launchd (macOS) ────────────────────────────────────────────────────────


def _launchagent_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launch_plist_path(label: str) -> Path:
    return _launchagent_dir() / f"{label}.plist"


def _render_plist(label: str, kyber_bin: Path, subcommand: str, log_dir: Path) -> str:
    env_path = f"{Path.home()}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    # KeepAlive is a dict with SuccessfulExit=false so launchd only restarts
    # when the gateway exits non-zero (i.e. our supervisor raised the alarm).
    # A clean exit from `launchctl unload` or `kyber service uninstall`
    # won't trigger a relaunch.
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{kyber_bin}</string>
    <string>{subcommand}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>5</integer>
  <key>StandardOutPath</key>
  <string>{log_dir}/{subcommand}.log</string>
  <key>StandardErrorPath</key>
  <string>{log_dir}/{subcommand}.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{env_path}</string>
  </dict>
</dict>
</plist>
"""


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, timeout=15
    )


def _launchd_is_loaded(label: str) -> bool:
    """Return True if the given label is currently loaded for this user.

    ``launchctl list <label>`` exits 0 when loaded, non-zero otherwise.
    """
    try:
        uid = os.getuid()
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _launchd_install(kyber_bin: Path) -> tuple[UnitStatus, UnitStatus]:
    launch_dir = _launchagent_dir()
    log_dir = Path.home() / ".kyber" / "logs"
    launch_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    results: list[UnitStatus] = []
    for name, label in (("gateway", GATEWAY_LABEL), ("dashboard", DASHBOARD_LABEL)):
        plist_path = _launch_plist_path(label)
        plist_path.write_text(
            _render_plist(label, kyber_bin, name, log_dir), encoding="utf-8"
        )

        # Unload first so updates to the plist take effect.
        _launchctl("unload", str(plist_path))
        load_result = _launchctl("load", str(plist_path))
        if load_result.returncode != 0:
            # Kickstart as a fallback so an orphan job doesn't block reload.
            uid = os.getuid()
            _launchctl("kickstart", "-k", f"gui/{uid}/{label}")

        # Give launchd a moment to flip the state before probing.
        time.sleep(0.3)
        active = _launchd_is_loaded(label)
        results.append(
            UnitStatus(
                name=name,
                installed=plist_path.is_file(),
                active=active,
                identifier=label,
                file_path=plist_path,
            )
        )
    return results[0], results[1]


def _launchd_uninstall() -> tuple[UnitStatus, UnitStatus]:
    results: list[UnitStatus] = []
    for name, label in (("gateway", GATEWAY_LABEL), ("dashboard", DASHBOARD_LABEL)):
        plist_path = _launch_plist_path(label)
        _launchctl("unload", str(plist_path))
        removed = False
        if plist_path.is_file():
            try:
                plist_path.unlink()
                removed = True
            except OSError:
                removed = False
        results.append(
            UnitStatus(
                name=name,
                installed=not removed and plist_path.is_file(),
                active=False,
                identifier=label,
                file_path=plist_path,
            )
        )
    return results[0], results[1]


def _launchd_restart() -> tuple[UnitStatus, UnitStatus]:
    results: list[UnitStatus] = []
    for name, label in (("gateway", GATEWAY_LABEL), ("dashboard", DASHBOARD_LABEL)):
        plist_path = _launch_plist_path(label)
        if plist_path.is_file():
            _launchctl("unload", str(plist_path))
            load_result = _launchctl("load", str(plist_path))
            if load_result.returncode != 0:
                uid = os.getuid()
                _launchctl("kickstart", "-k", f"gui/{uid}/{label}")
        time.sleep(0.3)
        results.append(
            UnitStatus(
                name=name,
                installed=plist_path.is_file(),
                active=_launchd_is_loaded(label),
                identifier=label,
                file_path=plist_path,
            )
        )
    return results[0], results[1]


def _launchd_status() -> tuple[UnitStatus, UnitStatus]:
    results: list[UnitStatus] = []
    for name, label in (("gateway", GATEWAY_LABEL), ("dashboard", DASHBOARD_LABEL)):
        plist_path = _launch_plist_path(label)
        results.append(
            UnitStatus(
                name=name,
                installed=plist_path.is_file(),
                active=_launchd_is_loaded(label),
                identifier=label,
                file_path=plist_path,
            )
        )
    return results[0], results[1]


# ── systemd (Linux) ────────────────────────────────────────────────────────


def _systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _render_unit(description: str, kyber_bin: Path, subcommand: str) -> str:
    env_path = f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    # systemd requires a fully-qualified path for ExecStart.
    #
    # Restart=on-failure (not always) so a clean exit actually stops the
    # service. The gateway returns non-zero when a subtask dies unexpectedly,
    # so real problems still get auto-restarted; deliberate stops don't loop.
    return f"""[Unit]
Description={description}
{'Wants=network-online.target' if subcommand == 'gateway' else ''}
After={'network-online.target' if subcommand == 'gateway' else 'network.target'}

[Service]
Type=simple
ExecStart={kyber_bin} {subcommand}
WorkingDirectory={Path.home()}
Restart=on-failure
RestartSec=5
Environment=PATH={env_path}

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=check,
    )


def _systemd_is_active(unit: str) -> bool:
    try:
        result = _systemctl("is-active", unit)
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _systemd_install(kyber_bin: Path) -> tuple[UnitStatus, UnitStatus]:
    unit_dir = _systemd_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)

    results: list[UnitStatus] = []
    for name, unit, description in (
        ("gateway", GATEWAY_UNIT, "Kyber Gateway"),
        ("dashboard", DASHBOARD_UNIT, "Kyber Dashboard"),
    ):
        unit_path = unit_dir / unit
        unit_path.write_text(_render_unit(description, kyber_bin, name), encoding="utf-8")

    _systemctl("daemon-reload")
    _systemctl("enable", GATEWAY_UNIT)
    _systemctl("enable", DASHBOARD_UNIT)
    _systemctl("reset-failed", GATEWAY_UNIT, DASHBOARD_UNIT)

    for name, unit in (("gateway", GATEWAY_UNIT), ("dashboard", DASHBOARD_UNIT)):
        # Restart if already running, otherwise start.
        if _systemd_is_active(unit):
            _systemctl("restart", unit)
        else:
            _systemctl("start", unit)
        # Wait up to ~3s for the unit to reach "active".
        for _ in range(10):
            if _systemd_is_active(unit):
                break
            time.sleep(0.3)
        results.append(
            UnitStatus(
                name=name,
                installed=(unit_dir / unit).is_file(),
                active=_systemd_is_active(unit),
                identifier=unit,
                file_path=unit_dir / unit,
            )
        )

    # Keep user services running after logout on headless boxes.
    if shutil.which("loginctl") is not None:
        try:
            subprocess.run(
                ["loginctl", "enable-linger", os.environ.get("USER", "")],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    return results[0], results[1]


def _systemd_uninstall() -> tuple[UnitStatus, UnitStatus]:
    results: list[UnitStatus] = []
    for name, unit in (("gateway", GATEWAY_UNIT), ("dashboard", DASHBOARD_UNIT)):
        _systemctl("stop", unit)
        _systemctl("disable", unit)
        unit_path = _systemd_dir() / unit
        removed = False
        if unit_path.is_file():
            try:
                unit_path.unlink()
                removed = True
            except OSError:
                removed = False
        results.append(
            UnitStatus(
                name=name,
                installed=not removed and unit_path.is_file(),
                active=False,
                identifier=unit,
                file_path=unit_path,
            )
        )
    _systemctl("daemon-reload")
    return results[0], results[1]


def _systemd_restart() -> tuple[UnitStatus, UnitStatus]:
    results: list[UnitStatus] = []
    for name, unit in (("gateway", GATEWAY_UNIT), ("dashboard", DASHBOARD_UNIT)):
        unit_path = _systemd_dir() / unit
        if unit_path.is_file():
            _systemctl("restart", unit)
            for _ in range(10):
                if _systemd_is_active(unit):
                    break
                time.sleep(0.3)
        results.append(
            UnitStatus(
                name=name,
                installed=unit_path.is_file(),
                active=_systemd_is_active(unit),
                identifier=unit,
                file_path=unit_path,
            )
        )
    return results[0], results[1]


def _systemd_status() -> tuple[UnitStatus, UnitStatus]:
    results: list[UnitStatus] = []
    for name, unit in (("gateway", GATEWAY_UNIT), ("dashboard", DASHBOARD_UNIT)):
        unit_path = _systemd_dir() / unit
        results.append(
            UnitStatus(
                name=name,
                installed=unit_path.is_file(),
                active=_systemd_is_active(unit),
                identifier=unit,
                file_path=unit_path,
            )
        )
    return results[0], results[1]


# ── Public API ─────────────────────────────────────────────────────────────


def install_services() -> ServiceInfo:
    backend = _detect_backend()
    kyber_bin = _resolve_kyber_bin()
    # Any `kyber gateway` / `kyber dashboard` the user ran manually before
    # enabling service mode will squat the TCP port and crash-loop the real
    # service. Nuke them before we hand off to launchd/systemd.
    kill_orphan_kyber_processes()
    if backend == "launchd":
        gw, dash = _launchd_install(kyber_bin)
    else:
        gw, dash = _systemd_install(kyber_bin)
    return ServiceInfo(backend=backend, gateway=gw, dashboard=dash)


def uninstall_services() -> ServiceInfo:
    backend = _detect_backend()
    if backend == "launchd":
        gw, dash = _launchd_uninstall()
    else:
        gw, dash = _systemd_uninstall()
    return ServiceInfo(backend=backend, gateway=gw, dashboard=dash)


def restart_services() -> ServiceInfo:
    backend = _detect_backend()
    kyber_bin = _resolve_kyber_bin()
    # Clear stray manually-run kyber processes before asking the service
    # manager to restart. Without this, an orphan still holds the port and
    # the freshly-started service crash-loops with EADDRINUSE.
    kill_orphan_kyber_processes()
    # Always re-render the unit/plist files first. If a past version
    # wrote a file with an older (buggy) template — e.g. Restart=always
    # or KeepAlive=true without SuccessfulExit=false — a plain reload
    # would preserve the stale template. Re-rendering guarantees the
    # running services pick up whatever template the current wheel ships.
    if backend == "launchd":
        _launchd_install(kyber_bin)  # rewrites plists and reloads
        gw, dash = _launchd_status()
    else:
        _systemd_install(kyber_bin)  # rewrites unit files, daemon-reload, restart
        gw, dash = _systemd_status()
    return ServiceInfo(backend=backend, gateway=gw, dashboard=dash)


def service_status() -> ServiceInfo:
    backend = _detect_backend()
    if backend == "launchd":
        gw, dash = _launchd_status()
    else:
        gw, dash = _systemd_status()
    return ServiceInfo(backend=backend, gateway=gw, dashboard=dash)
