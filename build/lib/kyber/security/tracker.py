"""Persistent issue tracker for security scan findings.

Maintains a ledger of all findings across scans so we can detect:
- New issues (first time seen)
- Recurring issues (seen before, still present)
- Resolved issues (previously seen, no longer present)

The tracker lives at ~/.kyber/security/issues.json.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TRACKER_PATH = Path.home() / ".kyber" / "security" / "issues.json"


def _load_tracker() -> dict[str, Any]:
    if _TRACKER_PATH.exists():
        try:
            return json.loads(_TRACKER_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "issues": {}}


def _save_tracker(data: dict[str, Any]) -> None:
    _TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TRACKER_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _fingerprint(finding: dict[str, Any]) -> str:
    """Generate a stable key for a finding based on its essential characteristics.

    We use category + title (lowered) rather than the agent-assigned ID,
    because the agent may assign different IDs across scans for the same issue.
    """
    cat = (finding.get("category") or "").lower().strip()
    title = (finding.get("title") or "").lower().strip()
    return f"{cat}::{title}"


def get_outstanding_issues() -> list[dict[str, Any]]:
    """Return all currently-open (unresolved, non-dismissed) issues for injection into scan prompts."""
    tracker = _load_tracker()
    issues = []
    for _fp, issue in tracker.get("issues", {}).items():
        if issue.get("status") not in ("resolved", "dismissed"):
            issues.append(issue)
    return issues


def dismiss_issue(fingerprint: str) -> bool:
    """Dismiss a finding so it no longer appears in future scan reports.

    Returns True if the issue was found and dismissed, False otherwise.
    """
    tracker = _load_tracker()
    issues = tracker.get("issues", {})
    if fingerprint not in issues:
        return False
    issues[fingerprint]["status"] = "dismissed"
    issues[fingerprint]["dismissed_at"] = datetime.now(timezone.utc).isoformat()
    _save_tracker(tracker)
    return True


def undismiss_issue(fingerprint: str) -> bool:
    """Restore a dismissed finding so it appears in future scans again.

    Returns True if the issue was found and restored, False otherwise.
    """
    tracker = _load_tracker()
    issues = tracker.get("issues", {})
    if fingerprint not in issues or issues[fingerprint].get("status") != "dismissed":
        return False
    issues[fingerprint]["status"] = "recurring"
    issues[fingerprint].pop("dismissed_at", None)
    _save_tracker(tracker)
    return True


def get_dismissed_issues() -> list[dict[str, Any]]:
    """Return all dismissed issues."""
    tracker = _load_tracker()
    return [
        issue for issue in tracker.get("issues", {}).values()
        if issue.get("status") == "dismissed"
    ]


def update_tracker(report: dict[str, Any]) -> dict[str, Any]:
    """Merge a new scan report into the issue tracker.

    Returns the updated tracker data with status annotations.
    Also annotates each finding in the report with a 'tracker_status' field.
    """
    tracker = _load_tracker()
    issues = tracker.get("issues", {})
    now = datetime.now(timezone.utc).isoformat()
    findings = report.get("findings") or []

    # Build set of fingerprints from this scan
    current_fps: set[str] = set()
    for finding in findings:
        fp = _fingerprint(finding)
        current_fps.add(fp)

        if fp in issues:
            existing = issues[fp]
            # Preserve dismissed status — don't resurface dismissed findings
            if existing.get("status") == "dismissed":
                finding["tracker_status"] = "dismissed"
                continue
            existing["status"] = "recurring"
            existing["last_seen"] = now
            existing["scan_count"] = existing.get("scan_count", 1) + 1
            # Update fields from latest scan (severity may change, evidence updates)
            existing["severity"] = finding.get("severity", existing.get("severity"))
            existing["title"] = finding.get("title", existing.get("title"))
            existing["description"] = finding.get("description", existing.get("description"))
            existing["remediation"] = finding.get("remediation", existing.get("remediation"))
            existing["evidence"] = finding.get("evidence", existing.get("evidence"))
            existing["category"] = finding.get("category", existing.get("category"))
            finding["tracker_status"] = "recurring"
        else:
            # New issue
            issues[fp] = {
                "fingerprint": fp,
                "status": "new",
                "first_seen": now,
                "last_seen": now,
                "scan_count": 1,
                "category": finding.get("category"),
                "severity": finding.get("severity"),
                "title": finding.get("title"),
                "description": finding.get("description"),
                "remediation": finding.get("remediation"),
                "evidence": finding.get("evidence"),
            }
            finding["tracker_status"] = "new"

    # Mark issues not in this scan as resolved (but don't touch dismissed ones)
    for fp, issue in issues.items():
        if fp not in current_fps and issue.get("status") not in ("resolved", "dismissed"):
            issue["status"] = "resolved"
            issue["resolved_at"] = now

    tracker["issues"] = issues
    tracker["last_updated"] = now
    tracker["last_processed_report"] = report.get("_report_filename", "")
    _save_tracker(tracker)
    return tracker


def get_tracker_summary() -> dict[str, Any]:
    """Return a summary of tracked issues for the dashboard."""
    tracker = _load_tracker()
    issues = tracker.get("issues", {})
    summary = {"new": 0, "recurring": 0, "resolved": 0, "dismissed": 0, "total": len(issues)}
    for issue in issues.values():
        status = issue.get("status", "new")
        if status in summary:
            summary[status] += 1
    return summary
