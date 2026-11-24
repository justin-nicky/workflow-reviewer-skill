# Changelog

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
