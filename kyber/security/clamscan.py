"""Background ClamAV scanner.

Runs clamscan/clamdscan as a subprocess and writes results to
~/.kyber/security/clamscan/latest.json (plus timestamped history).

Designed to be triggered by the cron service once a day so the
user-facing security scan never blocks on a multi-hour malware scan.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

CLAMSCAN_DIR = Path.home() / ".kyber" / "security" / "clamscan"
LATEST_PATH = CLAMSCAN_DIR / "latest.json"
RUNNING_PATH = CLAMSCAN_DIR / "running.json"
HISTORY_LIMIT = 10

# Directories to scan — same as the old inline scan
SCAN_DIRS = [
    Path.home() / "Downloads",
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / ".local" / "bin",
    Path("/tmp"),
    Path("/var/tmp"),
]

# Directories to exclude (standalone clamscan only)
EXCLUDE_DIRS = [
    r"^\.git$", "node_modules", r"\.venv", "__pycache__",
    r"\.cache", r"\.npm", r"\.nvm", "venv", r"\.Trash", "Library",
]


def _find_scanner() -> tuple[str, bool]:
    """Return (binary_path, is_daemon).

    Prefers clamdscan (daemon) over clamscan (standalone).
    Raises FileNotFoundError if neither is available.
    """
    import shutil

    clamdscan = shutil.which("clamdscan")
    if clamdscan:
        # Check if daemon is actually running
        try:
            result = subprocess.run(
                ["clamdscan", "--ping", "1"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return clamdscan, True
        except Exception:
            pass

    clamscan = shutil.which("clamscan")
    if clamscan:
        return clamscan, False

    raise FileNotFoundError("ClamAV not installed (neither clamdscan nor clamscan found)")


def _build_command(scanner: str, is_daemon: bool) -> list[str]:
    """Build the clamscan/clamdscan command."""
    dirs = [str(d) for d in SCAN_DIRS if d.exists()]
    if not dirs:
        dirs = [str(Path.home() / "Downloads")]

    if is_daemon:
        return [scanner, "--multiscan", "--infected", "--no-summary"] + dirs
    else:
        cmd = [
            scanner, "-r", "--infected",
            "--max-filesize=25M", "--max-scansize=100M",
        ]
        for exc in EXCLUDE_DIRS:
            cmd.append(f"--exclude-dir={exc}")
        cmd.extend(dirs)
        return cmd


def run_clamscan() -> dict:
    """Run a ClamAV scan and persist the result.

    Returns the report dict. Safe to call from a thread/subprocess.
    Skips if another scan is already running.
    """
    # Guard against concurrent scans
    existing = get_running_state()
    if existing:
        return {"status": "skipped", "error": "Another scan is already running"}

    CLAMSCAN_DIR.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)

    report: dict = {
        "version": 1,
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "duration_seconds": 0,
        "scanner": None,
        "is_daemon": False,
        "status": "error",  # overwritten on success
        "infected_files": [],
        "scanned_dirs": [],
        "error": None,
    }

    # Write running state so the dashboard can show progress
    _set_running(started_at)

    try:
        scanner, is_daemon = _find_scanner()
    except FileNotFoundError as e:
        report["error"] = str(e)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _save_report(report)
        _clear_running()
        return report

    report["scanner"] = scanner
    report["is_daemon"] = is_daemon
    report["scanned_dirs"] = [str(d) for d in SCAN_DIRS if d.exists()]

    cmd = _build_command(scanner, is_daemon)
    start = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=6 * 3600,  # 6 hour hard cap
        )
        elapsed = time.monotonic() - start
        report["duration_seconds"] = round(elapsed)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()

        # Parse infected files from output
        infected = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("---"):
                continue
            # clamdscan/clamscan format: "/path/to/file: ThreatName FOUND"
            if "FOUND" in line:
                parts = line.rsplit(":", 1)
                if len(parts) == 2:
                    filepath = parts[0].strip()
                    threat = parts[1].replace("FOUND", "").strip()
                    infected.append({"file": filepath, "threat": threat})

        report["infected_files"] = infected

        if result.returncode == 0:
            report["status"] = "clean"
        elif result.returncode == 1:
            report["status"] = "threats_found"
        else:
            report["status"] = "error"
            report["error"] = result.stderr.strip()[:500] if result.stderr else f"Exit code {result.returncode}"

    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        report["duration_seconds"] = round(elapsed)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["status"] = "error"
        report["error"] = "Scan timed out after 6 hours"
    except Exception as e:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["status"] = "error"
        report["error"] = str(e)

    _save_report(report)
    _clear_running()
    return report


def _save_report(report: dict) -> None:
    """Write report to latest.json and a timestamped copy."""
    CLAMSCAN_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2)
    LATEST_PATH.write_text(payload, encoding="utf-8")

    # Timestamped copy
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    history_path = CLAMSCAN_DIR / f"scan_{ts}.json"
    history_path.write_text(payload, encoding="utf-8")

    # Prune old history
    history_files = sorted(CLAMSCAN_DIR.glob("scan_*.json"), reverse=True)
    for old in history_files[HISTORY_LIMIT:]:
        old.unlink(missing_ok=True)


def _set_running(started_at: datetime) -> None:
    """Write a running.json marker so the dashboard knows a scan is in progress."""
    CLAMSCAN_DIR.mkdir(parents=True, exist_ok=True)
    RUNNING_PATH.write_text(json.dumps({
        "started_at": started_at.isoformat(),
        "pid": os.getpid(),
    }), encoding="utf-8")


def _clear_running() -> None:
    """Remove the running marker."""
    RUNNING_PATH.unlink(missing_ok=True)


def get_running_state() -> dict | None:
    """Return running scan info if a scan is currently in progress, else None.

    Also cleans up stale markers from dead processes.
    """
    if not RUNNING_PATH.exists():
        return None
    try:
        data = json.loads(RUNNING_PATH.read_text(encoding="utf-8"))
    except Exception:
        RUNNING_PATH.unlink(missing_ok=True)
        return None

    # Check if the process is still alive
    pid = data.get("pid")
    if pid:
        try:
            os.kill(pid, 0)  # signal 0 = check existence
        except OSError:
            # Process is dead — stale marker
            RUNNING_PATH.unlink(missing_ok=True)
            return None

    return data


def get_latest_report() -> dict | None:
    """Read the most recent clamscan report, or None."""
    if not LATEST_PATH.exists():
        return None
    try:
        return json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_scan_history(limit: int = 10) -> list[dict]:
    """Return recent scan summaries (newest first)."""
    if not CLAMSCAN_DIR.exists():
        return []
    files = sorted(CLAMSCAN_DIR.glob("scan_*.json"), reverse=True)[:limit]
    results = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            results.append({
                "filename": f.name,
                "started_at": data.get("started_at"),
                "finished_at": data.get("finished_at"),
                "duration_seconds": data.get("duration_seconds", 0),
                "status": data.get("status"),
                "threat_count": len(data.get("infected_files", [])),
            })
        except Exception:
            continue
    return results
