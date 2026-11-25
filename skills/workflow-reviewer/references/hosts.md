# Hosts

Every mapping key, with one line each: [mapping-keys.md](mapping-keys.md).

| Host | Skill locations | Transcripts | Explicit-only | Capabilities |
|---|---|---|---|---|
| Claude Code | `~/.claude/skills/`, project `.claude/skills/` (and parents up to the repo root), plugin skills | `~/.claude/projects/<slug>/<session>.jsonl` plus `<session>/subagents/*.jsonl`; moves with `CLAUDE_CONFIG_DIR` | `disable-model-invocation: true` (official) | full. Mapping checked against one real session in phase 0 (versions 2.1.295 to 2.1.309, plus 2.1.321 from a second real session) |
| Codex | `.agents/skills/` (cwd up to repo root), `~/.agents/skills/` (`~/.codex/skills` deprecated), `/etc/codex/skills` | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (default `~/.codex`) | `agents/openai.yaml`: `allow_implicit_invocation: false` | full. Mapping checked against one real rollout in phase 0 (`cli_version` 0.137.0-alpha.4 to 0.142.2, classic variant; 0.162.0-alpha.17.2, desktop variant); wider coverage comes from drift reports |
| Cursor | `.cursor/skills/`, `~/.cursor/skills/`, `.agents/skills/`, `~/.agents/skills/`, plus `.claude`/`.codex` compatibility folders | `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` (unofficial) | `disable-model-invocation: true` (verified in both `.cursor/skills/` and `.agents/skills/`) | no tokens, tool results or ids; no per-record timestamps, but each prompt carries a clock, so wall time is reported coarsely (minute precision; it runs until the next prompt, including the invocation, and when there is none it runs from the first to the last prompt and the last turn's duration is unknown). Interruptions come from `turn_ended` with `user_stopped_generation`; other turn errors are reported apart. Subagents are linked by the markdown link to their id in the parent's text when there is one, otherwise by brief (see Subagents below). The invocation is a prompt whose `<manually_attached_skills>` block names `workflow-reviewer`. No version marker exists, so `tested_versions` is "version not recorded" |
| Devin (desktop app and CLI) | `.cognition/skills/` (project), `~/.claude/skills/`, built-in skills inside the app | SQLite `~/.local/share/devin/cli/sessions.db` (unofficial; most rows sit in the `-wal` file, which the read-only open includes). Select with `--host devin`; cloud Devin keeps no local database, so it is degraded mode only | `disable-model-invocation: true` hides the skill from `skills-loaded`: it shows as used (typed `/name`) but not offered, which is normal | full. Mapping checked against three real sessions on the desktop app 3000.12.43 (database schema 20 in `refinery_schema_history`, reported as `coverage.transcript_version`) |
| Cline (VS Code extension and CLI) | `.clinerules/skills/` | `~/.cline/data/sessions/<id>/` holding `<id>.json` (metadata: cwd, model, started_at, `metadata.usage`, `metadata.tokensIn`, `metadata.sessionHistoryOrigin.version`) and `<id>.messages.json` (`messages[]`: role, content blocks, `ts` in ms, `metrics`, `modelInfo`); child sessions are rows of `~/.cline/data/db/sessions.db` with `is_subagent` and `parent_session_id` | none documented | full for turns, tools, tokens, errors, time and subagents. Skills offered and instruction files are not recorded, so they are not measurable. Mapping made with the adapter flow from one real session (version 4.1.26); env marker `CLINE_ACTIVE` (set in Cline's terminal) |
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
- **The default window** ends just before the latest reviewer invocation and starts just after the one before it, or at the session start when there is none. Reviewer turns (the invocation and its reply) are never in it, and work after the latest invocation is not in it either. When nothing but reviewer turns lie between the last two invocations (a re-run), it is the window the earlier invocation had. `coverage.window_note` says when the start moved or a re-run was detected, and `window.reviewer_turns` lists the invocation turns. An invocation is a prompt in the host's own form: `/workflow-reviewer` or `$workflow-reviewer` starting the prompt (Cline wraps it in `<user_input>`, and also accepts asking "use the workflow-reviewer skill"), Claude Code's command markers, Cursor's `<manually_attached_skills>` block naming it, or Devin's typed `/workflow-reviewer`. A prompt that only mentions the skill is not one. `--from-turn` and `--to-turn` override all of this; a bound left out is the plain one (session start, or just before the latest invocation). `--list-turns` lists every turn, marks reviewer turns with `reviewer: true` and names the default window in `default_window`.
- **Subagents:** Claude Code reads `<session>/subagents/*.jsonl` and attaches events to turns by timestamp. Cursor reads `<id>/subagents/*.jsonl` beside the transcript and, having no timestamps, places each subagent right after the call that spawned it. A spawn is a `CallDynamicTool` call whose `toolName` is `Task` (the mapping's `call_via` unwraps it). When the parent's text links the subagent's id as a markdown link, that links it (`markdown_link`). Otherwise each subagent file is matched to the spawn whose arguments overlap its first message best (`link = brief`; the overlap is the share of words the two texts have in common, 1 when one contains the other, at least 0.3 to count). Briefs often share a template, so the first match is not used: the best score wins, a spawn serves one subagent, and ties go in order. The actor row shows `match_score` and `runner_up_score`. A subagent no spawn explains is not placed in any window: `coverage.unlinked_subagents` counts them, and only a whole-session run lists them as actors. Duplicates across files (forks, a subagent that is also its own session) are counted once.

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
| 4 | Unsupported input: a compressed `.zst` rollout, a document or database file over 200 MB, or a replica asked for from a kind of storage that cannot be copied yet |
| 5 | Python older than 3.9; nothing was read |
| 6 | No transcript found; the report lists where it looked and shows the working tree (`git status` and the staged and unstaged diff summaries) in degraded mode |
| 7 | The findings JSON given to `--report -` is not valid; the errors are listed so they can be fixed and the report re-run |

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
- `json_document`: a session is a folder of JSON documents (Cline keeps `tasks/<id>/api_conversation_history.json` and `ui_messages.json` under VS Code's `globalStorage/saoudrizwan.claude-dev` or `~/.cline/data/sessions`). Keys: `roots` (the folders that hold session folders), `[[container.documents]]` (`file`, a plain file name, and `records`, the path to its record list, `.` for a list at the root), `[container.merge]` `by` (`order`, the declared document order, or a timestamp path to merge by time; a record with no time stays beside its neighbour) and optionally `[container.session]` (`cwd_document` and `cwd_path`: where a session records its working directory). Each record is the JSON object plus `$doc` (the document's name without `.json`) and `$index`, both usable in `match`. Session choice: a folder or document passed as an argument, `--session-id` (the folder name), else the most recent session whose working directory is this one (exit 3 when two are recent). A document over 200 MB is exit 4, a document that is not valid JSON or has no record list is exit 2 naming the file. Only the chosen session's documents are read, and none is ever written.
- `sqlite`: sessions in a database. The mapping names tables and columns, never SQL: `[container.sessions]` (`table`, `id`, `cwd`, `created`, `last_activity`, `head`), `[container.nodes]` (`table`, `session`, `id`, `parent`, `payload`, `created`) and optionally `[container.subagent_heads]` (`table`, `session`, `agent`, `head`). The script builds the SELECTs itself and reads only the chosen session's rows. Nodes are walked from the session's head through their parent ids; nodes off that chain (drafts) are skipped and counted in `coverage.notes` ("off-chain nodes: N"). Each subagent head is its own chain and actor, with link `subagent_heads`. Each record is the decoded payload, and its row columns are reachable as `$row.<column>` (for example `timestamp = ["ts", "$row.created_at"]`). Session choice follows the jsonl rules: working directory, most recent activity, `--session-id`, exit 3 when ambiguous (the list shows the id, the masked directory and times only).
- Safety: every identifier must be a plain identifier (letters, digits, underscore) before anything is opened; the file is opened read-only (`mode=ro`, not `immutable`, so a running app's WAL data is included); names are checked against `sqlite_master` and `table_info`; virtual and FTS tables are refused; values are passed as parameters. A missing table or column is drift (exit 2) and is named.
- `json_document` is reserved for hosts that keep JSON documents; it is not built yet and exits 2.

Every mapping declares a top-level `schema_version = 1`. A mapping without one is treated as 1 and adds a `coverage.notes` entry. A version newer than the script supports exits 2 with "mapping schema N is newer than this script supports (max M)". This also applies to agent-drafted overrides passed with `--mapping`.

**Adding a host:** see [adding-a-host.md](adding-a-host.md).

## Drift procedure

Transcript formats are internal and change between releases. Drift degrades to a warning, never to silently wrong numbers or a needless pause:

- A record type the mapping doesn't know, a cross-check (second count) that disagrees, and coverage under 90% of lines are warnings. The report's Coverage row names the affected number in plain words, with both values ("token totals couldn't be cross-checked: 4,133 vs 3,337").
- The review pauses (exit 2) only when a core number is clearly missing: a presence check fails (no token usage at all on a host that records tokens, results without calls, calls without any results, assistant activity with no turn in the main session on any host) or a field a core metric needs is missing from every record that should carry it (each declared usage field, the turn marker, the call name, the call id). A missing number is null and shows as "not measurable" in the JSON, the markdown and the HTML report, never as 0, with a reason such as "output token counts missing from all 14 usage records".
- A number the host never records is different: it is expected, so it is not a failure. A token field the mapping does not declare (cache writes on Codex and Devin) is null in every breakdown, `tokens.not_recorded` names it, and the reports say "not recorded by <host>" and leave it out of the token bar. Tool time from `tool_time_source = "result_spans"` is null the same way (`time.not_recorded`) when no tool result in the window carries the timing; the time row gets a warning naming the field, never a pause.
- Outside the tested range the script adds `coverage.version_note`: `newer than tested; checks passed` (or `older than ...`) when the checks pass, or the drift warning when they fail.
- A review never drafts, saves or passes an adapter to get past a pause. Adding or fixing an adapter is contributor work done outside a review: [adapter-flow.md](adapter-flow.md). `--sample 20` (redacted, truncated raw lines) and `--mapping -` (a drafted mapping on stdin, nothing written) are tools for that work.

## The window is frozen

A review reads nothing after its window: not the review's own turn, and nothing written while the review runs. Each record belongs to the turn of the event at or before it in its file; records of a later turn are never read, for any number or check. With `--to-turn N` the window ends at the end of turn N. Second counts compare the window only: a running total (a check side with `last`, such as Codex's `token_count` total) is its value at the window's end minus its value before the window's first record. A check whose second side is a whole-session total (`scope = "session"`, Cline's session document) runs only when the window is the whole session.

## Devin

- **Tables** (names live in `mappings/devin.toml`): `sessions` (working directory, model, `main_chain_id` as the head), `message_nodes` (one row per message, linked by `parent_node_id`) and `subagent_heads`. The `message_search*` tables are never touched.
- **Turns:** main-chain user messages with `metadata.is_user_input` true. System messages are ignored except `[Response interrupted by user]`, which is an interruption.
- **Tokens:** `metadata.metrics`, once per `message_id`. `input_tokens` is fresh only (cache excluded): fresh = `input_tokens`, cache read = `cache_read_tokens`. Cache writes are not read: `cache_creation_tokens` is null in every record seen, so the mapping does not declare it. A second way of counting checks `input_tokens + cache_read_tokens == context_input_tokens` per call; the totals appear as `tokens.context_check` and a mismatch is a coverage warning. A reply cut off by an interruption has empty metrics and still counts as one call (`usage_present`).
- **Tools:** the category comes from the tool call's `kind` (read, edit, execute, search, other), kept on the assistant message under `chisel/tool_call_content` and keyed by call id. Only `execute` is a shell command. A `skill` call is kind `other` and has a `command` field that is **not** a shell command. A `run_subagent` call is a subagent spawn; its `rawInput.task` size is the brief.
- **Errors:** the exit code in `chisel/terminal_output` decides. `chisel/tool_result_meta.success` is true even for exit code 2, so it is ignored.
- **Time:** wall time from node `created_at`; tool time from each result's own `chisel/tool_call_timing` start and end.
- **Skills:** offered = `agent-ext/skills-loaded` names with `model_invocable` true. Used = a `skill` tool call (`detected_by` "skill tool call") or a typed `/name` at the start of the prompt's `chisel/acp-content-blocks` text ("slash command"); `skills.loaded[].detections` lists every way with its turn. A skill with `disable-model-invocation: true` is hidden from the model, so it can be used but not offered.
- **Rules:** `agent-ext/rules-loaded` paths (masked) join `skills.instruction_files` as always-on, with the reported byte total in `skills.instruction_bytes_always_on`.
- **Subagents:** each `subagent_heads` chain is an actor (link `subagent_heads`); its model comes from the spawn result's `subagent/model`. `coverage.storage` reports `nodes_shared_with_main` (expected 0), `off_chain_nodes` and `schema_version`.
- **Session choice:** a running skill gets no session id, so the most recent session in the working directory is used (exit 3 when two are recent). `skill_events_spool.jsonl` is not read: it is flushed.

## Mapping keys added for databases and call kinds

- `[[skill_folders]]`: `path`, `scope`, optional `walk` (false: the repo only), `base` and `base_env` (an environment variable replaces the base). A host without them gets no inventory and the note "skill folders unknown for this host".
- `[container.sessions] model`, `[container.version] table/column` (the database's schema number, also the variant tiebreak: `versions = "schema 18 to 25"` or `"schema 20 and newer"`).
- Rules: `call_kind` and `call_raw` (paths with `{id}` for the call id) with top-level `[call_categories]` (kind to category) and `[call_name_categories]`; `results = "."` (the record itself); `result_started`/`result_finished` with `tool_time_source = "result_spans"`; `usage_context`, `usage_present`; `offered_each`; `instruction_paths`/`instruction_bytes`/`instruction_always_on`; `skill_command = { path, regex }`; `spawned_model`. Top level: `skill_tool_label`, `skill_detections = "all"`. A `match` value of `"$present"` means the field exists. Paths starting `$row.` read the node row; `$session.model` reads the session's model.
- Broken chains: "chain broken: N earlier nodes unreachable" and "chain loop at node <masked id>" appear in `coverage.notes`.

## Skill, plugin and hook locations (read by the overlap scan)

The scan reads these files and writes nothing. A host lists its folders in its mapping; a host with none says "hooks unknown for this host" (hooks) or "skill folders unknown for this host" (skills).

| Host | Skill folders (`skill_folders`) | Plugin skill folders (`plugin_skill_folders`) | Hook files (`hook_files`) |
|---|---|---|---|
| Claude Code | `.claude/skills/` (repo and parents), `~/.claude/skills/` (or `$CLAUDE_CONFIG_DIR`) | the desktop app's folders under `~/Library/Application Support/Claude/local-agent-mode-sessions/`: `skills-plugin/*/*/skills/` and `*/*/rpm/*/skills/` (plus one category level below it) | `.claude/settings.json`, `.claude/settings.local.json`, `~/.claude/settings.json` (format `claude-settings`: `hooks.<Event>[].matcher` and `.hooks[].command`) |
| Codex | `.agents/skills/`, `~/.agents/skills/`, `~/.codex/skills/`, `/etc/codex/skills/` | none declared | unknown |
| Cursor | `.cursor/skills/`, `~/.cursor/skills/`, `.agents/skills/`, `~/.agents/skills/`, `.claude/skills/`, `.codex/skills/` | none declared | unknown |
| Devin | `.cognition/skills/`, `~/.claude/skills/` | none declared | unknown |
| Kiro | `.kiro/skills/`, `~/.kiro/skills/` | none declared | unknown |

Plugin skills do not live under `~/.claude` (its `plugins/store` held none on the machine checked): the Claude desktop app keeps them in its own data folder (macOS path shown; Windows and Linux paths are unverified). Those folders also hold skills nobody enabled, so the scan counts only the ones the host offered.

Skill descriptions without a file: Claude Code's `skill_listing` attachment (`content`, one `- name: description` line per skill), Codex's developer-message skill list (the `(file: …)` suffix is removed) and Devin's `skills-loaded` entries. Instruction files with content: Claude Code's `instructions` attachment (`files[]`: path, type, content). Mapping keys: `offered_desc_from`/`offered_desc_item` (regex with name and description groups), `offered_desc_pairs`, `instruction_items`.

Mapping keys: `[[plugin_skill_folders]]` and `[[hook_files]]` take the same fields as `[[skill_folders]]` (`path`, `scope`, `walk`, `base`, `base_env`), plus `glob = true` for a pattern under the base. `hooks_format` names the file shape. Always-on instructions are what the host reported loading this session (for example Devin's `rules-loaded`), plus the repo's `CLAUDE.md`, `AGENTS.md`, `~/.claude/CLAUDE.md` and Cursor rules with `alwaysApply: true`.

## Health table and second counts

`coverage.health` has one row per metric (turns, tool_calls, tokens, errors, verify, interruptions, time, skills, subagents) with a status and the checks that ran, each with its two values. Statuses: `ok` (a second count agrees), `one_way` (only one way to count it: normal, not a failure), `warn`, `broken`, `not_measurable`. Core metrics are turns, tool_calls and tokens (where the host records them). Only a core metric that is clearly missing pauses (exit 2); every other row that needs attention is a warning that continues with exit 0 and appears in the report's Coverage row. The table shows the rows that need attention (warn, broken) plus a summary of all statuses and the version context (transcript version, tested range, `within`/`newer`/`older`/`unknown`); `--health-all` shows every row.

Layer 1 (structure and presence): coverage under the threshold is a warn on every measurable row and never breaks anything. A core row breaks only when the number is clearly missing: a failed presence check or a missing required field (tool name breaks tool_calls). A failed second count is a warn with its two values, and an unseen record type is a warn on the metric its name hints at (`[health_hints]` in a mapping overrides the default words). Presence checks: **tokens**, when the host records tokens and there is assistant activity (assistant records, tool calls or tool results, since results prove a reply happened), or an unseen record type named like usage or replies, but no usage record was found: "no token usage found"; **tool_calls**, when tool results exist but no calls were found ("results without calls"), or the host records results, more than one call exists and there are no results at all ("calls without any results"; one call may still be running); **turns**, assistant activity in a main session with no turn (`turnless_activity` in the mapping: broken for Claude Code and Codex, a warn elsewhere and for a subagent rollout passed alone); and for every core metric, a file where no record at all was recognised. Unseen record types no metric is hinted at are listed in `coverage.warnings` as a warn. Layer 2 (second counts): a mapping's `[[checks]]`, each with `metric`, `first` and `second` sides, and a `relation` (`equal` default, `at_most`, `at_least`), `tolerance_pct` (2 for tokens), `slack_first` (how far the first side may exceed the second while a turn or call is still in flight), `skip_if_second_zero` (the host does not write that record at all) and `severity = "warn"` (a check that may only warn). A side is an engine quantity by name (`turns`, `tool_calls`, `calls_with_id`, `calls_with_result_pct`, `api_calls`, `tokens_output_main`, `context_counted`, `context_reported`, and others) or a count taken from the raw records: `{ match = {...}, count = true | distinct = "path" | sum = "path" | last = "path", actors = "main" }`, or `{ const = N }`.

| Host | Second counts kept | Checked on |
|---|---|---|
| Claude Code | tool calls: at least 95% of `tool_use` ids have a `tool_result` (one call may still be in flight); no `tool_use` id counted twice (warn only) | the conformance session, its golden, and 11 other local sessions |
| Codex | turns vs `turn_context` records; calls vs distinct output call ids; summed per-response output tokens vs the final `token_count` total (2%); no call id counted twice (warn only) | the conformance rollouts, the golden, and 3 other local rollouts |
| Cursor | none | 2 conformance chats, 2 goldens, 23 local chats |
| Devin | turns (`is_user_input`) vs user messages with a `chisel/client-message-id`; `tool_call_content` ids vs tool results; input + cache_read vs `context_input_tokens`; no call id counted twice (warn only) | the 3 real sessions and their goldens |

Dropped because they did not hold on real data: Claude Code API calls vs `api-request` records (equal in the conformance session, but 2,737 vs 740 and 950 vs 753 in others: the records are not written for every call); Claude Code turns vs `turnOrigin: human` records (human-origin records include `!` shell inputs, empty prompts and reminders that the mapping deliberately does not count as turns: 557 vs 461 in one session); Claude Code "every `tool_use` has a `tool_result`" as an exact rule (sessions that ended with calls pending have 3 without a result: replaced by the 95% rule); Cursor turns vs `turn_ended` rows (3 vs 1, 55 vs 3, 11 vs 12 in real chats: not one per prompt). A metric with no second count is `one_way`. Mutation fixtures (`tests/test_health_mutations.py`) check that each host's golden flips the right row when a field is renamed, a record type dropped or added, or a record duplicated.

## User adapters, labels and host detection

- **User adapters** live in `$XDG_CONFIG_HOME/workflow-reviewer/hosts/*.toml` (default `~/.config/...`; `%APPDATA%\workflow-reviewer\hosts` on Windows) and are loaded after the built-ins. An adapter for a new host adds that host (use it with `--host NAME`). An adapter for a built-in host adds variants chosen by shape; it never replaces a built-in variant. A file that cannot be read, or has a newer schema, is skipped with a `coverage.notes` entry.
- **Labels:** `coverage.label` is `verified` for a built-in mapping with `tested_versions`, `experimental` for a built-in mapping marked `stability = "experimental"` (Devin and Cline; reports say "(experimental adapter)"), `unproven` for anything user-made (user adapters, `--mapping` overrides). Reports show it next to the host ("Claude Code session (verified adapter)").
- **`--host auto`** chooses in this order, and says which in `coverage.host_chosen_by`: `--host` (given), `env marker` (a host's `detect = { env = ["NAME"] }`: names only, a variable counts when set and not empty, its value is never read or printed), `data presence` (the host with the most recent session for this working directory, across the jsonl hosts, Devin's database and json_document or glob-declared user hosts), `transcript shape` (explicit paths with `--host auto`), `--mapping` (an override). Otherwise exit 6 with `hosts_checked`. If a marker names a host that has no session here, the run reports that (exit 6) instead of guessing another host. Two recent sessions of one host are exit 3 as before.
- **Verified markers:** Claude Code sets `CLAUDECODE` (checked by listing the names of the variables set in a live session; also set in programs it starts). The other hosts declare none yet.

## The Cline format and the keys it needed

- **Turns:** a user message whose text block is wrapped in `<user_input>` (`turn_regex`). Tool results also arrive as user messages and are not turns.
- **Interruption:** the cancelled reply is not stored, so an interruption shows as two prompts in a row with no assistant message between them: `interrupt_on_repeated_prompt = true` counts it on the first prompt; the second is a new turn.
- **Tools:** `tool_use` blocks, categorised by name (`[call_name_categories]`: `read_files`, `editor`, `run_commands`, `skills`). `call_split = "commands"` makes each command of a `run_commands` call its own call (one id, one result).
- **Errors:** `is_error` is never set; the exit code is read from the result text (`result_exit_code_regex`, "... with code 2").
- **Tokens:** `metrics.inputTokens` includes the cache reads (`usage_input_includes_cache`), so fresh = input minus cache read. Second counts: the sums over messages equal `metadata.usage` (input, output, cache read) and fresh equals `metadata.tokensIn`, all exactly; `tool_use` ids equal `tool_result` ids.
- **json_document additions:** `{id}` in a document's `file` stands for the session folder's name; `name` sets the `$doc` tag; `as_record = true` makes a whole object (the metadata document) one record; `[container.subagents]` (`db`, `table`, `id`, `parent`, `flag`) lists child sessions in a database and reads their folders as subagent actors.
- **`[replica]`** (for `--make-replica`): `keep_paths`, `id_paths`, `command_paths`, `keep_patterns`, as normalised paths like `messages[].content[].name`.
