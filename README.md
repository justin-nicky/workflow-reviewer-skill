# workflow-reviewer

An agent skill that audits **the coding-agent task you just finished**. It finds where tokens and time went without improving quality, and which process slips happened, then returns a report in chat with findings and proposals.

- **Report only.** It never edits code, skills or config, and never writes a report to disk.
- **Quality first.** A saving that weakens an independent review, final verification, a fail-closed rule or a human approval gate is not proposed.
- **Code counts, the model judges.** All numbers come from a deterministic, read-only, standard-library Python script (3.11+). The model reads only its summary and small redacted event windows.
- **Explicit only.** Run it yourself, in the same session, right after the task.

## Quick start (60 seconds)

**Requires Python 3.11+** (the macOS system `python3` is 3.9; install a newer one, for example from python.org or Homebrew). The script exits with code 5 and a clear message on older versions.

1. Install the skill (below).
2. Finish a task in your agent.
3. In the same session, run `/workflow-reviewer`.

Read the report. Ask for any finding or proposal ID in full. Applying a proposal is a separate request; do it in its own commit or branch.

## Install

Copy or symlink `skills/workflow-reviewer/` into your host's skill folder, or use `npx skills add justin-nicky/workflow-reviewer-skill`.

| Host | Folder | Notes |
|---|---|---|
| Claude Code | `~/.claude/skills/` or `.claude/skills/` | `disable-model-invocation: true` keeps it explicit-only |
| Codex | `.agents/skills/` or `~/.agents/skills/` | `agents/openai.yaml` sets `allow_implicit_invocation: false` |
| Cursor | `.cursor/skills/` | Install here, not `.agents/skills/`: a forum report says the explicit-only field is ignored there. Reduced capabilities: no tokens, timestamps or tool results |
| Kiro | `.kiro/skills/` | Degraded mode (working tree and repo records only). Explicit-only is not documented for Kiro, so it may occasionally trigger on its own |

## Example

See [examples/sample-report.md](examples/sample-report.md) (synthetic data).

## Privacy

- The script reads the local transcript and prints to stdout only. It writes nothing and imports no network module.
- Output is redacted by default: API keys and tokens, high-entropy strings, emails, and home paths (shown as `~/…`).
- Previews are truncated. Transcript content is quoted in the report only in short redacted excerpts, and the report is shown in chat, never saved.
- Issue templates ask for mappings and record type names, never transcript content.

## Format drift

Hosts change their internal transcript formats. Each mapping in `skills/workflow-reviewer/scripts/mappings/` records `tested_versions`. If the script reports drift, the agent drafts an override mapping from a redacted sample and re-runs it through stdin. Please share working mappings with the format-drift issue template.

## Output

The audit runs inside the session it audits, so the script keeps its output small. Per-turn and per-actor rows show the top 5 (by tokens, or by tool calls on hosts without tokens) plus an "other" row. `--list-turns` shows the last 30 turns with a header, and `--all` lists every turn. File, failure, skill and record-type lists are truncated with a remainder count. On a real 361-turn session the Markdown summary is about 7k characters. How turns, windows, time and context are computed is in [hosts.md](skills/workflow-reviewer/references/hosts.md).

## Status and verification

Each mapping was checked against one real transcript in phase 0 (nothing from those transcripts is in the repo; tests use synthetic fixtures that reproduce their structure):

| Host | `tested_versions` |
|---|---|
| Claude Code | 2.1.295 to 2.1.309 |
| Codex | 0.137.0-alpha.4 to 0.142.2 (`cli_version`) |
| Cursor | unknown (no version marker in transcripts) |

Wider version coverage comes from drift reports: when a host changes its format, the script warns and the agent drafts an override mapping (see Format drift). CI runs pytest on macOS, Linux and Windows with Python 3.11 and 3.12.

## Known gaps (v1)

- Claude Code subagents are attached to turns by timestamp, not linked by the spawning call.
- Codex subagent linking by parent id is not implemented (no subagent rollouts were seen in phase 0).
- `.zst` rollouts are only reported when passed explicitly; transcript discovery does not look for them.
- Windows transcript paths are unverified for all hosts.
- Cursor transcripts carry no version marker, so its `tested_versions` is "unknown" and drift warnings cannot quote a transcript version.
- Claude Code `cost-state` records carry cumulative per-model token totals (including thinking tokens). They are not used as a cross-check on the per-message usage the script reads.

## Development

```bash
python -m pip install pytest
python -m pytest -q
```

Tests run the CLI black-box on synthetic fixtures only. Real transcripts are never committed. Design notes: `docs/`.

## License

MIT
