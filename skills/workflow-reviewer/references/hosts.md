# Hosts

| Host | Skill locations | Transcripts | Explicit-only | Capabilities |
|---|---|---|---|---|
| Claude Code | `~/.claude/skills/`, project `.claude/skills/` (and parents up to the repo root), plugin skills | `~/.claude/projects/<slug>/<session>.jsonl` plus `<session>/subagents/*.jsonl`; moves with `CLAUDE_CONFIG_DIR` | `disable-model-invocation: true` (official) | full. Mapping checked against one real session in phase 0 (versions 2.1.295 to 2.1.309, plus 2.1.321 from a second real session) |
| Codex | `.agents/skills/` (cwd up to repo root), `~/.agents/skills/` (`~/.codex/skills` deprecated), `/etc/codex/skills` | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (default `~/.codex`) | `agents/openai.yaml`: `allow_implicit_invocation: false` | full. Mapping checked against one real rollout in phase 0 (`cli_version` 0.137.0-alpha.4 to 0.142.2, classic variant; 0.162.0-alpha.17.2, desktop variant); wider coverage comes from drift reports |
| Cursor | `.cursor/skills/`, `~/.cursor/skills/`, `.agents/skills/`, `~/.agents/skills/`, plus `.claude`/`.codex` compatibility folders | `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` (unofficial) | `disable-model-invocation: true` (verified in both `.cursor/skills/` and `.agents/skills/`) | no tokens, tool results or ids; no per-record timestamps, but each prompt carries a clock, so wall time is reported coarsely (minute precision, first to last prompt). Interruptions come from `turn_ended` with `user_stopped_generation`; other turn errors are reported apart. Subagents are linked by the markdown link to their id in the parent's text. No version marker exists, so `tested_versions` is "version not recorded" |
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
| 2 | Format drift (output still printed from what parses), or a mapping that cannot be used: a `schema_version` newer than the script supports, an unsupported `[container]` kind, a hostile identifier, or a database table or column the mapping names that is missing |
| 3 | Ambiguous session; list printed |
| 4 | Unsupported input (`.zst`) |
| 5 | Python older than 3.11; nothing was read |
| 6 | No transcript found; the report lists where it looked and shows the working tree (`git status`, staged and unstaged `git diff --stat`) in degraded mode |

## Mapping variants and alternative paths

A mapping file may hold several `[[variants]]`, each with a `name` and its own `[[variants.records]]` (anything else it omits is inherited from the top level). The script picks the variant whose shape fits the transcript: the best classified ratio among variants whose optional `requires = { types = [...], fields = [...] }` markers appear. An optional `versions = "low to high"` (or open-ended `"low and newer"`) range only breaks ties between equal shapes, so a transcript with no version string still resolves. `coverage.variant` names the choice; with several variants `coverage.variant_choice` shows each score and why one won. A mapped field may also list alternative paths (`["message.id", "id"]`): the first one present in the record wins.

### Codex variants

`codex.toml` has two variants. `classic` is the Codex CLI shape up to 0.142.x. `desktop` is Codex Desktop 0.162.x, whose records all carry an `ordinal` (its `requires` marker). In the desktop shape:

- A turn is one `event_msg/task_started` (rule flag `turn_start = true`). User-role messages starting with `<environment_context>`, `<external_codex_apps_open_page>` or `<skill>` are injected context, counted under `window.non_turn_user_records`.
- Commands are JavaScript sent to the `exec` tool. A rule's `call_command_regex` pulls each `cmd:"..."` out of it (name `exec_command`); an `exec` without a `cmd` stays a plain tool call. Exit codes are read from `exit_code=N` in the output text.
- Tokens come from `token_usage_record`, deduplicated by `response_id`. `token_count` rules marked `usage_fallback = true` count only when a rollout has no `token_usage_record` rows.
- A subagent rollout names its parent in `payload.source.subagent.thread_spawn.parent_thread_id`; matching the parent's `payload.id` gives `link = "parent_thread_id"`. When it also has `forked_from_id`, records before `subagent_history_start_ordinal` are copied parent history and are skipped (reported per actor as `history_from_ordinal` and `history_records_skipped`).
- Skills offered are the names under `### Available skills` in the developer message (`offered_from`, `offered_block`, `offered_item`).

## Storage kinds and schema_version

A mapping says how its host *stores* records (`[container] kind`) apart from how it *reads* them (the `[[records]]` rules).

- `jsonl` (the default; existing mappings need no change): a JSONL file, a session folder, subagent files.
- `sqlite`: sessions in a database. The mapping names tables and columns, never SQL: `[container.sessions]` (`table`, `id`, `cwd`, `created`, `last_activity`, `head`), `[container.nodes]` (`table`, `session`, `id`, `parent`, `payload`, `created`) and optionally `[container.subagent_heads]` (`table`, `session`, `agent`, `head`). The script builds the SELECTs itself and reads only the chosen session's rows. Nodes are walked from the session's head through their parent ids; nodes off that chain (drafts) are skipped and counted in `coverage.notes` ("off-chain nodes: N"). Each subagent head is its own chain and actor, with link `subagent_heads`. Each record is the decoded payload, and its row columns are reachable as `$row.<column>` (for example `timestamp = ["ts", "$row.created_at"]`). Session choice follows the jsonl rules: working directory, most recent activity, `--session-id`, exit 3 when ambiguous (the list shows the id, the masked directory and times only).
- Safety: every identifier must be a plain identifier (letters, digits, underscore) before anything is opened; the file is opened read-only (`mode=ro`, not `immutable`, so a running app's WAL data is included); names are checked against `sqlite_master` and `table_info`; virtual and FTS tables are refused; values are passed as parameters. A missing table or column is drift (exit 2) and is named.
- `json_document` is reserved for hosts that keep JSON documents; it is not built yet and exits 2.

Every mapping declares a top-level `schema_version = 1`. A mapping without one is treated as 1 and adds a `coverage.notes` entry. A version newer than the script supports exits 2 with "mapping schema N is newer than this script supports (max M)". This also applies to agent-drafted overrides passed with `--mapping`.

**Adding a host:** copy the nearest mapping, keep `schema_version = 1`, choose the `[container]` kind, and bump `schema_version` only when the mapping format itself changes in a way old scripts cannot read.

## Drift procedure

Transcript formats are internal and change between releases. A mapping fits only if coverage is at least 90% of lines, required fields are present (usage when `has_tokens`, tool names), and unseen record types are listed.

Outside the tested range the script adds `coverage.version_note`: `newer than tested; checks passed` (or `older than ...`) when the checks pass, or the drift warning when they fail.

1. The script prints a warning quoting the mapping's `tested_versions` and exits with code 2.
2. Run `--sample 20` for 20 redacted, truncated raw lines.
3. Draft a **full override mapping** in the same TOML shape as `scripts/mappings/*.toml` (dotted paths, `[]` wildcards, `[k=v]` filters, equality matches only; no expressions).
4. Re-run with `--mapping -` and pass the mapping on **stdin**, so nothing is written to disk.
5. Confirm coverage is acceptable. The report shows drift only in the Coverage row of the Metrics table, noting that metrics from the drafted mapping are **medium** confidence. Do not print the drafted mapping in the report.
