---
name: security-scan
description: "Perform comprehensive security audits of the user's environment and write structured reports for the Security Center dashboard."
metadata: {"kyber":{"emoji":"🛡️","requires":{"bins":["curl"]},"optional_bins":["clamscan","freshclam"],"always":false}}
---

# Security Scan

You are performing a comprehensive security audit of the user's environment. Your job is to check for vulnerabilities, misconfigurations, and threats, then write a structured JSON report that the Security Center dashboard can display.

## Previous Issues

Before starting, check if there are previous findings to re-verify:

```bash
cat ~/.kyber/security/issues.json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    issues = [i for i in data.get('issues', {}).values() if i.get('status') != 'resolved']
    if issues:
        print(f'{len(issues)} outstanding issues from previous scans:')
        for i in issues:
            print(f\"  [{i.get('severity','?').upper()}] ({i.get('category','?')}) {i.get('title','?')} — seen {i.get('scan_count',1)} time(s)\")
    else:
        print('No outstanding issues from previous scans.')
except: print('No previous scan data found.')
" 2>/dev/null
```

If there are outstanding issues, you MUST specifically re-check each one and include it in your report if it still exists. Consistency across scans is critical — do not skip previously-found issues.

## What to Check

Run these checks systematically. Use `exec` for shell commands and `read_file` / `list_dir` for file inspection.

### 1. Open Ports & Network Exposure

```bash
# macOS
netstat -an | grep LISTEN
# or
lsof -i -P -n | grep LISTEN
```

Look for unexpected listeners, services bound to 0.0.0.0 instead of 127.0.0.1, and high-risk ports (22, 3306, 5432, 6379, 27017, etc.).

### 2. SSH Configuration

```bash
cat ~/.ssh/config 2>/dev/null
ls -la ~/.ssh/
cat /etc/ssh/sshd_config 2>/dev/null | grep -E "^(PermitRootLogin|PasswordAuthentication|Port|AllowUsers)"
```

Check for: password auth enabled, root login allowed, keys without passphrases (check permissions), authorized_keys with unknown entries.

### 3. File Permissions

```bash
# World-writable files in home
find ~ -maxdepth 3 -perm -o+w -type f 2>/dev/null | head -20
# Sensitive files with loose permissions
ls -la ~/.ssh/id_* ~/.gnupg/ ~/.aws/credentials ~/.env 2>/dev/null
# Check for .env files with secrets
find ~ -maxdepth 4 -name ".env" -type f 2>/dev/null
```

Flag any sensitive files (keys, credentials, tokens) that are world-readable.

### 4. Environment Variables & Secrets Exposure

```bash
# Check for leaked secrets in shell config
grep -i "api_key\|secret\|token\|password\|credential" ~/.bashrc ~/.zshrc ~/.bash_profile ~/.zprofile 2>/dev/null
# Check for .env files
find ~/Projects ~/Developer ~/code ~/workspace -maxdepth 3 -name ".env" -type f 2>/dev/null | head -10
```

### 5. Outdated Software & Known Vulnerabilities

```bash
# macOS: check for system updates
softwareupdate -l 2>&1
# Homebrew outdated
brew outdated 2>/dev/null
# npm global packages
npm outdated -g 2>/dev/null
# pip
pip list --outdated 2>/dev/null | head -10
```

### 6. Running Processes & Suspicious Activity

```bash
# Unusual processes
ps aux | grep -v "^root\|^_\|^nobody" | head -30
# Check for crypto miners or suspicious CPU usage
ps aux --sort=-%cpu | head -10
# Crontabs
crontab -l 2>/dev/null
ls -la /etc/cron.* 2>/dev/null
```

### 7. Firewall Status

```bash
# macOS
/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate
# Linux
sudo ufw status 2>/dev/null || sudo iptables -L -n 2>/dev/null | head -20
```

### 8. Docker & Container Security (if present)

```bash
docker ps 2>/dev/null
docker images --format "{{.Repository}}:{{.Tag}} {{.Size}}" 2>/dev/null
# Check for containers running as root
docker ps --format "{{.Names}} {{.Status}}" 2>/dev/null
```

### 9. Git Security

```bash
# Check for secrets in recent commits (workspace)
git log --oneline -20 2>/dev/null
# Check .gitignore exists and covers sensitive files
cat .gitignore 2>/dev/null | grep -E "\.env|credentials|secret|\.pem|\.key"
```

### 10. Kyber-Specific Security

