<div align="center">
  <img src="kyber_logo.png" alt="Kyber logo" width="380">
  <h1>Kyber</h1>
  <p>A personal AI assistant that actually works.</p>
</div>

Kyber is a lightweight AI bot you can set up in 60 seconds and talk to from Discord, Telegram, WhatsApp, or the command line. It doesn't get stuck. It doesn't take minutes to respond. It handles multiple conversations at once, runs tasks in the background, and stays out of your way.

One install command, pick your provider, and you're chatting. No bloat, no config rabbit holes.

**Why Kyber**

- 💎 Set up in under a minute — one command installs, configures, and runs
- Never locks up — concurrent message handling means the bot keeps responding, even during long tasks
- Background subagents — kick off complex work without blocking the conversation, with live progress
- Works with the providers you already use (OpenRouter, Anthropic, OpenAI, Gemini, DeepSeek, Groq, or any OpenAI-compatible endpoint)
- Split providers — use one model for chat and a different one for background tasks
- Chat where you already are — Discord, Telegram, and WhatsApp out of the box
- Built-in tools — web search, shell commands, GitHub, file I/O, and an extensible skills system
- Runs on anything — your laptop, a VPS, a Raspberry Pi. Optional system service keeps it always on
- Secure by default — API keys stored in a locked-down `.env` file, never in plaintext JSON
- Built-in Security Center — environment scanning, vulnerability detection, and malware scanning via ClamAV
- Secure local dashboard for config and monitoring
- Scheduled tasks and heartbeat for proactive check-ins

---

## Install

**One-liner (recommended):**

```bash
curl -fsSL https://kyber.chat/install.sh | bash
```

This auto-detects your OS, installs Python/uv/pipx if needed, walks you through provider setup, writes your config and secrets, and optionally sets up system services.

**Manual install:**

```bash
pipx install kyber-chat    # recommended
uv tool install kyber-chat  # or with uv
pip install kyber-chat      # or plain pip
```

From source (for development):

```bash
git clone git@github.com:cyph3rasi/kyber.git
cd kyber
pip install -e .
```

Requires Python 3.11+.

---

## Quick start

**1. Initialize**

```bash
kyber onboard
```

This creates:
- `~/.kyber/config.json` — non-sensitive settings
- `~/.kyber/.env` — secrets file with 600 permissions
- `~/.kyber/workspace/` — agent workspace

**2. Add your API key**

Add your key to `~/.kyber/.env`:

```bash
KYBER_PROVIDERS__OPENROUTER__API_KEY=sk-or-v1-xxx
```

Set your provider and model in `~/.kyber/config.json`:

```json
{
  "providers": {
    "openrouter": {
      "chatModel": "anthropic/claude-sonnet-4-20250514"
    }
  },
  "agents": {
    "defaults": {
      "provider": "openrouter"
    }
  }
}
```

API keys never go in `config.json` — they live in `.env` with restricted file permissions.

**3. Chat**

Single message:

```bash
kyber agent -m "Hello from Kyber"
```

Interactive mode:

```bash
kyber agent
```

**4. Start the gateway** (for chat channels)

```bash
kyber gateway
```

---

## Providers

Kyber supports multiple LLM providers through LiteLLM. You can pin a specific provider so it won't fall back to another key when multiple are configured.

Supported providers: `openrouter`, `openai`, `anthropic`, `deepseek`, `gemini`, `groq`, plus any OpenAI-compatible endpoint via custom providers

Each provider supports separate models for chat and background tasks:

```bash
# ~/.kyber/.env
KYBER_PROVIDERS__DEEPSEEK__API_KEY=sk-xxx
```

```json
{
  "agents": {
    "defaults": {
      "provider": "deepseek"
    }
  },
  "providers": {
    "deepseek": {
      "chatModel": "deepseek-chat",
      "taskModel": "deepseek-chat"
    }
  }
}
```

You can also use entirely different providers for chat and tasks:

```json
{
  "agents": {
    "defaults": {
      "chatProvider": "openrouter",
      "taskProvider": "deepseek"
    }
  }
}
```

Custom providers (Ollama, vLLM, etc.):

```json
{
  "agents": {
    "defaults": {
      "provider": "my-local"
    }
  },
  "providers": {
    "custom": [
      {
        "name": "my-local",
        "apiBase": "http://localhost:11434/v1",
        "chatModel": "llama3"
      }
    ]
  }
}
```

