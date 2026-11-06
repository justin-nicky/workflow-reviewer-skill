# Hosts

| Host | Skill locations | Transcripts | Explicit-only | Capabilities |
|---|---|---|---|---|
| Claude Code | `~/.claude/skills/`, project `.claude/skills/` (and parents up to the repo root), plugin skills | `~/.claude/projects/<slug>/<session>.jsonl` plus `<session>/subagents/*.jsonl`; moves with `CLAUDE_CONFIG_DIR` | `disable-model-invocation: true` (official) | full. Mapping checked against one real session in phase 0 (versions 2.1.295 to 2.1.309) |
| Codex | `.agents/skills/` (cwd up to repo root), `~/.agents/skills/` (`~/.codex/skills` deprecated), `/etc/codex/skills` | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (default `~/.codex`) | `agents/openai.yaml`: `allow_implicit_invocation: false` | full. Mapping checked against one real rollout in phase 0 (`cli_version` 0.137.0-alpha.4 to 0.142.2); wider coverage comes from drift reports |
| Cursor | `.cursor/skills/`, `~/.cursor/skills/`, `.agents/skills/`, `~/.agents/skills/`, plus `.claude`/`.codex` compatibility folders | `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` (unofficial) | `disable-model-invocation: true`. **Install to `.cursor/skills/`**: a forum report says the field is ignored in `.agents/skills/` | no tokens, timestamps, tool results or ids. Mapping checked against one real transcript in phase 0; no version marker exists, so `tested_versions` is "unknown" |
| Kiro | `.kiro/skills/`, `~/.kiro/skills/` | IDE: internal storage; CLI: SQLite in `~/.kiro/` (schema undocumented) | none documented; the description discourages auto-triggering and the README warns it may trigger | degraded mode only |

Windows transcript paths are **unverified** for all hosts. `CODEX_THREAD_ID` is undocumented and may change; the rollout filename ends with the same id.

## Session finding

1. Session id where the host provides one (Claude Code `${CLAUDE_SESSION_ID}`, Codex `CODEX_THREAD_ID`).
2. Otherwise the newest transcript for the repo. If two or more were modified in the last 10 minutes, the script exits with code 3 and lists them; ask the user.
3. A task spread across files: pass several paths; they merge in time order. Forks and duplicated subagents are counted once.

## Turns and the window

- **A turn** is a real prompt from the human to the main agent. Injected user records are not turns, but their tool results stay attached. `window.non_turn_user_records` counts what was excluded, by reason.
  - **Claude Code:** records flagged `isMeta`, `isCompactSummary` or `isVisibleInTranscriptOnly`, plus records starting with a prefix from the mapping's `not_a_turn_prefixes` (local-command output and caveats, user `!` shell input, task notifications, system reminders, interruption markers). A real slash command counts as a turn. A new injected kind counts as a turn until its flag or prefix is added to the mapping.
  - **Codex:** `event_msg` `user_message` events. The user-role and developer-role response items Codex injects are ignored.
  - **Cursor:** every `user` role record.
- **The default window** runs from the start of the session (or just after an earlier `/workflow-reviewer` invocation, said in `coverage.window_note`) to the reviewer's own invocation. An invocation is a prompt starting with `/workflow-reviewer` or `$workflow-reviewer`, or Claude Code's command markers; a prompt that only mentions the skill is not one. `--from-turn` overrides the start.
- **Subagents:** Claude Code reads `<session>/subagents/*.jsonl` and attaches events to turns by timestamp. Cursor reads `<id>/subagents/*.jsonl` beside the transcript and, having no timestamps, places each subagent right after the call that spawned it, matched by brief. Duplicates across files (forks, a subagent that is also its own session) are counted once.

## Metric definitions

- **Tool time** counts the main agent's calls only, with overlapping intervals merged. Subagent tool time is reported per actor (`tool_s`), never added to the main total. It may include permission prompts.
- **Waiting time** is the gap from the last agent or tool activity to the next real prompt, counted when it exceeds the idle threshold. Injected records are not agent activity.
- **`context_at_invocation`** is the input plus cache tokens of the main agent's chronologically last usage record in the window. Id-less records and subagent calls do not override it.
- **Tokens** are deduplicated by message id (max per field); records without an id are counted and flagged. Codex repeats cumulative token totals, so a repeated total is counted once.

## Ignored record types

Records the mapping deliberately skips are counted per type in `coverage.ignored_types`, so a format change that moves data into an ignored type cannot hide behind full coverage. Claude Code's `api-request` and `api-request-blob` records carry request parameters and bodies, not usage. `cost-state` carries cumulative per-model token totals that are not read (see the README's Known gaps).

## Interruptions

`interruptions` is a plain count of user interruptions in the window, with the turn each falls in.

- **Claude Code:** user records starting with `[Request interrupted by user`.
- **Codex:** `turn_aborted` events with `reason = "interrupted"`. Observed in real rollouts: only `interrupted` (9 of 9). Any other abort reason (the Codex protocol also defines `replaced` and `review_ended`) is **excluded**: it is not a user interruption, and it stays visible under `coverage.ignored_types` as `event_msg/turn_aborted`.
- **Cursor, Kiro:** no known marker; reported as "not measurable".

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 2 | Format drift (output still printed from what parses) |
| 3 | Ambiguous session; list printed |
| 4 | Unsupported input (`.zst`) |
| 5 | Python older than 3.11; nothing was read |
| 6 | No transcript found; the report lists where it looked and shows the working tree (`git status`, staged and unstaged `git diff --stat`) in degraded mode |

## Drift procedure

Transcript formats are internal and change between releases. A mapping fits only if coverage is at least 90% of lines, required fields are present (usage when `has_tokens`, tool names), and unseen record types are listed.

1. The script prints a warning quoting the mapping's `tested_versions` and exits with code 2.
2. Run `--sample 20` for 20 redacted, truncated raw lines.
3. Draft a **full override mapping** in the same TOML shape as `scripts/mappings/*.toml` (dotted paths, `[]` wildcards, `[k=v]` filters, equality matches only; no expressions).
4. Re-run with `--mapping -` and pass the mapping on **stdin**, so nothing is written to disk.
5. Confirm coverage is acceptable. In the report: say drift happened, mark metrics from the drafted mapping as **medium** confidence, include the mapping in a code block, and point to the format-drift issue template.