```bash
# Check kyber config and .env permissions
ls -la ~/.kyber/config.json ~/.kyber/.env 2>/dev/null
# Check which providers have API keys configured (checks both .env and config.json)
python3 -c "
import json, os
env_path = os.path.expanduser('~/.kyber/.env')
cfg_path = os.path.expanduser('~/.kyber/config.json')
env_keys = {}
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            if 'API_KEY' in k and v.strip().strip('\"').strip(\"'\"):
                name = k.replace('KYBER_PROVIDERS__','').replace('__API_KEY','').lower()
                env_keys[name] = True
cfg_keys = {}
if os.path.exists(cfg_path):
    c = json.load(open(cfg_path))
    for p, v in c.get('providers', {}).items():
        if isinstance(v, dict) and v.get('api_key', v.get('apiKey', '')):
            cfg_keys[p] = True
all_keys = sorted(set(list(env_keys) + list(cfg_keys)))
if all_keys:
    sources = []
    for p in all_keys:
        src = 'env' if p in env_keys else 'config'
        sources.append(f'{p} ({src})')
    print(f'{len(all_keys)} provider(s) with API keys: {chr(44).join(sources)}')
else:
    print('No provider API keys found in config.json or .env')
" 2>/dev/null
# Dashboard token
cat ~/.kyber/config.json 2>/dev/null | python3 -c "import sys,json; c=json.load(sys.stdin); t=c.get('dashboard',{}).get('auth_token',''); print('Dashboard token length:', len(t))" 2>/dev/null
```

Note: API keys are stored in `~/.kyber/.env`, NOT in `config.json`. Empty `api_key` fields in `config.json` are normal — they were migrated to `.env`. Do NOT flag empty config.json keys as a finding if the key exists in `.env`.

### 11. Malware Scan (Background ClamAV)

ClamAV malware scans now run in the background on a daily schedule via the `kyber-clamscan` cron job. Do NOT run clamscan or clamdscan during the security scan — just read the latest background results.

```bash
cat ~/.kyber/security/clamscan/latest.json 2>/dev/null
```

**If the file does not exist**, the initial background scan may still be running. Add this finding:

```json
{
  "id": "MAL-000",
  "category": "malware",
  "severity": "low",
  "title": "Malware scan in progress — initial background ClamAV scan has not completed yet",
  "description": "ClamAV scans run automatically in the background on a daily schedule. The first scan starts when kyber launches. Results will appear once it completes.",
  "remediation": "Results will be available shortly. If ClamAV is not installed, run `kyber setup-clamav`.",
  "evidence": "~/.kyber/security/clamscan/latest.json not found"
}
```

Set the `malware` category to `"status": "skip"`.

**If the file exists**, read the JSON and check the `status` field:

- `"status": "clean"` → No threats. Set malware category to `"status": "pass"`, `"finding_count": 0`.
- `"status": "threats_found"` → Create a finding for each entry in `infected_files` array with category "malware", severity "critical".
- `"status": "error"` → Note the error. Set malware category to `"status": "warn"`.

Include the `finished_at` timestamp in the report notes so the user knows when the last scan ran.

### 12. Skill Security Scan (Cisco AI Defense)

First, check if the Cisco AI Defense skill-scanner is installed:

```bash
which skill-scanner 2>/dev/null
```

**If `skill-scanner` is NOT found**, skip the skill scan and add this finding to the report:

```json
{
  "id": "SKL-000",
  "category": "skill_scan",
  "severity": "medium",
  "title": "Skill scanner not installed — skill security scanning disabled",
  "description": "The Cisco AI Defense skill-scanner detects prompt injection, data exfiltration, and malicious code patterns in agent skills. Without it, kyber cannot verify that installed skills are safe. It uses static analysis (YAML + YARA patterns), behavioral dataflow analysis, and optional LLM-based semantic analysis.",
  "remediation": "Run `kyber setup-skillscanner` to install the scanner. Once installed, future security scans will automatically check all installed skills for threats.",
  "evidence": "skill-scanner binary not found in PATH"
}
```

Set the `skill_scan` category to `"status": "skip"` with a note that skill-scanner is not installed.

**If `skill-scanner` IS found**, scan all skill directories.

Note: If the user has configured `tools.skill_scanner` in their kyber config (e.g. `use_llm: true` with an API key), the scan task description will include the appropriate env vars and CLI flags automatically. The commands below show the base form — the actual commands in the scan task may include additional flags like `--use-llm`, `--use-virustotal`, `--use-aidefense`, and `--enable-meta` with their corresponding env vars prepended.

#### Step 1: Scan managed skills (~/.kyber/skills)

```bash
skill-scanner scan-all ~/.kyber/skills --recursive --use-behavioral --format json 2>&1
```

