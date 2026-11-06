# Hosts

| Host | Skill locations | Transcripts | Explicit-only | Capabilities |
|---|---|---|---|---|
| Claude Code | `~/.claude/skills/`, project `.claude/skills/` (and parents up to the repo root), plugin skills | `~/.claude/projects/<slug>/<session>.jsonl` plus `<session>/subagents/*.jsonl`; moves with `CLAUDE_CONFIG_DIR` | `disable-model-invocation: true` (official) | full |
| Codex | `.agents/skills/` (cwd up to repo root), `~/.agents/skills/` (`~/.codex/skills` deprecated), `/etc/codex/skills` | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (default `~/.codex`) | `agents/openai.yaml`: `allow_implicit_invocation: false` | full (confirm on a real rollout) |
| Cursor | `.cursor/skills/`, `~/.cursor/skills/`, `.agents/skills/`, `~/.agents/skills/`, plus `.claude`/`.codex` compatibility folders | `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` (unofficial) | `disable-model-invocation: true`. **Install to `.cursor/skills/`**: a forum report says the field is ignored in `.agents/skills/` | no tokens, timestamps, tool results or ids |
| Kiro | `.kiro/skills/`, `~/.kiro/skills/` | IDE: internal storage; CLI: SQLite in `~/.kiro/` (schema undocumented) | none documented; the description discourages auto-triggering and the README warns it may trigger | degraded mode only |

Windows transcript paths are **unverified** for all hosts. `CODEX_THREAD_ID` is undocumented and may change; the rollout filename ends with the same id.

## Session finding

1. Session id where the host provides one (Claude Code `${CLAUDE_SESSION_ID}`, Codex `CODEX_THREAD_ID`).
2. Otherwise the newest transcript for the repo. If two or more were modified in the last 10 minutes, the script exits with code 3 and lists them; ask the user.
3. A task spread across files: pass several paths; they merge in time order. Forks and duplicated subagents are counted once.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 2 | Format drift (output still printed from what parses) |
| 3 | Ambiguous session; list printed |
| 4 | Unsupported input (`.zst`) |

## Drift procedure

Transcript formats are internal and change between releases. A mapping fits only if coverage is at least 90% of lines, required fields are present (usage when `has_tokens`, tool names), and unseen record types are listed.

1. The script prints a warning quoting the mapping's `tested_versions` and exits with code 2.
2. Run `--sample 20` for 20 redacted, truncated raw lines.
3. Draft a **full override mapping** in the same TOML shape as `scripts/mappings/*.toml` (dotted paths, `[]` wildcards, `[k=v]` filters, equality matches only; no expressions).
4. Re-run with `--mapping -` and pass the mapping on **stdin**, so nothing is written to disk.
5. Confirm coverage is acceptable. In the report: say drift happened, mark metrics from the drafted mapping as **medium** confidence, include the mapping in a code block, and point to the format-drift issue template.
