# kyber Skills

This directory contains built-in skills that extend kyber's capabilities.

## Skill Format (AgentSkills-compatible)

Each skill is a directory containing a `SKILL.md` file with:
- YAML frontmatter (`name`, `description`, optional `metadata`)
- Markdown instructions for the agent

Kyber uses the [AgentSkills](https://opencode.ai/docs/skills/) format, which is also used by OpenClaw. Skills from either ecosystem work in Kyber without modification.

## Skill Locations & Precedence

Skills are loaded from three places (highest priority first):

1. **Workspace skills**: `~/.kyber/workspace/skills/`
2. **Managed/local skills**: `~/.kyber/skills/`
3. **Bundled skills**: shipped with kyber (this directory)

If the same skill name exists in multiple locations, the higher-priority one wins.

## OpenClaw Compatibility

Kyber understands both `"kyber"` and `"openclaw"` metadata namespaces in frontmatter. An OpenClaw skill like:

```yaml
metadata: {"openclaw":{"emoji":"♊️","requires":{"bins":["gemini"]}}}
```

works identically to:

```yaml
metadata: {"kyber":{"emoji":"♊️","requires":{"bins":["gemini"]}}}
```

To use an OpenClaw skill, drop its folder into `~/.kyber/skills/` or your workspace `skills/` directory.

## Available Built-in Skills

| Skill | Description |
|-------|-------------|
| `github` | Interact with GitHub using the `gh` CLI |
| `weather` | Get weather info using wttr.in and Open-Meteo |
| `summarize` | Summarize URLs, files, and YouTube videos |
| `tmux` | Remote-control tmux sessions |
| `skill-creator` | Create new skills |