#### Step 2: Scan workspace skills (if they exist)

```bash
ls ~/kyber-workspace/skills 2>/dev/null && skill-scanner scan-all ~/kyber-workspace/skills --recursive --use-behavioral --format json 2>&1
```

#### Step 3: Record findings

For each finding from skill-scanner, create a report finding:

```json
{
  "id": "SKL-001",
  "category": "skill_scan",
  "severity": "high",
  "title": "Prompt injection detected in skill: <skill_name>",
  "description": "The skill-scanner detected <threat_type> in <skill_name>. <details about the finding>.",
  "remediation": "Review the skill content and remove or quarantine the affected skill. Run `kyber skills remove <skill_name>` to uninstall it.",
  "evidence": "<skill-scanner output>"
}
```

Map skill-scanner severity levels: CRITICAL → critical, HIGH → high, MEDIUM → medium, LOW/INFO → low.

If no threats are found, set the `skill_scan` category to `"status": "pass"` with `"finding_count": 0`.

## Report Format

After completing all checks, write the report as JSON to `~/.kyber/security/reports/`. The filename must be `report_<ISO_TIMESTAMP>.json`.

Use `exec` to create the directory first:
```bash
mkdir -p ~/.kyber/security/reports
```

Then use `write_file` to write the JSON report:

```json
{
  "version": 1,
  "timestamp": "2026-02-09T10:30:00Z",
  "duration_seconds": 45,
  "summary": {
    "total_findings": 12,
    "critical": 1,
    "high": 3,
    "medium": 5,
    "low": 3,
    "score": 72
  },
  "findings": [
    {
      "id": "NET-001",
      "category": "network",
      "severity": "high",
      "title": "Redis bound to 0.0.0.0",
      "description": "Redis server is listening on all interfaces (port 6379). This exposes the database to the network.",
      "remediation": "Bind Redis to 127.0.0.1 in redis.conf or use firewall rules to restrict access.",
      "evidence": "lsof output: redis-server *:6379 (LISTEN)"
    }
  ],
  "categories": {
    "network": {"checked": true, "finding_count": 2, "status": "warn"},
    "ssh": {"checked": true, "finding_count": 1, "status": "warn"},
    "permissions": {"checked": true, "finding_count": 3, "status": "fail"},
    "secrets": {"checked": true, "finding_count": 2, "status": "warn"},
    "software": {"checked": true, "finding_count": 1, "status": "pass"},
    "processes": {"checked": true, "finding_count": 0, "status": "pass"},
    "firewall": {"checked": true, "finding_count": 1, "status": "warn"},
    "docker": {"checked": false, "finding_count": 0, "status": "skip"},
    "git": {"checked": true, "finding_count": 1, "status": "warn"},
    "kyber": {"checked": true, "finding_count": 1, "status": "pass"},
    "malware": {"checked": true, "finding_count": 0, "status": "pass"},
    "skill_scan": {"checked": true, "finding_count": 0, "status": "pass"}
  },
  "notes": "Your environment is in decent shape overall. The main concern is Redis being exposed on all interfaces — that should be locked down immediately. A few .env files have overly permissive permissions. Consider running `chmod 600` on any files containing API keys or secrets. Your SSH config looks solid, but you have one authorized_keys entry I don't recognize — worth double-checking. Firewall is enabled which is great. No suspicious processes detected."
}
```

### Field Reference

**severity**: `critical` | `high` | `medium` | `low`

**category**: `network` | `ssh` | `permissions` | `secrets` | `software` | `processes` | `firewall` | `docker` | `git` | `kyber` | `malware` | `skill_scan`

**category status**: `pass` (no issues) | `warn` (minor issues) | `fail` (critical/high issues) | `skip` (not applicable)

**score**: 0-100 security score. Start at 100, deduct: critical=-20, high=-10, medium=-5, low=-2. Floor at 0.

**notes**: Free-form natural language. Write as if you're a security consultant briefing the user. Be specific, actionable, and mention the most important things first. Keep it conversational but professional.

### Important

- Always create the directory before writing: `mkdir -p ~/.kyber/security/reports`
- Filename format: `report_YYYY-MM-DDTHH-MM-SS.json` (use dashes in time, not colons)
- Keep only the 20 most recent reports. After writing, clean up old ones:
  ```bash
  ls -t ~/.kyber/security/reports/report_*.json | tail -n +21 | xargs rm -f 2>/dev/null
  ```
- If a check fails or times out, mark the category as `"checked": false` and move on
- Never include actual secret values in the report — only note their presence/absence
- The `evidence` field should contain sanitized command output snippets