The provider handles automatic retries with exponential backoff for transient errors (rate limits, timeouts, malformed responses from upstream APIs).

---

## Dashboard

Kyber includes a secure local web dashboard for viewing and editing configuration.

**Start the dashboard:**

```bash
kyber dashboard
```

The dashboard runs at `http://127.0.0.1:18890` by default. On startup it prints a masked version of your auth token:

```
💎 Kyber dashboard running at http://127.0.0.1:18890
  Token: abc123…wxyz  (run with --show-token to reveal)
  Open:  http://127.0.0.1:18890
```

The dashboard UI has sections for Providers, Agent, Channels, Tools, Gateway, Skills, Cron Jobs, Security Center, Tasks, Debug, and a Raw JSON editor. Each provider card shows an API key field and a model dropdown that fetches available models directly from the provider's API.

When you save changes through the dashboard, secrets (API keys, tokens) are automatically written to `~/.kyber/.env` and non-sensitive settings go to `config.json`. The gateway is restarted automatically on save.

---

## Chat channels

Enable channels in `~/.kyber/config.json`, add tokens to `~/.kyber/.env`, and start the gateway with `kyber gateway`.

**Discord:**

```bash
# ~/.kyber/.env
KYBER_CHANNELS__DISCORD__TOKEN=YOUR_BOT_TOKEN
```

```json
{
  "channels": {
    "discord": {
      "enabled": true,
      "allowFrom": ["YOUR_USER_ID"],
      "requireMentionInGuilds": true,
      "typingIndicator": true
    }
  }
}
```

**Telegram:**

```bash
# ~/.kyber/.env
KYBER_CHANNELS__TELEGRAM__TOKEN=YOUR_BOT_TOKEN
```

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "allowFrom": ["YOUR_USER_ID"]
    }
  }
}
```

**WhatsApp:**

WhatsApp uses a Node.js bridge. First link your device:

```bash
kyber channels login
```

Then enable in config:

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "bridgeUrl": "ws://localhost:3001",
      "allowFrom": ["YOUR_PHONE_NUMBER"]
    }
  }
}
```

Use `allowFrom`, `allowGuilds`, and `allowChannels` to restrict who can interact with the bot.

---

## Background tasks and subagents

Kyber handles long-running tasks without blocking the conversation. Every incoming message is processed concurrently — you can keep chatting while the bot works on something complex.

**How it works:**

- Each message runs in its own async task
- The agent uses a structured intent system to declare actions — it can't claim it started work without actually spawning a task
- For complex tasks, the agent spawns a **subagent** — a lightweight background worker that handles the task independently
- Each spawned task gets a reference code (e.g. `⚡a3f1b2c4`) and a complexity estimate (`simple`, `moderate`, `complex`)
- Workers run with the bot's full personality and have access to all tools
- When approaching the step limit, workers are prompted to wrap up; if exhausted, a forced summary guarantees a result

**Status tracking:**

All background tasks are tracked with:
- Current step number
- Elapsed time
- What tool is currently running
- Recent completed actions

Ask the bot "what's the status?" or "how's that task going?" and it will check the tracker and respond immediately.

---

## Tools

The agent has access to these built-in tools:

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents |
| `write_file` | Write/create files |
| `edit_file` | Edit existing files |
| `list_dir` | List directory contents |
| `exec` | Execute shell commands |
| `web_search` | Search the web (requires Brave API key) |
| `web_fetch` | Fetch and extract web page content |
| `message` | Send messages to chat channels |

**Web search** requires a Brave Search API key in `~/.kyber/.env`:

```bash
KYBER_TOOLS__WEB__SEARCH__API_KEY=YOUR_BRAVE_API_KEY
```

**Shell execution** can be restricted in `config.json`:

```json
{
  "tools": {
    "exec": {
      "timeout": 60,
      "restrictToWorkspace": false
    }
  }
}
```

---

## Skills

Skills extend the agent's capabilities through markdown instruction files. They live in `~/.kyber/workspace/skills/` (or the built-in `kyber/skills/` directory).

