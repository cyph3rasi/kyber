"""OpenHands runtime directory preflight checks."""

from __future__ import annotations

import getpass
import os
import shlex
from pathlib import Path


def _current_uid() -> int | None:
    if os.name == "nt" or not hasattr(os, "getuid"):
        return None
    return os.getuid()


def _owner_uid(path: Path) -> int | None:
    if os.name == "nt":
        return None
    return path.stat().st_uid


def _current_group_name() -> str:
    if os.name == "nt":
        return getpass.getuser() or "user"
    try:
        import grp

        return grp.getgrgid(os.getgid()).gr_name
    except Exception:
        return getpass.getuser() or "user"


def _ownership_fix_hint(base_dir: Path) -> str:
    user = getpass.getuser() or "user"
    group = _current_group_name()
    quoted = shlex.quote(str(base_dir))
    return (
        f"sudo chown -R {shlex.quote(user)}:{shlex.quote(group)} {quoted} && "
        f"chmod 700 {quoted} {shlex.quote(str(base_dir / 'auth'))}"
    )


def _probe_write(dir_path: Path) -> None:
    probe = dir_path / ".kyber-write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)


def ensure_openhands_runtime_dirs(home_dir: Path | None = None) -> Path:
    """Ensure OpenHands runtime directories exist and are writable.

    OpenHands stores auth and runtime state under ``~/.openhands``. If that
    directory was created by another user (commonly via sudo), every task spawn
    can fail with Permission denied. This preflight raises a clear, actionable
    error before task execution.
    """
    root = (home_dir or Path.home()) / ".openhands"
    auth = root / "auth"

    try:
        root.mkdir(parents=True, exist_ok=True)
        auth.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise RuntimeError(
            f"OpenHands directory is not writable: {root}. "
            f"Fix ownership and permissions, then restart Kyber. "
            f"Suggested fix: {_ownership_fix_hint(root)}"
        ) from exc

    uid = _current_uid()
    if uid is not None:
        try:
            owner = _owner_uid(root)
        except OSError as exc:
            raise RuntimeError(
                f"Unable to inspect OpenHands directory ownership at {root}: {exc}"
            ) from exc
        if owner is not None and owner != uid:
            raise RuntimeError(
                f"OpenHands directory {root} is owned by a different user (uid={owner}). "
                f"Current uid={uid}. Suggested fix: {_ownership_fix_hint(root)}"
            )

    if os.name != "nt":
        try:
            root.chmod(0o700)
            auth.chmod(0o700)
        except PermissionError as exc:
            raise RuntimeError(
                f"OpenHands directory permissions could not be updated for {root}. "
                f"Suggested fix: {_ownership_fix_hint(root)}"
            ) from exc

    try:
        _probe_write(root)
    except PermissionError as exc:
        raise RuntimeError(
            f"OpenHands directory is not writable: {root}. "
            f"Suggested fix: {_ownership_fix_hint(root)}"
        ) from exc

    return root
