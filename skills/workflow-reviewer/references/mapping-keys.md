# Mapping keys: everything the engine accepts

A mapping is a TOML file of declarative data (see `scripts/mappings/*.toml` for working ones). The script refuses a key that is not listed here ("unknown key ... declarative keys only"), so this page is the whole vocabulary: an agent drafting a mapping never needs the script's source. Procedure: [adapter-flow](adapter-flow.md) and [adding-a-host](adding-a-host.md). Storage and variants: [hosts](hosts.md).

## Paths and matches

- **Path:** dotted names into a JSON record (`message.content`), `[]` for every item of a list (`calls[]`), `[k=v]` to keep only the items whose `k` equals `v`. A rule field may also list several alternative paths (`["a.b", "c"]`): the first that exists wins.
- **`match`:** a table of path = value, all equal to select a record (`match = { type = "user" }`). A value may be a list (any of them). The value `"$present"` means "the path exists".
- **Rules** (`[[records]]`) are tried in file order; the first whose `match` fits classifies the record. A record that no rule matches is an unseen type (drift). Role `ignore` lists a record type on purpose, so a new type still shows up as drift.
- **Paths starting `$row.`** read the database row; `$session.model` reads the session's model (sqlite hosts).

## Top level

- `host`: the host's name (lower case, letters, digits, `_`, `-`).
- `schema_version`: the mapping format number. Always `1`; a newer number than the script knows exits 2.
- `stability`: `"experimental"` for a built-in mapping checked on few real sessions; reports then say "(experimental adapter)" instead of verified.
- `tested_versions`: the host versions the mapping was checked on (`"0.1.0 to 0.2.0, 0.3.0 (cli_version)"`). Outside them the report says "newer than tested".
- `type_path`: the path of a record's type (default `type`); a list of paths is joined with `/` (`["type", "payload.type"]`).
- `detect`: `{ env = ["NAME"] }`, environment variable names (never values) that mean "this host is running". Used to choose a host when several have recent sessions.
- `fields`: see "Fields" below.
- `capabilities`: see "Capabilities" below.
- `records`: the list of rules, see "Record rules" below.
- `checks`: second counts for the health table, see "Checks" below.
- `variants`: a list of `[[variants]]`, each with a `name` and its own `[[variants.records]]`; anything it leaves out is inherited. The script picks the one whose shape fits the file.
- `variant`: set by the script from a variant's `name`; do not write it.
- `requires`: in a variant, `{ types = [...], fields = [...] }`: record types and fields that must appear for the variant to be eligible.
- `versions`: in a variant, `"low to high"` or `"low and newer"`; breaks ties between equal shapes.
- `container`: how the host stores records, see "Container" below.
- `skill_folders`: a list of `[[skill_folders]]` (`path`, `scope`, optional `walk`, `base`, `base_env`) where the host keeps skills; read by the overlap scan.
- `plugin_skill_folders`: more skill folders with the same fields plus `glob = true` for a pattern under the base.
- `hook_files`: files that define hooks, same fields as `skill_folders`.
- `hooks_format`: the shape of the hook files (`"claude-settings"`).
- `skill_detections`: `"all"` lists every way a skill was seen (with its turn) in `skills.loaded[].detections`.
- `skill_tool_label`: the label for a skill used through the host's skill tool (default `skill-tool`).
- `call_categories`: a table mapping a call's `kind` (from `call_kind`) to a category (`read`, `edit`, `shell`, `search`, `agent`, `other`).
- `call_name_categories`: a table mapping a tool name (lower case) to a category, for hosts that name their tools plainly.
- `not_a_turn_prefixes`: user records starting with one of these are injected context, not prompts.
- `interruption_prefixes`: user records starting with one of these are interruptions.
- `interrupt_on_repeated_prompt`: `true` counts a prompt repeated unchanged as an interruption.
- `turnless_activity`: accepted for older mappings and ignored: assistant activity with no turn in the main session always means turns are missing (exit 2); in a subagent file it is a warning.
- `token_method`: a sentence saying how tokens are counted; shown in the report.
- `token_method_fallback`: the same, for the fallback counting.
- `null_means_zero`: token fields (`fresh_input`, `cache_read`, `output`, `cache_write`) the host writes as null when it means 0. A null counts as 0 only on a record whose context size adds up with it as 0 (`usage_context`); otherwise, or when the key is missing, it is a gap. Built-in mappings only, each with a test showing it on the goldens; a user adapter may not add or widen it.
- `tool_time_source`: `"result_spans"` takes tool time from each result's own start and end. When no tool result in the window carries them, tool time is null ("not recorded by <host>") with a warning, never 0.
- `health_hints`: a table of metric = list of words; an unseen record type containing a word is tied to that metric (extends the defaults).
- `subagent_marker`: a path whose presence in a file's first record marks the file as a subagent's.
- `subagents`: see "Subagents" below.
- `replica`: a `[replica]` table for `--make-replica`: `keep_paths`, `id_paths`, `command_paths`, `keep_patterns` (what may be kept, which paths hold ids, which hold shell commands, which text patterns are safe).
- `made_by`: written by `--save-adapter` into a user adapter; never write it.
- `made_from_version`: the host version `--save-adapter` saw; written by the script, never by you.

