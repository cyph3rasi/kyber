---
name: security-scan
description: "Perform comprehensive security audits of the user's environment and write structured reports for the Security Center dashboard."
metadata: {"kyber":{"emoji":"🛡️","requires":{"bins":["curl"]},"optional_bins":["clamscan","freshclam"],"always":false}}
---

# Security Scan

You are performing a comprehensive security audit of the user's environment. Your job is to check for vulnerabilities, misconfigurations, and threats, then write a structured JSON report that the Security Center dashboard can display.

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
# Check kyber config permissions
ls -la ~/.kyber/config.json
# Check if API keys are exposed
cat ~/.kyber/config.json 2>/dev/null | python3 -c "import sys,json; c=json.load(sys.stdin); [print(f'{p}: key set') if v.get('api_key') else print(f'{p}: no key') for p,v in c.get('providers',{}).items() if isinstance(v,dict)]" 2>/dev/null
# Dashboard token
cat ~/.kyber/config.json 2>/dev/null | python3 -c "import sys,json; c=json.load(sys.stdin); t=c.get('dashboard',{}).get('auth_token',''); print('Dashboard token length:', len(t))" 2>/dev/null
```

### 11. Malware Scan (ClamAV)

First, check if ClamAV is installed:

```bash
which clamscan 2>/dev/null
```

**If `clamscan` is NOT found**, skip the malware scan and add this finding to the report:

```json
{
  "id": "MAL-000",
  "category": "malware",
  "severity": "medium",
  "title": "ClamAV not installed — malware scanning disabled",
  "description": "ClamAV is a free, open-source antivirus engine that detects trojans, viruses, malware, and other threats. Without it, kyber cannot scan your system for malicious files. ClamAV is maintained by Cisco Talos and is the industry standard for open-source malware detection on Linux and macOS.",
  "remediation": "Run `kyber setup-clamav` to automatically install and configure ClamAV for your platform. This will install the scanner, configure the signature database, and download the latest threat definitions. Once installed, future security scans will automatically include malware detection.",
  "evidence": "clamscan binary not found in PATH"
}
```

Set the `malware` category to `"status": "skip"` with a note that ClamAV is not installed.

**If `clamscan` IS found**, proceed with the malware scan:

#### Step 1: Update threat database before scanning

Always update signatures before scanning to catch the latest threats:

```bash
# Try without sudo first (works on macOS), fall back to sudo on Linux
freshclam 2>&1 || sudo freshclam 2>&1
```

If freshclam fails, note it in the report but continue with the scan using existing signatures.

#### Step 2: Run full system scan

Run a thorough scan of the entire home directory and common system locations:

```bash
clamscan -r --infected ~/ 2>&1
```

This recursively scans the entire home directory — all user files, downloads, projects, configs, and application data.

The `--infected` flag means only infected files are printed. The exit code tells you the result:
- Exit 0: No threats found
- Exit 1: Threats found (infected files listed in output)
- Exit 2: Errors occurred during scan

#### Step 3: Record findings

For each infected file found, create a finding:

```json
{
  "id": "MAL-001",
  "category": "malware",
  "severity": "critical",
  "title": "Malware detected: <threat_name>",
  "description": "ClamAV detected <threat_name> in <file_path>. This file should be quarantined or removed immediately.",
  "remediation": "Delete or quarantine the infected file. If it's a downloaded file, re-download from a trusted source. Run a full scan with `clamscan -r ~/` to check for additional infections.",
  "evidence": "<clamscan output line>"
}
```

If no threats are found, set the `malware` category to `"status": "pass"` with `"finding_count": 0`.

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
    "malware": {"checked": true, "finding_count": 0, "status": "pass"}
  },
  "notes": "Your environment is in decent shape overall. The main concern is Redis being exposed on all interfaces — that should be locked down immediately. A few .env files have overly permissive permissions. Consider running `chmod 600` on any files containing API keys or secrets. Your SSH config looks solid, but you have one authorized_keys entry I don't recognize — worth double-checking. Firewall is enabled which is great. No suspicious processes detected."
}
```

### Field Reference

**severity**: `critical` | `high` | `medium` | `low`

**category**: `network` | `ssh` | `permissions` | `secrets` | `software` | `processes` | `firewall` | `docker` | `git` | `kyber` | `malware`

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
