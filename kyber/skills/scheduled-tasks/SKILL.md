---
name: scheduled-tasks
description: "Schedule recurring or one-shot tasks using kyber's cron system, and use the heartbeat for periodic monitoring. Covers cron expressions, intervals, one-time runs, delivery to channels, HEARTBEAT.md, and job management."
metadata: {"kyber":{"emoji":"⏰"}}
---

# Scheduled Tasks

kyber has a built-in cron system for scheduling recurring or one-shot tasks. Jobs are stored in `~/.kyber/cron/jobs.json` and persist across restarts.

When a job fires, its `message` is sent to you (the agent) as a prompt. You process it like any other user message, and optionally the response is delivered to a chat channel.

## Creating Jobs

Use the `exec` tool to run `kyber cron add` with the appropriate flags.

### Required flags (always)

| Flag | Description |
|------|-------------|
| `--name "..."` or `-n "..."` | Human-readable job name |
| `--message "..."` or `-m "..."` | The prompt sent to the agent when the job fires |

### Schedule flags (pick exactly one)

| Flag | Description | Example |
|------|-------------|---------|
| `--every N` or `-e N` | Run every N **seconds** | `--every 3600` (hourly) |
| `--cron "expr"` or `-c "expr"` | Standard 5-field cron expression | `--cron "0 9 * * *"` (daily 9am) |
| `--at "ISO"` | One-shot at a specific time (ISO 8601) | `--at "2026-03-01T14:00:00"` |

### Delivery flags (optional)

If you want the agent's response delivered to a chat channel:

| Flag | Description |
|------|-------------|
| `--deliver` or `-d` | Enable delivery |
| `--channel "name"` | Channel: `telegram`, `discord`, or `whatsapp` |
| `--to "id"` | Recipient ID (phone number, user ID, channel ID) |

All three (`--deliver`, `--channel`, `--to`) must be provided together for delivery to work.

## Schedule Types in Detail

### Interval (`--every`)

Runs repeatedly at a fixed interval. The value is in **seconds**.

Common intervals:
- 5 minutes: `--every 300`
- 30 minutes: `--every 1800`
- 1 hour: `--every 3600`
- 6 hours: `--every 21600`
- 24 hours: `--every 86400`

```bash
kyber cron add --name "health check" --message "Check if the API at https://example.com/health is responding and report any issues" --every 1800
```

### Cron expression (`--cron`)

Standard 5-field cron: `minute hour day-of-month month day-of-week`

| Field | Values | Special |
|-------|--------|---------|
| minute | 0-59 | `*` = every, `*/5` = every 5 |
| hour | 0-23 | |
| day of month | 1-31 | |
| month | 1-12 | |
| day of week | 0-6 (0=Sunday) | |

Common patterns:
- Every day at 9am: `"0 9 * * *"`
- Every Monday at 8am: `"0 8 * * 1"`
- Every hour: `"0 * * * *"`
- Every 15 minutes: `"*/15 * * * *"`
- Weekdays at 6pm: `"0 18 * * 1-5"`
- First of every month at noon: `"0 12 1 * *"`

**Important:** Always quote the cron expression to prevent shell interpretation of `*`.

```bash
kyber cron add --name "morning briefing" --message "Give me a summary of today's weather and any reminders from my memory" --cron "0 8 * * *"
```

Cron expressions respect the user's configured timezone (set in Agent settings). If no timezone is configured, system local time is used.

### One-shot (`--at`)

Runs once at a specific time, then auto-disables. Use ISO 8601 format.

```bash
kyber cron add --name "deploy reminder" --message "Remind the user that the v2.0 deployment window opens in 30 minutes" --at "2026-03-15T13:30:00"
```

The timestamp is interpreted in the system's local timezone unless an offset is included (e.g. `2026-03-15T13:30:00-05:00`).

## Delivery Examples

Send a daily digest to Telegram:
```bash
kyber cron add --name "daily digest" --message "Compile a daily digest: weather, calendar reminders from memory, and any pending tasks" --cron "0 8 * * *" --deliver --channel telegram --to "123456789"
```