## Fields

Defaults for every rule: where a record keeps these.

- `version`: the host's version string (used for `tested_versions` and the version note).
- `msg_id`: the id of the model response; records with the same id are one API call.
- `model`: the model name.
- `timestamp`: the record's time (ISO text or epoch).
- `event_id`: a unique id of the record (used to drop a record repeated by a host).

## Capabilities

- `has_tokens`: the host records token usage.
- `has_timestamps`: records carry times.
- `has_tool_results`: tool results are recorded (so calls can be matched to results).
- `has_ids`: records carry message and call ids.

A `false` value says the host does not record it: the report says "not measurable" for what depends on it instead of guessing.

## Record rules

Each `[[records]]` has a `match`, a `role` and any of these keys.

- `match`: which records the rule takes (see "Paths and matches").
- `role`: `user`, `assistant`, `meta` (counts tokens, results and so on but is not a message) or `ignore` (listed by type, otherwise skipped).
- `text`: paths to the record's text.
- `msg_id`: the response id path, when it differs from the default.
- `model`: the model path, when it differs from the default.
- `timestamp`: the time path, when it differs from the default.
- `event_id`: accepted in a rule, but the engine reads a record's id from `fields.event_id`.
- `turn`: `false` says a user record is not a prompt.
- `non_turn_reason`: the label shown for a record with `turn = false`.
- `turn_start`: `true` makes the record the start of a turn (hosts with an explicit marker, such as Codex's `task_started`).
- `turn_regex`: a user record counts as a prompt only if its text matches.
- `turn_error`: `true` marks a turn that ended in an error that was not the user stopping it.
- `interrupt`: `true` marks a user interruption.
- `prompt_time`: a regex that reads the prompt's own clock out of its text (hosts with no record timestamps).
- `calls`: the path of the list of tool calls in an assistant record.
- `call_name`: the tool name inside one call.
- `call_input`: the tool arguments inside one call.
- `call_id`: the call's id.
- `call_via`: `{ tool, name, input }`; when the call's tool is `tool` (a generic one), the real tool name is read from its `name` argument and the real arguments from its `input` argument.
- `call_kind`: the path of the call's kind on the message, with `{id}` for the call id; the kind decides the category through `call_categories`.
- `call_raw`: the path of the call's raw input on the message (with `{id}`).
- `call_command_regex`: pulls each shell command out of a script sent to an exec tool; one call per command.
- `call_command_name`: the tool name given to those commands.
- `call_split`: the input field that holds a list of commands; each becomes its own call.
- `results`: the path of the list of tool results in a record (`"."` is the record itself).
- `result_id`: the id of the call a result answers.
- `result_text`: paths to the result's output text.
- `result_exit_code`: the path of the exit code.
- `result_exit_code_regex`: a regex whose first group reads the exit code from the output text.
- `result_error`: a path that is true when the result is an error.
- `result_started`: the path of the host's own start time of a tool call (with `result_finished`, gives tool time when `tool_time_source = "result_spans"`).
- `result_finished`: the path of its end time.
- `usage_input`: the path of the input token count. Declare a usage field only if the host really records it: a declared field that no usage record in the window carries is a missing number (null, "not measurable", exit 2).
- `usage_output`: the path of the output token count.
- `usage_cache_read`: the path of the cache-read token count.
- `usage_cache_write`: the path of the cache-write token count.

A usage field no rule declares is one the host never records: it is null in every breakdown and named in `tokens.not_recorded`; the reports say "not recorded by <host>". That is expected, not a failure.
- `usage_input_includes_cache`: `true` when the input count includes cache reads, so fresh input is input minus cache read.
- `usage_context`: the path of the host's own context-size count (a second count for tokens).
- `usage_present`: a path whose existence makes a record with all-zero usage still count as an API call.
- `usage_dedupe_key`: a path whose value is a running total; a record repeating the same total adds nothing.
- `usage_fallback`: `true` uses this rule's usage only when no rule without the flag produced any.
- `spawned_agent`: the path in a spawn call's result that names the agent it started.
- `spawned_model`: the path of the model a spawned agent ran with.
- `skill_command`: `{ path, regex }`; a typed `/name` at the start of the prompt (the first group is the skill name).
- `offered_skills`: the path of a list of skill names the host offered.
- `offered_each`: a wildcard path with one skill name per match.
- `offered_from`: paths to prose that contains the skill list.
- `offered_block`: a regex whose first group is the part of that prose that holds the list.
- `offered_item`: a regex whose first group is one skill name per line of the block.
- `offered_section`: a regex that recognises a message that holds a skill list; when it matches but no names came out, the report warns "skill list found but not parsed". Anchor it to a heading on its own line so prose that only quotes the heading does not match.
- `offered_desc_from`: paths to the prose that holds descriptions, when it is not `offered_from`.
- `offered_desc_item`: a regex with a name group and a description group.
- `offered_desc_pairs`: `{ list, name, description }`; a list of name and description entries.
- `instruction_paths`: paths of always-on instruction files the host reports loading.
- `instruction_bytes`: the reported byte total of those files.
- `instruction_always_on`: a path that is true when they are always loaded.
- `instruction_items`: `{ list, path, content }`; loaded instruction files whose content the record carries.

## Checks

Each `[[checks]]` counts one metric a second, independent way. Two keys name the sides, the rest set the comparison.

- `name`: what the check says in the table.
- `metric`: `turns`, `tool_calls`, `tokens`, `errors`, `verify`, `interruptions`, `time`, `skills` or `subagents`.
- `first`: the first value: an engine quantity by name (`turns`, `tool_calls`, `calls_with_id`, `results`, `calls_without_result`, `results_without_call`, `calls_with_result_pct`, `subagents`, `interruptions`, `api_calls`, `tokens_output`, `tokens_fresh`, `tokens_cache_read`, `tokens_cache_write`, `tokens_input_incl_cache`, `tokens_all`, `context_counted`, `context_reported`, `duplicate_calls`, with `_main` variants for the main session), or a table taken from raw records (below).
- `second`: the other value, the same way.
- `relation`: `equal` (default), `at_most` or `at_least`, comparing first to second.
- `tolerance_pct`: allowed difference in percent.
- `slack_first`: extra amount the first value may be off by.
- `skip_if_second_zero`: `true` skips the check when the second value is zero (the host does not write that record).
- `scope`: `session` when the second side is a whole-session total (it cannot be split by turn); the check then runs only when the review covers the whole session. The default compares the window only.
- `severity`: `warn` or `broken` when the check fails; the default is `broken` for a core metric (turns, tool calls, tokens) and `warn` otherwise.

A side taken from raw records is a table: `match` (which records), `actors` (`all`, or `main` for the main session only) and one of `count` (the default), `distinct` (distinct values of a path), `sum` (a path's total), `last` (a path's last value), or `const` (a fixed number).

## Subagents

The `[subagents]` table says how a host's subagent files link to the call that started them.

- `meta_file`: a sidecar file next to a subagent's file (`{stem}` is its name without extension).
- `call_id`: the sidecar's path of the spawning call's id.
- `model`: the sidecar's path of the subagent's model.
- `file_prefix`: the prefix a subagent's file name has before the agent id in the parent's result.
- `self_id`: the path of a file's own id in its first record.
- `parent_id`: the path of the parent's id in a subagent's first record.
- `agent_path`: the path of the subagent's own name in its first record.
- `spawn_key`: the spawn call's input field that holds the agent's name.
- `brief_key`: the spawn call's input field that holds the brief text; the brief links a subagent to its call when nothing exact does.
- `forked_from`: the path that says a subagent starts with a copy of its parent's history.
- `history_start`: the path of the record number where the subagent's own records begin.
- `ordinal`: the path of each record's sequence number (to compare with `history_start`).
- `link_regex`: a regex whose group is a subagent id in the parent's text (a link).

## Container

`[container]` says how a host stores records, apart from how it reads them. `kind` is `jsonl` (the default), `sqlite` or `json_document`.

- `kind`: the storage kind.
- `globs`: for a `jsonl` user adapter, patterns of the files that hold sessions (`~` allowed).
- `roots`: for `json_document`, the folders that hold session folders.
- `documents`: a list of `[[container.documents]]`, the JSON files that make up a session.
- `file`: in a document entry, its plain file name (no path).
- `records`: in a document entry, the path of its record list (`.` for a list at the root).
- `name`: in a document entry, the name its records carry (default: the file name).
- `as_record`: in a document entry, `true` treats the whole document as one record.
- `merge`: `[container.merge]`, how documents combine.
- `by`: in `merge`, `order` keeps the declared order; a timestamp path merges by time.
- `session`: `[container.session]`, where a session records its working directory.
- `cwd_document`: in `session`, the document that holds it.
- `cwd_path`: in `session`, the path inside it.
- `sessions`: for `sqlite`, `[container.sessions]` with the columns below.
- `nodes`: for `sqlite`, `[container.nodes]` with the columns below.
- `subagent_heads`: for `sqlite`, optional `[container.subagent_heads]` with the columns below.
- `version`: for `sqlite`, optional `[container.version]` (`table`, `column`): the database's schema number, also a variant tie-break.
- `table`: the table's name, in each of the tables above.
- `column`: the column's name, in `version`.
- `id`: in `sessions` and `nodes`, the id column.
- `cwd`: in `sessions`, the working directory column.
- `created`: in `sessions` and `nodes`, the creation time column.
- `last_activity`: in `sessions`, the last activity column.
- `head`: in `sessions` and `subagent_heads`, the column of the last node of the chain.
- `model`: in `sessions` (optional), the model column.
- `session`: in `nodes` and `subagent_heads`, the column that names the session.
- `parent`: in `nodes`, the parent node's id column.
- `payload`: in `nodes`, the column that holds the record (JSON).
- `agent`: in `subagent_heads`, the subagent's id column.

Every database identifier must be a plain identifier (letters, digits, underscore); the script builds the queries itself.