| Skill | Description |
|-------|-------------|
| `github` | Interact with GitHub using the `gh` CLI |
| `weather` | Get weather info using wttr.in and Open-Meteo |
| `summarize` | Summarize URLs, files, and YouTube videos |
| `tmux` | Remote-control tmux sessions |
| `skill-creator` | Create new skills |
| `security-scan` | Environment security scanning and malware detection |

Skills are loaded progressively — always-on skills are included in every prompt, while others are loaded on demand when the agent reads their `SKILL.md` file.

---

## Cron

Schedule recurring tasks:

```bash
# Run every 5 minutes
kyber cron add --name "check-news" --message "Check tech news" --every 300

# Run daily at 9am
kyber cron add --name "morning-brief" --message "Give me a morning briefing" --cron "0 9 * * *"

# List jobs
kyber cron list

# Remove a job
kyber cron remove <job-id>
```

Jobs can optionally deliver their output to a chat channel with `--deliver --to <chat_id> --channel <channel>`.

---

## CLI reference

| Command | Description |
|---------|-------------|
| `kyber onboard` | Initialize config, secrets, and workspace |
| `kyber agent -m "..."` | Send a single message |
| `kyber agent` | Interactive chat mode |
| `kyber gateway` | Start the gateway (channels + agent) |
| `kyber dashboard` | Start the web dashboard |
| `kyber dashboard --show-token` | Start dashboard and show auth token |
| `kyber show-dashboard-token` | Print dashboard token without starting it |
| `kyber status` | Show config, provider, and secrets status |
| `kyber migrate-secrets` | Move API keys from config.json to .env |
| `kyber channels status` | Show channel status |
| `kyber channels login` | Link WhatsApp via QR code |
| `kyber skills list` | List all skills |
| `kyber skills add` | Install a skill |
| `kyber skills remove <name>` | Remove a skill |
| `kyber skills search <query>` | Search skills.sh |
| `kyber cron list` | List scheduled jobs |
| `kyber cron add` | Add a scheduled job |
| `kyber cron remove <id>` | Remove a scheduled job |
| `kyber restart gateway` | Restart the gateway service |
| `kyber restart dashboard` | Restart the dashboard service |
| `kyber setup-clamav` | Install and configure ClamAV for malware scanning |
| `kyber --version` | Show version |

---

## Security

Kyber separates secrets from configuration by design:

| File | Contents | Permissions |
|------|----------|-------------|
| `~/.kyber/config.json` | Settings (provider, model, ports, channels) | `600` |
| `~/.kyber/.env` | API keys, bot tokens, dashboard auth token | `600` |

- API keys and tokens are never stored in `config.json`
- The `.env` file is created with `600` permissions (owner read/write only)
- The dashboard handles the split automatically — secrets route to `.env` on save
- Environment variables override `.env` values for production deployments
- The dashboard is local-only by default with bearer token auth
- Use `allowFrom` on all channels to restrict who can interact with the bot
- Shell execution can be sandboxed with `restrictToWorkspace`

**Security Center:** Kyber includes a built-in Security Center that scans your environment for vulnerabilities across 11 categories — network exposure, SSH config, file permissions, secrets, outdated software, suspicious processes, firewall, Docker, git, kyber config, and malware. Run a scan from the dashboard or ask the agent directly.

**Malware scanning:** Install ClamAV for full-system malware detection:

```bash
kyber setup-clamav
```

This installs ClamAV, configures it, and downloads the latest virus signatures. The security scan automatically updates signatures before each scan and performs a full recursive scan of your system.

**Migrating from older versions:** If you have API keys in `config.json`, run `kyber migrate-secrets` to move them to `.env`.

---

## Project layout

```
kyber/
├── agent/          Core agent loop, intent system, subagent manager
│   └── tools/      Built-in tools (filesystem, shell, web, message)
├── bus/            Message bus for routing between channels and agent
├── channels/       Chat integrations (Discord, Telegram, WhatsApp)
├── cli/            Command-line interface
├── config/         Configuration schema, loader, and secrets management
├── cron/           Scheduled task service
├── dashboard/      Secure local web UI
├── heartbeat/      Proactive wake-ups
├── providers/      LLM provider integration (LiteLLM)
├── session/        Conversation state management
├── skills/         Built-in skill definitions
├── skillhub/       Skill installation and management
└── utils/          Helpers
```

---

## License

MIT. See `LICENSE`.
