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
- Chat where you already are — Discord, Telegram, and WhatsApp out of the box
- Built-in tools — web search, shell commands, GitHub, file I/O, and an extensible skills system
- Runs on anything — your laptop, a VPS, a Raspberry Pi. Optional system service keeps it always on
- Secure local dashboard for config and monitoring
- Scheduled tasks and heartbeat for proactive check-ins

---

## Install

**One-liner (recommended):**

```bash
curl -fsSL https://kyber.chat/install.sh | bash
```

This auto-detects your OS, installs Python/uv/pipx if needed, walks you through provider setup, writes your config, and optionally sets up system services.

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

This creates `~/.kyber/config.json` and the workspace at `~/.kyber/workspace/`.

**2. Add your API key**

Edit `~/.kyber/config.json`:

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx",
      "model": "anthropic/claude-sonnet-4-20250514"
    }
  },
  "agents": {
    "defaults": {
      "provider": "openrouter"
    }
  }
}
```

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

Example — using DeepSeek directly:

```json
{
  "agents": {
    "defaults": {
      "provider": "deepseek"
    }
  },
  "providers": {
    "deepseek": { "apiKey": "sk-xxx", "model": "deepseek-chat" }
  }
}
```

Example — using a local/self-hosted endpoint (Ollama, vLLM, etc.) as a custom provider:

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
        "apiKey": "not-needed",
        "model": "llama3"
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

**Reveal the full token:**

```bash
kyber dashboard --show-token
```

If no token exists yet, one is generated automatically and saved to your config.

You can also find the token in `~/.kyber/config.json` under `dashboard.authToken`.

**Dashboard options:**

| Flag | Description |
|------|-------------|
| `--host` | Bind address (default: `127.0.0.1`) |
| `--port` | Port (default: `18890`) |
| `--show-token` | Print the full auth token on startup |

The dashboard UI has a sidebar with sections for Providers, Agent, Channels, Tools, Gateway, Dashboard settings, and a Raw JSON editor. Each provider card shows an API key field and a model dropdown that fetches available models directly from the provider's API. Custom OpenAI-compatible providers can be added from the Providers page. The Agent tab has a provider dropdown that shows only providers with a key configured. The topbar includes Restart Gateway and Restart Dashboard buttons for quick service restarts. Changes are saved directly to `~/.kyber/config.json` and the gateway is automatically restarted on save.

---

## Chat channels

Enable channels in `~/.kyber/config.json` and start the gateway with `kyber gateway`.

**Discord:**

```json
{
  "channels": {
    "discord": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"],
      "requireMentionInGuilds": true,
      "typingIndicator": true
    }
  }
}
```

**Telegram:**

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
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

Check channel status:

```bash
kyber channels status
```

---

## Background tasks and subagents

Kyber handles long-running tasks without blocking the conversation. Every incoming message is processed concurrently — you can keep chatting while the bot works on something complex.

**How it works:**

- Each message runs in its own async task
- If a task takes longer than 30 seconds, the bot sends an acknowledgment and keeps working in the background
- The user can ask for status updates at any time — the bot has a `task_status` tool that returns live progress instantly
- Subagents can also be spawned explicitly by the LLM for tasks it wants to run in parallel

**Status tracking:**

All long-running tasks (both auto-promoted and explicitly spawned subagents) are tracked with:
- Current step and total steps
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
| `spawn` | Spawn a background subagent |
| `task_status` | Check progress of running tasks |

**Web search** requires a Brave Search API key:

```json
{
  "tools": {
    "web": {
      "search": {
        "apiKey": "YOUR_BRAVE_API_KEY",
        "maxResults": 5
      }
    }
  }
}
```

**Shell execution** can be restricted:

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
| `kyber onboard` | Initialize config and workspace |
| `kyber agent -m "..."` | Send a single message |
| `kyber agent` | Interactive chat mode |
| `kyber gateway` | Start the gateway (channels + agent) |
| `kyber dashboard` | Start the web dashboard |
| `kyber dashboard --show-token` | Start dashboard and show auth token |
| `kyber status` | Show config and provider status |
| `kyber channels status` | Show channel status |
| `kyber channels login` | Link WhatsApp via QR code |
| `kyber cron list` | List scheduled jobs |
| `kyber cron add` | Add a scheduled job |
| `kyber cron remove <id>` | Remove a scheduled job |
| `kyber --version` | Show version |

---

## Project layout

```
kyber/
├── agent/          Core agent loop, context builder, subagent manager
│   └── tools/      Built-in tools (filesystem, shell, web, message, spawn, task_status)
├── bus/            Message bus for routing between channels and agent
├── channels/       Chat integrations (Discord, Telegram, WhatsApp)
├── cli/            Command-line interface
├── config/         Configuration schema and loader
├── cron/           Scheduled task service
├── dashboard/      Secure local web UI
├── heartbeat/      Proactive wake-ups
├── providers/      LLM provider integration (LiteLLM)
├── session/        Conversation state management
├── skills/         Built-in skill definitions
└── utils/          Helpers
```

---

## Configuration reference

All configuration lives in `~/.kyber/config.json`. The full schema:

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.kyber/workspace",
      "provider": "",
      "maxTokens": 8192,
      "temperature": 0.7,
      "maxToolIterations": 20
    }
  },
  "providers": {
    "openrouter": { "apiKey": "", "apiBase": null, "model": "" },
    "anthropic":  { "apiKey": "", "model": "" },
    "openai":     { "apiKey": "", "model": "" },
    "deepseek":   { "apiKey": "", "model": "" },
    "gemini":     { "apiKey": "", "model": "" },
    "groq":       { "apiKey": "", "model": "" },
    "custom": [
      {
        "name": "my-provider",
        "apiBase": "https://your-endpoint.com/v1",
        "apiKey": "",
        "model": ""
      }
    ]
  },
  "channels": {
    "discord":  { "enabled": false, "token": "", "allowFrom": [], "allowGuilds": [], "allowChannels": [], "requireMentionInGuilds": true },
    "telegram": { "enabled": false, "token": "", "allowFrom": [] },
    "whatsapp": { "enabled": false, "bridgeUrl": "ws://localhost:3001", "allowFrom": [] }
  },
  "gateway": { "host": "0.0.0.0", "port": 18790 },
  "dashboard": { "host": "127.0.0.1", "port": 18890, "authToken": "" },
  "tools": {
    "web": { "search": { "apiKey": "", "maxResults": 5 } },
    "exec": { "timeout": 60, "restrictToWorkspace": false }
  }
}
```

---

## Security notes

- The dashboard is local-only by default and protected with a bearer token
- Use `allowFrom`, `allowGuilds`, and `allowChannels` to restrict chat access
- Shell execution can be sandboxed to the workspace with `restrictToWorkspace`
- Keep API keys out of shared logs and rotate them if exposed
- The WhatsApp bridge stores session data locally — treat `~/.kyber/` as sensitive

---

## License

MIT. See `LICENSE`.
