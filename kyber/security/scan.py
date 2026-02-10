"""Deterministic security scan task builder.

Generates the task description used by both the gateway endpoint and the
orchestrator's chat-triggered scan intercept. Having a single source of
truth ensures every scan runs the same commands regardless of entry point.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kyber.security.tracker import get_outstanding_issues


def _build_skill_scanner_env_and_flags() -> tuple[str, str]:
    """Build env var prefix and CLI flags for skill-scanner from kyber config.

    Returns:
        (env_prefix, cli_flags) — shell strings to prepend/append to skill-scanner commands.
    """
    try:
        from kyber.config.loader import load_config
        config = load_config()
    except Exception:
        return "", "--use-behavioral"

    sc = config.tools.skill_scanner
    env_parts: list[str] = []
    flag_parts: list[str] = ["--use-behavioral"] if sc.use_behavioral else []

    # Resolve LLM API key: explicit skill_scanner config > active provider key
    llm_key = sc.llm_api_key
    if not llm_key:
        # Auto-detect from the active provider
        provider_name = config.agents.defaults.provider
        if provider_name:
            provider = getattr(config.providers, provider_name, None)
            if provider and provider.api_key:
                llm_key = provider.api_key

    if llm_key and sc.use_llm:
        env_parts.append(f'SKILL_SCANNER_LLM_API_KEY="{llm_key}"')
        flag_parts.append("--use-llm")
        if sc.llm_model:
            env_parts.append(f'SKILL_SCANNER_LLM_MODEL="{sc.llm_model}"')
        if sc.enable_meta:
            flag_parts.append("--enable-meta")

    if sc.virustotal_api_key and sc.use_virustotal:
        env_parts.append(f'VIRUSTOTAL_API_KEY="{sc.virustotal_api_key}"')
        flag_parts.append("--use-virustotal")

    if sc.ai_defense_api_key and sc.use_aidefense:
        env_parts.append(f'AI_DEFENSE_API_KEY="{sc.ai_defense_api_key}"')
        flag_parts.append("--use-aidefense")

    env_prefix = " ".join(env_parts) + " " if env_parts else ""
    cli_flags = " ".join(flag_parts) if flag_parts else ""
    return env_prefix, cli_flags


def _build_malware_section() -> str:
    """Build the malware section for the scan description from background clamscan results.

    Instead of running ClamAV inline (which can take hours), we read the latest
    results from the background daily clamscan job.
    """
    from kyber.security.clamscan import get_latest_report

    report = get_latest_report()
    if report is None:
        return (
            "\nNo background ClamAV scan results found yet — the initial scan may still be running. "
            "Add a finding with id \"MAL-000\", category \"malware\", severity \"low\", "
            "title \"Malware scan in progress — initial background ClamAV scan has not completed yet\", "
            "remediation \"The daily ClamAV scan runs automatically. Results will be available after the "
            "first scan completes. If ClamAV is not installed, run `kyber setup-clamav`.\"\n"
            "Set the malware category to \"status\": \"skip\".\n"
        )

    status = report.get("status", "error")
    finished = report.get("finished_at", "unknown")
    duration = report.get("duration_seconds", 0)
    infected = report.get("infected_files", [])
    error = report.get("error")
    scanned_dirs = report.get("scanned_dirs", [])

    lines = [
        f"\nRead the background ClamAV scan results (last run: {finished}, took {duration}s).",
        f"Scanned directories: {', '.join(scanned_dirs) if scanned_dirs else 'unknown'}.",
    ]

    if status == "clean":
        lines.append(
            "\nThe last background scan found NO threats. "
            "Set the malware category to \"checked\": true, \"status\": \"pass\", \"finding_count\": 0."
        )
    elif status == "threats_found":
        lines.append(f"\nThe last background scan found {len(infected)} infected file(s):")
        for inf in infected:
            lines.append(f"- File: {inf.get('file', 'unknown')} — Threat: {inf.get('threat', 'unknown')}")
        lines.append(
            "\nFor EACH infected file, create a finding with category \"malware\", severity \"critical\", "
            "id \"MAL-NNN\" (sequential starting at MAL-001). "
            "Set the malware category to \"checked\": true, \"status\": \"fail\"."
        )
    elif status == "error":
        err_msg = error or "Unknown error"
        lines.append(
            f"\nThe last background scan encountered an error: {err_msg}. "
            "Add a finding with id \"MAL-001\", category \"malware\", severity \"low\", "
            "title \"Background ClamAV scan error\", description with the error details. "
            "Set the malware category to \"checked\": true, \"status\": \"warn\"."
        )
    else:
        lines.append(
            f"\nBackground scan status: {status}. Include this in the malware category notes."
        )

    return "\n".join(lines) + "\n"


def build_scan_description() -> tuple[str, str]:
    """Build the scan task description and report path.

    Returns:
        (description, report_path) tuple.
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H-%M-%S")
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    report_path = f"~/.kyber/security/reports/report_{ts}.json"

    # Build skill-scanner env vars and CLI flags from kyber config
    skl_env, skl_flags = _build_skill_scanner_env_and_flags()

    # Build previous-issues section
    outstanding = get_outstanding_issues()
    prev_issues_section = ""
    if outstanding:
        lines = []
        for issue in outstanding:
            sev = issue.get("severity", "medium")
            cat = issue.get("category", "unknown")
            title = issue.get("title", "Unknown issue")
            count = issue.get("scan_count", 1)
            lines.append(f"- [{sev.upper()}] ({cat}) {title} — seen {count} time(s)")
        prev_issues_section = (
            "\n\n## Previously detected issues (for reference only)\n\n"
            "These issues were found in previous scans. They are listed here so you know what\n"
            "was found before. Do NOT create duplicate findings for these — just run your normal\n"
            "checks. If your scan output shows the same issue still exists, include it ONCE in\n"
            "your findings as you normally would. The tracker will automatically mark it as\n"
            "recurring. If the issue no longer exists, simply omit it.\n\n"
            + "\n".join(lines) + "\n"
        )

    # Build dismissed-issues section so the agent knows to skip them
    from kyber.security.tracker import get_dismissed_issues
    dismissed = get_dismissed_issues()
    dismissed_section = ""
    if dismissed:
        d_lines = []
        for issue in dismissed:
            cat = issue.get("category", "unknown")
            title = issue.get("title", "Unknown issue")
            d_lines.append(f"- ({cat}) {title}")
        dismissed_section = (
            "\n\n## Dismissed findings\n\n"
            "The user has dismissed the following findings. Do NOT include them in your report,\n"
            "even if the underlying condition still exists. The user has reviewed these and\n"
            "decided they are not a concern.\n\n"
            + "\n".join(d_lines) + "\n"
        )

    # Build malware section from background clamscan results
    malware_section = _build_malware_section()

    description = f"""Perform a security scan. Follow these steps EXACTLY.
{prev_issues_section}{dismissed_section}
## Step 1: Run all checks in ONE exec call

Run this combined script in a single `exec` tool call:

```
echo "===NETWORK==="
lsof -i -P -n 2>/dev/null | grep LISTEN || netstat -an 2>/dev/null | grep LISTEN
echo "===SSH==="
ls -la ~/.ssh/ 2>/dev/null
cat ~/.ssh/config 2>/dev/null | head -50
echo "===PERMISSIONS==="
ls -la ~/.ssh/id_* ~/.kyber/config.json ~/.kyber/.env 2>/dev/null
find ~ -maxdepth 3 -name ".env" -type f 2>/dev/null | head -10
echo "===SECRETS==="
grep -il "api_key\|secret\|token\|password" ~/.bashrc ~/.zshrc ~/.bash_profile ~/.zprofile 2>/dev/null | while read f; do echo "$f: contains secrets ($(grep -ci 'api_key\|secret\|token\|password' "$f" 2>/dev/null) matches)"; done
echo "===SOFTWARE==="
brew outdated 2>/dev/null | head -10
echo "===PROCESSES==="
ps aux --sort=-%cpu 2>/dev/null | head -10 || ps aux -r 2>/dev/null | head -10
crontab -l 2>/dev/null
echo "===FIREWALL==="
/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null || sudo ufw status 2>/dev/null
echo "===DOCKER==="
docker ps 2>/dev/null || echo "Docker not running"
echo "===GIT==="
git config --global user.email 2>/dev/null || echo "No global git user configured"
echo "===KYBER==="
ls -la ~/.kyber/config.json ~/.kyber/.env 2>/dev/null
python3 -c "
import json, os
env_path = os.path.expanduser('~/.kyber/.env')
cfg_path = os.path.expanduser('~/.kyber/config.json')
env_keys = {{}}
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            if 'API_KEY' in k and v.strip().strip('\"').strip(\"'\"):
                name = k.replace('KYBER_PROVIDERS__','').replace('__API_KEY','').lower()
                env_keys[name] = True
cfg_keys = {{}}
if os.path.exists(cfg_path):
    c = json.load(open(cfg_path))
    for p, v in c.get('providers', {{}}).items():
        if isinstance(v, dict) and v.get('api_key', v.get('apiKey', '')):
            cfg_keys[p] = True
all_keys = sorted(set(list(env_keys) + list(cfg_keys)))
if all_keys:
    sources = []
    for p in all_keys:
        src = 'env' if p in env_keys else 'config'
        sources.append(f'{{p}} ({{src}})')
    print(f'{{len(all_keys)}} provider(s) with API keys: {{', '.join(sources)}}')
else:
    print('No provider API keys found in config.json or .env')
" 2>/dev/null
echo "===SKILL_SCAN==="
which skill-scanner 2>/dev/null && echo "skill-scanner installed" || echo "skill-scanner not installed"
```

## Step 2: Malware scan (background results)

ClamAV malware scans now run in the background on a daily schedule via `kyber-clamscan` cron job.
Do NOT run clamscan or clamdscan yourself — just read the latest results.
{malware_section}

## Step 2b: Skill Security Scan (Cisco AI Defense skill-scanner)

If the output shows "skill-scanner installed", scan all user skill directories for prompt injection, data exfiltration, and malicious code patterns.

Run this in a single `exec` call:

```
{skl_env}skill-scanner scan-all ~/.kyber/skills --recursive {skl_flags} --format json 2>&1
```

Then also scan workspace skills if they exist:

```
{skl_env}skill-scanner scan-all ~/kyber-workspace/skills --recursive {skl_flags} --format json 2>&1
```

For each finding from skill-scanner, create a report finding with:
- id: "SKL-NNN" (sequential)
- category: "skill_scan"
- severity: Map skill-scanner severity (CRITICAL→critical, HIGH→high, MEDIUM→medium, LOW/INFO→low)
- title: The finding's rule name or description
- description: What was detected and in which skill
- remediation: How to fix or remove the malicious skill
- evidence: The relevant skill-scanner output

IMPORTANT: Only include findings that represent actual security threats (prompt injection,
data exfiltration, malicious code, unsafe network calls, etc.). Do NOT include findings about
missing metadata like licenses, descriptions, or manifest fields — those are not security issues.

If skill-scanner finds no issues, set the `skill_scan` category to `"status": "pass"` with `"finding_count": 0`.

If skill-scanner is NOT installed, skip this and add a finding with id "SKL-000", category "skill_scan", severity "medium", title "Skill scanner not installed — skill security scanning disabled", remediation "Run `kyber setup-skillscanner` to enable skill security scanning.".

## Step 3: Write the report

Create the directory and write the JSON report in ONE write_file call:

First: `exec` with `mkdir -p ~/.kyber/security/reports`

Then: `write_file` to `{report_path}` with this EXACT JSON structure:

```json
{{{{
  "version": 1,
  "timestamp": "{iso_ts}",
  "duration_seconds": <elapsed seconds>,
  "summary": {{{{
    "total_findings": <count>,
    "critical": <count>,
    "high": <count>,
    "medium": <count>,
    "low": <count>,
    "score": <0-100, start at 100, deduct: critical=-20, high=-10, medium=-5, low=-2>
  }}}},
  "findings": [
    {{{{
      "id": "<CAT-NNN>",
      "category": "<network|ssh|permissions|secrets|software|processes|firewall|docker|git|kyber|malware|skill_scan>",
      "severity": "<critical|high|medium|low>",
      "title": "<short title>",
      "description": "<what's wrong>",
      "remediation": "<how to fix>",
      "evidence": "<sanitized command output>"
    }}}}
  ],
  "categories": {{{{
    "network": {{{{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "ssh": {{{{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "permissions": {{{{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "secrets": {{{{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "software": {{{{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "processes": {{{{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "firewall": {{{{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "docker": {{{{"checked": <true|false>, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "git": {{{{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "kyber": {{{{"checked": true, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "malware": {{{{"checked": <true if scanned>, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}},
    "skill_scan": {{{{"checked": <true if scanned>, "finding_count": <n>, "status": "<pass|warn|fail|skip>"}}}}
  }}}},
  "notes": "<conversational summary of findings and recommendations>"
}}}}
```

Category status: pass (no issues), warn (medium/low), fail (critical/high), skip (not applicable).
Never include actual secret values — only note presence/absence.
Only report genuine security risks. Do NOT create findings for missing configuration preferences
(like global gitignore), missing metadata (like skill licenses), or informational items that
have no security impact.

## Step 4: Clean up old reports

`exec`: `ls -t ~/.kyber/security/reports/report_*.json | tail -n +21 | xargs rm -f 2>/dev/null`

## Step 5: Deliver results

Your final message should summarize the key findings conversationally. Do NOT end on a tool call."""

    return description, report_path