Send a weekly report to Discord:
```bash
kyber cron add --name "weekly report" --message "Summarize what happened this week based on my memory and daily notes" --cron "0 17 * * 5" --deliver --channel discord --to "987654321"
```

## Managing Jobs

### List jobs
```bash
kyber cron list          # Active jobs only
kyber cron list --all    # Include disabled jobs
```

### Remove a job
```bash
kyber cron remove <job-id>
```
The job ID is the short hex string shown in `kyber cron list`.

### Enable / disable
```bash
kyber cron enable <job-id>
kyber cron enable <job-id> --disable
```

### Manually trigger a job
```bash
kyber cron run <job-id>
kyber cron run <job-id> --force   # Run even if disabled
```

## Writing Good Job Messages

The `--message` is the prompt you (the agent) will receive when the job fires. Write it as if the user is asking you to do something:

- Be specific: `"Check the disk usage on the server and warn if any partition is above 80%"`
- Include context: `"Fetch the latest BTC price and compare it to yesterday's price in memory"`
- Mention delivery if relevant: `"Write a short morning greeting for the user"`

Bad: `"do the thing"`
Good: `"Read ~/project/TODO.md and summarize any items due this week"`

## Tips

- Jobs persist across gateway restarts — they're stored in `~/.kyber/cron/jobs.json`
- One-shot (`--at`) jobs auto-disable after running; they aren't deleted unless you remove them
- The agent processes job messages exactly like user messages, so jobs can spawn background tasks
- If delivery is enabled but the channel isn't connected, the response is lost silently
- List jobs first (`kyber cron list`) before removing to confirm the correct ID

---

# Heartbeat System

Separate from cron, kyber has a **heartbeat** that wakes the agent every 30 minutes to check `HEARTBEAT.md` in the workspace.

## How It Works

1. Every 30 minutes, the heartbeat service reads `{workspace}/HEARTBEAT.md`
2. If the file is empty, missing, or contains only headers/comments, the heartbeat is skipped (no LLM call)
3. If there's actionable content, the agent is prompted to read and act on it
4. After processing, if nothing needed attention, the agent replies `HEARTBEAT_OK`

## Writing HEARTBEAT.md

Write tasks or reminders in `HEARTBEAT.md` as if you're leaving a note for yourself to check later:

```markdown
# Heartbeat Tasks

- Check if the backup job at /var/backups completed successfully
- If it's Monday, remind the user about the weekly standup at 10am
- Monitor disk usage and warn if any partition exceeds 85%
```

The agent reads this file every 30 minutes and acts on whatever it finds. Once a task is done, the agent can edit the file to remove completed items.

## When to Use Heartbeat vs Cron

| Use case | Heartbeat | Cron |
|----------|-----------|------|
| "Check this thing periodically" | ✅ Write it in HEARTBEAT.md | |
| "Do X every day at 9am" | | ✅ `--cron "0 9 * * *"` |
| "Do X once on March 15th" | | ✅ `--at "2026-03-15T14:00:00"` |
| "Do X every 2 hours" | | ✅ `--every 7200` |
| "Keep an eye on this until it's resolved" | ✅ Agent removes it when done | |
| "Send the result to Telegram" | | ✅ Use `--deliver` flags |

**Heartbeat** is best for open-ended monitoring tasks that the agent should keep checking until resolved. **Cron** is best for precise schedules and tasks that need delivery to a channel.

## Heartbeat File Location

The file is at `{workspace}/HEARTBEAT.md` where `{workspace}` is the configured workspace path (default: `~/.kyber/workspace`).

To add a heartbeat task, write or append to this file:
```bash
echo "- Check if https://example.com is responding" >> ~/.kyber/workspace/HEARTBEAT.md
```

To clear all heartbeat tasks:
```bash
echo "# Heartbeat Tasks" > ~/.kyber/workspace/HEARTBEAT.md
```
