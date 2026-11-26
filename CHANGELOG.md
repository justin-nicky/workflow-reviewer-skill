# Changelog

## v0.2.6

- **Claude Code 2.1.331.** Tested and supported. Its new `data-flow-records` are recognised and ignored, so coverage is back to 100%. A warn-level check makes sure the tool calls they point at exist. Two info notes count what they name that the transcript doesn't hold: tools with no matching call, and model responses that are likely the host's side calls (the token totals cover the transcript's calls).
- **A coverage drop is reported once.** The report and the issue link say "only N% of records recognised" once, not once per health row.
- **Rule files loaded mid-session.** A rule file is always-on only when the host loads it at the start of the session. A file that arrives later, for example when Devin adds a folder's `AGENTS.md` after a tool call looks in that folder, is marked as loaded on demand with its turn, and its bytes no longer count toward the always-on total.
- **Cursor issue link.** The title says "cursor (version not recorded)", and the body lists what Cursor never records.

## v0.2.5

- **Report a problem in one click.** When a review pauses or a health check needs attention, the output gives a link to a GitHub issue that is already filled in. It holds only allowlisted diagnostics: versions, exit code, host and adapter kind, health statuses and check names, coverage, OS family and Python version. No paths, prompts, commands, file names, model names or session ids. You read and edit it before submitting; nothing is sent automatically. The HTML report always has the link in its footer.
- **Partial token counts are a lower bound.** When some records carry a usage field and others don't, the total sums the ones that do, the report says "at least", and a warning names how many records lack it. It no longer counts the gaps as 0.
- **Interrupted calls.** A call that recorded no token usage at all is named as interrupted in the warning, and the context cross-check skips it.
- **Devin's nulls.** Devin writes some input and cache-read counts as null. These count as 0 only when the record's context size adds up with them as 0; otherwise they are a gap. Adapters can't extend this rule.

Known issues:

- Devin reports `version_position` as unknown, because its tested range ("Devin desktop app 3000.12.43 (database schema 20)") isn't a version range.

## v0.2.4

- **"Not recorded", never 0.** A number the host never writes is now shown as "not recorded by <host>" instead of 0. Cache writes on Codex and Devin are null in every breakdown, named in `tokens.not_recorded`, and left out of the token bar and its legend. This is expected, so it never pauses a review.
- **Tool time on Devin.** When no tool result in the window carries Devin's own tool timing, tool time is "not recorded" rather than 0, and the report warns, naming the missing field. The exit code is unchanged.

## v0.2.3

- **The review reads only its window.** Nothing after the reviewed work is read: not the review's own turn, and nothing written while it runs. Cross-checks compare the reviewed work only, so a running total that lags behind a live turn no longer looks like a mismatch.
- **Warnings instead of pauses.** A cross-check that disagrees, or a record type the skill doesn't know, is now a warning in the report that names the number and shows both values. A review stops only when a core number is clearly missing.
- **Checks can't be weakened.** `--save-adapter` refuses an adapter for a built-in host that removes or loosens one of its checks.
- **No adapter building during a review.** When a core number is missing, the agent says which, offers degraded mode or stopping, and points to the issues page. Adding an adapter is contributor work, described in `references/adapter-flow.md`.
- **Experimental hosts.** Devin and Cline are labelled experimental; reports say "(experimental adapter)".
- **Codex 0.171 skills updates.** A "Host skills update" message adds its skills to the offered list instead of raising a warning.
- **Missing numbers are never 0.** A core field missing from every record that should carry it is null in every breakdown, shown as "not measurable", and stops the review with exit 2.

## v0.2.2

- **Codex 0.171.** Reads Codex 0.171.0-alpha.13 sessions with the built-in mapping: the new tool-listing record is ignored, the skill link `[$workflow-reviewer](...)` is recognised as the invocation, and a message that only quotes the skills heading no longer triggers "skill list found but not parsed".
- **No false pause.** A record type nobody has seen before now stops a review only when nothing else vouches for the metric it would feed. When the second count of that metric agrees, it is a warning that names the type, and the review goes on.
- **Mapping reference.** `references/mapping-keys.md` lists every key a mapping may use, so an agent drafting an adapter does not need to read the script.

## v0.2.1

- **Python 3.9 and newer.** Runs on Python 3.9+, including the Python built into macOS. Python 3.9 and 3.10 have no `tomllib`, so the skill carries a copy of tomli 2.5.0 (the parser `tomllib` was made from) in `scripts/_vendor/`; Python 3.11 and newer still use `tomllib`.

## v0.2.0

- **Hosts.** Reads sessions from Claude Code, Codex (including Codex Desktop), Cursor, Devin (desktop app and CLI) and Cline. Kiro runs in degraded mode. Each host is a declarative mapping file, so a new host needs data, not code.
- **HTML report.** The review is a single self-contained HTML page (no JavaScript, no network): the bottom line, a token breakdown, up to five ranked actions with cause, cost, evidence and why the change is safe, and a skills audit. A markdown version is available for hosts with no browser.
- **Review window.** The review covers the task you just finished: the work between the previous invocation of the skill and this one, or the original task again on a re-run. `--list-turns` shows the turns and the default window.
- **Skill overlap detection.** Finds skills whose descriptions overlap, and tells apart pairs that were both loaded from pairs that only sit side by side.
- **Health table.** Every number carries a status (`ok`, `one_way`, `warn`, `broken`, `not_measurable`). Only a broken core metric stops a review; a host that does not record something says so instead of guessing.
- **Subagents.** Subagent work is counted and linked to the call that started it; on Cursor, where the link is not recorded, it is matched by the brief and anything unmatched is reported, not guessed.
- **Adapter flow.** For an unsupported host or a changed format, the agent can probe the data, draft a mapping, save it as a user adapter and check it against the health table. It can also print the commands to propose the adapter upstream; it never runs them.
- **Privacy.** Read-only and offline. Keys, tokens, emails and home paths are masked by default.
- **Install.** `npx skills add justin-nicky/workflow-reviewer-skill -g`, or copy `skills/workflow-reviewer/` into your host's skill folder. Python 3.11 or newer.
