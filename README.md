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

Copy or symlink `skills/workflow-reviewer/` into your host's skill folder, or use `npx skills add <owner>/workflow-reviewer`.

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

## Status and verification

- Claude Code mapping: written against the documented record shapes; confirm on real files.
- Codex and Cursor mappings: drafted from known formats and **not yet verified on real transcripts** (phase 0 in `docs/workflow-reviewer-plan.md`).
- Windows transcript paths are unverified. CI runs pytest on macOS, Linux and Windows.

## Known gaps (v1)

- Subagents are read from Claude Code's `<session>/subagents/` folder and attached to turns by timestamp. Linking by spawning call (Claude Code), brief and time (Cursor) and parent id (Codex) is not implemented yet.
- `context_at_invocation` is the last API call's input plus cache tokens inside the window; tool time sums overlapping parallel calls.
- Degraded mode reports the working tree (git status and diff stats) and repo records. `.zst` rollouts are only reported when passed explicitly.

## Development

```bash
python -m pip install pytest
python -m pytest -q
```

Tests run the CLI black-box on synthetic fixtures only. Real transcripts are never committed. Design notes: `docs/`.

## License

MIT
