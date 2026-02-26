---
name: summarize
description: Summarize or extract text/transcripts from URLs, podcasts, and local files (great fallback for “transcribe this YouTube/video”).
homepage: https://summarize.sh
metadata: {"kyber":{"emoji":"🧾","install":[{"id":"brew","kind":"brew","formula":"steipete/tap/summarize","bins":["summarize"],"label":"Install summarize (brew, optional)"}]}}
---

# Summarize

Fast CLI to summarize URLs, local files, and YouTube links.

Out-of-box behavior:
- If `summarize` CLI is available, use it for best quality/speed.
- If it is not installed, this skill is still usable:
  - URL: use `web_fetch` then summarize key points.
  - Local file: use `read_file` then summarize key points.
  - YouTube/video URL: use `web_fetch`; if transcript is unavailable, explain limitation and provide best-effort page/video summary.

## When to use (trigger phrases)

Use this skill immediately when the user asks any of:
- “use summarize.sh”
- “what’s this link/video about?”
- “summarize this URL/article”
- “transcribe this YouTube/video” (best-effort transcript extraction; no `yt-dlp` needed)

## Quick start

```bash
summarize "https://example.com" --model google/gemini-3-flash-preview
summarize "/path/to/file.pdf" --model google/gemini-3-flash-preview
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto
```

## YouTube: summary vs transcript

Best-effort transcript (URLs only):

```bash
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto --extract-only
```

If the user asked for a transcript but it’s huge, return a tight summary first, then ask which section/time range to expand.

## Fallback workflow (no summarize CLI installed)

1. Detect availability:
```bash
command -v summarize >/dev/null 2>&1 && echo yes || echo no
```
2. If not available:
- URL/video URL: call `web_fetch(url=...)`, then summarize the returned text.
- Local file: call `read_file(path=...)`, then summarize the returned text.
- If extraction is thin/blocked, clearly state limitation and offer alternatives (another source URL, specific section, or manual paste).

## Model + keys

Kyber should use the same provider secrets/model as the main agent by default.

- Kyber loads provider keys from `~/.kyber/.env` (`KYBER_*`) and exports compatibility env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, etc.) for external CLIs like `summarize`.
- Prefer the active Kyber model/provider context for `summarize` runs.
- Only ask the user for manual API key setup if Kyber has no configured provider key at all.

## Useful flags

- `--length short|medium|long|xl|xxl|<chars>`
- `--max-output-tokens <count>`
- `--extract-only` (URLs only)
- `--json` (machine readable)
- `--firecrawl auto|off|always` (fallback extraction)
- `--youtube auto` (Apify fallback if `APIFY_API_TOKEN` set)

## Config

Optional config file: `~/.summarize/config.json`

```json
{ "model": "openai/gpt-5.2" }
```

Optional services:
- `FIRECRAWL_API_KEY` for blocked sites
- `APIFY_API_TOKEN` for YouTube fallback
