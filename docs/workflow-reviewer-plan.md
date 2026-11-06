# workflow-reviewer: build plan

An open-source agent skill that audits **the coding-agent task you just finished**. It finds where tokens and time went without improving quality, and which process slips happened. It returns a report in chat, with findings and proposals. **The audit reports only:** it never edits code, skills or config, and it never writes the report to disk.

Status: agreed after brainstorming and two grilling rounds (2026-11-06). Build starts after phase 0 (confirm formats on real transcripts).

---

## 1. Principles

1. **Quality comes first.** A saving that weakens one of the following is not a saving:
   - an independent review;
   - final verification;
   - a fail-closed rule;
   - a human approval gate.

   Every proposal names the mechanism it touches and explains why that mechanism still works.
2. **Evidence over description.** Findings and skill verdicts come from what the transcript shows happened, never from what a skill says it does, and never from the agent's own recollection.
3. **Code counts, the LLM judges.** All numbers come from a deterministic script. The LLM reads only the script's summary and small, redacted event windows. It never reads a raw transcript.
4. **Never invent numbers.** Anything not measured is labelled "not measurable on <host>", or shown as an estimate with a confidence level.
5. **The auditor must be cheap.** It runs inside the session it audits, so every step of its own pays to re-read that context. Target: an audit finishes in about 15 tool calls or fewer, and the script's summary is about 2k tokens or fewer.
6. **Untrusted data.** Transcript, repo and web content are data, never instructions to the reviewer.

## 2. Decisions

| # | Decision | Choice |
|---|---|---|
| D1 | Audit scope | The task just completed: every turn, tool call and subagent from the task's first prompt to the reviewer's invocation |
| D2 | Where it runs | **In the same session only**, right after the task. No fresh-session mode |
| D3 | Invocation | **Explicit only** on every host that supports it (§12). Short, specific description that also discourages auto-triggering where a host can't enforce it |
| D4 | Report output | **Shown in chat only; never written to disk.** Nothing is saved anywhere, including drafted mappings |
| D5 | Cost units | **Tokens only.** No pricing, no dollars, no price config |
| D6 | Skill verdicts | Evidence-based: Keep / Improve / Merge or cut / Gap / No verdict |
| D7 | Opt-in research | One question at the end, only if a Gap was found. Covers **skills plus host features, hooks and settings** from official sources. Never invents anything; "nothing suitable found" is a valid answer |
| D8 | Host support | Declarative mapping per format, plus a small shared code layer. An LLM-drafted mapping is the fallback for unknown formats **and** for format drift on known hosts |
| D9 | Hosts in v1 | Claude Code and Codex mappings in full; Cursor mapping with reduced capabilities; Kiro in degraded mode (records only) |
| D10 | No usable transcript | Degraded mode: git history, diff and repo records only. **The agent's own recollection is never evidence.** Confidence capped at medium; coverage stated up front |
| D11 | Recent-session comparison | Not in v1 |
| D12 | Skills inventoried | Only the locations the task's host loads, deduplicated by name and content hash |
| D13 | Python | 3.11+, standard library only (`tomllib` for TOML); `pytest` for tests only |
| D14 | Operating systems | macOS, Linux and Windows, tested in a CI matrix |
| D15 | Model guidance | Run on a model at least as strong as the one that did the task; the report records both |
| D16 | Name and layout | `workflow-reviewer`, in `skills/workflow-reviewer/` (works with `npx skills`, verified) |
| D17 | Audience | Solo developers and teams |
| D18 | Deliverable | Full repo; `evals/` arrives in phase 2 |
| D19 | Near-full context | Nothing special. The audit runs as normal; context size is reported as a metric |
| D20 | Report length | At most 5 findings and 3 proposals in full; everything else one line each; about 80 lines in total; ends with "ask for any ID in full" |
| D21 | After the report | The skill's scope ends at the report. A later "apply P1" is an ordinary request outside the skill; the skill suggests doing it in a separate commit or branch |
| D22 | Growing the checklist | The report marks **candidate new patterns** with evidence. A pattern is merged into `checklist.md` once two independent reports show it (via the GitHub issue template) |
| D23 | Keeping mappings current | Each mapping records `tested_versions`; drift warnings quote them; a GitHub issue template collects drafted mappings and record types, never transcript content |
| D24 | Compressed rollouts | Not supported in v1. Same-session audits always read a live, uncompressed file; a `.zst` file is named and reported as unsupported |

**Cut on purpose:**
- analysis of human intervention vs approval gates;
- checking whether past proposals saved anything;
- the auditor reporting its own cost;
- saved reports;
- pricing;
- comparison with other sessions;
- a fresh-session mode;
- self-reported evidence;
- a lite mode when the context is near full;
- `.zst` support.

## 3. Task window

```
session.jsonl
 ├─ turn 1  "add rate limiting to /login"   ◄── task start
 │    ├─ tool calls · subagents (planner, reviewer)
 ├─ turn 2  "use Redis, not in-memory"
 │    └─ tool calls · subagent (implementer)
 ├─ turn 3  "tests fail on CI, fix"
 └─ turn 4  /workflow-reviewer               ◄── window ends (self-excluded)
```

**Finding the current session:**
1. **Session id, where the host provides one:**
   - Claude Code: `SKILL.md` passes `${CLAUDE_SESSION_ID}` (documented substitution) as `--session-id`.
   - Codex: the script reads the `CODEX_THREAD_ID` env var. It's in the source code, not the docs, so it's labelled "undocumented, may change". The rollout filename ends with the same id.
2. **Fallback** (Cursor, or no id): the newest transcript for this repo. If two or more were modified in the last 10 minutes, the skill lists them (time and a redacted first-prompt preview) and asks the user to choose.

**Window:**
- **Default:** the whole session up to the reviewer's own invocation.
- **Earlier unrelated work:** `--list-turns` prints the user turns (index, time, an 80-character redacted preview), and the agent picks the start with `--from-turn`.
- **A task spread across files:** several PATHs are merged in time order. Per the official docs, a Claude Code resume keeps the same transcript, and only forks get a new file.

**Lagging writes:** hosts write transcripts as the session runs, but the file can lag.
- A truncated final line is ignored.
- The script reports the timestamp of its last record, with "the latest turn may be incomplete" when that's close to now.
- If the reviewer's invocation isn't in the file yet, the window runs to the end of the file.
- No retries, no waiting.

**Subagents:**
- counted if spawned inside the window;
- Claude Code: linked by the spawning call;
- Cursor: matched by brief and time, each counted once;
- Codex: linked by parent id if recorded, otherwise by time (to verify in phase 0).

**Breakdown:** totals, per actor (main agent and each subagent) and per user turn.

## 4. Script architecture

```
transcript ─► detect format ─► mapping ─► generic parser ─► shared code ─► analysis ─► redaction ─► stdout
                  │                                          (dedupe, subagents,
                  └─ drift? ─► ⚠ warning ─► --sample ─► agent   time split, lag)
                                drafts mapping ─► re-run with --mapping - (stdin)
```

### 4.1 Mappings (data, not code)
- One TOML file per format: `scripts/mappings/{claude_code,cursor,codex}.toml`.
- Syntax: dotted paths, array wildcards (`message.content[].type`) and equality matches only. No `eval` and no expressions, because a drafted mapping is untrusted input.
- Each mapping declares:
  - **capabilities**, e.g. `has_tokens`, `has_timestamps`, `has_tool_results`, `has_ids`. The report turns missing capabilities into "not measurable" lines.
  - **`tested_versions`**, which drift warnings quote.

### 4.2 Drift detection (known and unknown formats)
A mapping is considered to fit only if three checks pass:
1. **Coverage:** classified lines are at least 90% of all lines (`--drift-threshold`).
2. **Required fields:** for example, tool calls have names, and `usage` is present when the mapping declares `has_tokens`.
3. **Record types:** any unseen record types are listed by name.

Claude Code's docs call its transcript format internal and say it changes between versions, so this path is expected to run.

On a mismatch, the script:
- prints the warning, e.g. `⚠ claude_code mapping (tested up to 2.1.x): 62% coverage; usage missing; new types: X, Y; transcript version 2.3.0`;
- **continues** with the parts that still parse;
- exits with a distinct code.

The skill then has the agent:
1. run `--sample 20` to get 20 redacted, truncated lines;
2. draft an override mapping;
3. re-run with `--mapping -`, passing the mapping on **stdin** so nothing is written to disk;
4. confirm the coverage is now acceptable.

The report:
- says drift happened;
- marks metrics from the drafted mapping as medium confidence;
- includes the drafted mapping in a code block, with a pointer to the drift issue template.

### 4.3 Shared code (what mappings can't express)
- **Usage deduplication** by message or request id; records with no id are counted and flagged, not dropped.
- **Duplicates across files:** forked sessions, and Cursor subagents that also appear as their own sessions.
- **Time split** (only where `has_timestamps`):
  - *active time*;
  - *waiting for the human:* gaps from the end of the agent's turn to the next user message;
  - *tool time:* may include permission prompts, and is labelled so.

  Cursor: no time metrics, stated as such.
- **Lag handling** (§3).
- **Command detection:** strips leading env assignments (including unquoted `$(…)`); splits on `;`, `&&`, `||` and `|`; descends into `( … )` and `bash -c '…'`; ignores text that only mentions a command (commit messages, heredocs).

### 4.4 Redaction (on by default)
- Masks API keys and tokens (common prefixes plus high-entropy strings), emails, and full home paths (shown as `~/…`).
- Applies to everything the script prints: the summary, turn previews, `--sample` and `--events`.

### 4.5 Guarantees
- Read-only; standard library only.
- **No network:** a test asserts the script never imports a network module.
- **Writes nothing, anywhere.** All output goes to stdout.

## 5. CLI

```
transcript_metrics.py [PATH ...]           # no PATH: current session for --repo (default: cwd)
  --host auto|claude-code|cursor|codex
  --session-id ID                          # Claude Code passes ${CLAUDE_SESSION_ID}; Codex falls back to CODEX_THREAD_ID
  --list-turns
  --from-turn N --to-turn M                # default: whole session up to the reviewer's call
  --events I:J                             # normalised, redacted events for targeted checks
  --sample N                               # N redacted raw lines, for drafting a mapping
  --mapping FILE|-                         # override mapping; "-" reads it from stdin
  --verify-pattern REGEX                   # repeatable; also .workflow-review.toml if present
  --idle-threshold SECS                    # active vs waiting split (default 120)
  --drift-threshold PCT                    # default 90
  --format md|json                         # stdout only
  --no-redact
```

The optional `.workflow-review.toml` holds only verify patterns and thresholds, and the script only ever reads it.

## 6. Metrics the script reports

- **Coverage:**
  - host and mapping used, with its `tested_versions`;
  - drift status and capabilities;
  - the last record's timestamp, and the lag note if any;
  - what can't be measured.
- **Tokens** (deduplicated, with the method stated): fresh input, output, cache read and cache write, and API calls. Context size at the reviewer's invocation.
- **Time:** active, waiting for the human, tool time, wall time (only where the host has timestamps).
- **Totals:**
  - tool calls by category (read, search, edit, shell, agent, skill);
  - test, lint, build, typecheck and verify runs, and how many came before the last code edit;
  - repeated reads per file;
  - edits per file;
  - the same failing command retried (only where the host records tool results);
  - the largest tool outputs (size only);
  - tool errors.
- **Per actor:** model, brief size, tokens, tool calls, test runs.
- **Per turn:** the same, so one expensive stretch stands out.
- **Skills:**
  - each skill loaded, and how it was detected (a skill-tool call, or a read of `*/skills/*/SKILL.md`);
  - the bytes loaded, including reference files;
  - the inventory with description lengths;
  - instruction-file sizes (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules`, `.kiro/steering`).

## 7. Skill flow (`SKILL.md`, about 120 lines)

1. **Scope:** find the current session (§3); run `--list-turns` if the session held more than one task; set the window.
2. **Detect the workflow:** instruction files, skills, review records, PR templates, CI config, commit trailers, branch naming, and the verify commands (`Makefile`, `package.json`, `pyproject.toml`, `justfile`, CI). Summarise in five lines and ask only if something can't be inferred.
3. **Measure:** run the script once. On drift, follow §4.2.
4. **Confirm:** check each candidate finding with a small `--events` window, and find the instruction text that causes it, or name the gap.
5. **Analyse:** use `references/checklist.md` and `references/skills-audit.md`. Mark anything outside the checklist as a candidate new pattern.
6. **Propose:** diff sketches, quality check, host impact, risk and how to notice it failing, plus the "never propose" list. Order by impact ÷ (effort + risk). List rejected ideas.
7. **Deliver:** the report in chat (§10), then the research question if there is a Gap (§11). **The skill's scope ends here** (D21).

**Never propose:**
- removing or merging review roles, or letting a reviewer edit code;
- dropping final verification;
- weakening human approval gates;
- forbidding a reviewer from reading what it needs to check a claim;
- host-specific advice without a host label;
- removing a skill because one task didn't use it.

## 8. Skills audit rubric (`references/skills-audit.md`)

| Verdict | Evidence needed |
|---|---|
| **Keep** | Loaded in the task, its steps visibly happened, and no overlap with other instructions |
| **Improve** | Loaded, but steps skipped or repeated, its size out of proportion to its use, or ambiguous text that caused rework |
| **Merge or cut** | Its content duplicates another skill or `AGENTS.md`/`CLAUDE.md` (a static fact), or a large always-on description that this task's evidence shows didn't help |
| **Gap** | The task needed a capability that no loaded skill covered, or a matching skill existed but didn't trigger (a description mismatch) |
| **No verdict** | Not exercised by this task. One task can't prove a skill unnecessary |

Each verdict cites its evidence (event indices, sizes, quoted text) and a confidence level: high = measured, medium = inferred, low = one example. In degraded mode (D10), only "Merge or cut" on duplicated text and "No verdict" are possible.

## 9. Checklist (`references/checklist.md`)

**Universal**
- **Waste:**
  - repeated full test or verify runs while iterating;
  - orchestrator context dominated by cache reads;
  - repeated reads of the same file;
  - large tool outputs dumped into context;
  - the same failing command retried;
  - bookkeeping churn;
  - test-file churn;
  - always-on instruction bloat.
- **Quality risks:**
  - dependent agents spawned in parallel;
  - subagent `git restore`, `stash` or `reset` on uncommitted work;
  - a reviewer on the same or a weaker model than the implementer;
  - thin reviews;
  - records stating unverified facts as verified;
  - reviewer-invented requirements;
  - broad auto-approve or bypass settings;
  - untrusted content (web, files) reaching an agent that holds tools.
- **Process slips:**
  - rework from ambiguous specs;
  - a skill loaded but its steps skipped;
  - a skill that should have triggered but didn't.

**Optional packs:** spec-driven / task-gated; PR review.

**Growing it (D22):** the report flags candidate new patterns. A pattern is merged into the file once two independent reports show it, via the "new pattern" issue template.

Each finding records:
- ID and category;
- evidence (numbers plus event indices or file:line);
- cause (quoted instruction, or a named gap);
- cost in tokens, time or tool calls, or "not measurable";
- confidence;
- pattern (two or more occurrences in this task) or one-off.

## 10. Report (in chat only; template in `assets/report-template.md`)

1. **Header:** reviewer model and task model(s), host, coverage, drift status, lag note.
2. **Summary:** biggest cost (with its number), biggest quality risk, top three proposals.
3. **Where it went:** a compact table of totals plus the most expensive turns and actors.
4. **Findings:** at most 5 in full, the rest one line each.
5. **Skills audit:** one line per skill with its verdict and evidence.
6. **Proposals:** at most 3 in full, the rest one line each. Each has a diff sketch, quality check, host impact, and risk and how to notice it failing.
7. **Rejected ideas:** each with the quality reason.
8. **Not measurable here.**
9. **Candidate new patterns** (if any).
10. **Drafted mapping** (only if drift happened).
11. **Closing:** "Ask for any ID in full." If there's a Gap: "Search for skills or host features that cover G1/G2?"

**Cap:** about 80 lines (D20).

## 11. Opt-in research (`references/skills-audit.md`)

- Runs only if the user says yes, and only for the listed Gaps.
- **What it looks for:** skills, **and** host features, hooks and settings that address a measured cost (context management, subagent controls, hooks, test selection, caching).
- **Sources:**
  - official host docs and changelogs, and the hosts' own skill repos;
  - `agentskills.io`;
  - established open-source repos with a licence and recent commits.

  Blogs and social posts are leads only, until an official or maintainer source confirms them.
- **Recency:** the last six months, since no previous reports exist to date from.
- **Budget:** about 12 fetches in total. Changelogs are read newest first, stopping at the recency date.
- **For each item:** what it is, its source and date, its maturity, the host it applies to, the gap it closes, and the adoption effort. For a skill, also read its `SKILL.md` and scripts first and flag network or shell use.
- Never install or enable anything. "Nothing suitable found right now" is a valid result.

## 12. Hosts (`references/hosts.md`)

| Host | Skill locations | Transcripts | Explicit-only | Capabilities |
|---|---|---|---|---|
| Claude Code | `~/.claude/skills/`, project `.claude/skills/` (+ parents up to the repo root), plugin skills | `~/.claude/projects/<slug>/<session>.jsonl` + `<session>/subagents/*.jsonl`; moves with `CLAUDE_CONFIG_DIR` | `disable-model-invocation: true` ✅ official | full |
| Codex | `.agents/skills/` (cwd up to the repo root), `~/.agents/skills/` (`~/.codex/skills` deprecated), `/etc/codex/skills` | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (default `~/.codex`) | `agents/openai.yaml`: `allow_implicit_invocation: false` ✅ official source | full (to confirm on a real rollout) |
| Cursor | `.cursor/skills/`, `~/.cursor/skills/`, `.agents/skills/`, `~/.agents/skills/`, plus `.claude`/`.codex` compatibility folders | `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` (unofficial) | `disable-model-invocation: true` ✅ official. **Install to `.cursor/skills/`**: a forum report says the field is ignored in `.agents/skills/` | no tokens, timestamps, tool results or ids |
| Kiro | `.kiro/skills/`, `~/.kiro/skills/` | IDE: internal storage; CLI: SQLite in `~/.kiro/` (schema undocumented) | **none documented.** The description discourages auto-triggering; the README warns it may occasionally trigger on its own | degraded mode only |

Per-OS paths are documented for each host. Windows paths are marked **unverified** until confirmed. The file also carries the drift procedure (§4.2).

## 13. Repo layout

```
workflow-reviewer/
  README.md        # what and why, 60-second quick start, install per host, example excerpt, privacy note
  LICENSE          # MIT
  .github/workflows/test.yml                 # pytest on macOS, Linux, Windows
  .github/ISSUE_TEMPLATE/format-drift.md     # drafted mapping + record types, no content
  .github/ISSUE_TEMPLATE/new-pattern.md      # candidate pattern + evidence summary
  skills/workflow-reviewer/
    SKILL.md
    references/checklist.md
    references/skills-audit.md
    references/hosts.md
    assets/report-template.md
    scripts/transcript_metrics.py
    scripts/mappings/{claude_code,cursor,codex}.toml
    agents/openai.yaml
  tests/
    fixtures/{claude_code,cursor,codex}/   # synthetic only
    test_*.py
  examples/sample-report.md   # from synthetic data
  evals/                      # phase 2
```

**Install, per host** (README): copy or symlink `skills/workflow-reviewer/` into the host's skill folder (`.cursor/skills/` for Cursor), or use `npx skills add <owner>/workflow-reviewer`. No custom installer.

## 14. Tests (written first)

| Area | Cases |
|---|---|
| Bug 1: command detection | `A=1 B=$(mktemp -d) make verify` · `(export X=1; time make verify)` · `bash -c 'npm test'` · `uv run pytest` · a commit message that mentions `make verify` (must not count) · a heredoc body (must not count) · custom `--verify-pattern` |
| Bug 2: double counting | split records sharing one id are counted once · records with no id are counted and flagged · a forked session duplicating history across files · a Cursor subagent listed twice |
| Bug 3: time | an idle gap counted as waiting · tool time labelled · a host without timestamps gives "not measurable" |
| Session and window | `--session-id` selection · `CODEX_THREAD_ID` fallback · ambiguity (two recent transcripts) is listed, not guessed · self-exclusion at the reviewer's call · `--from-turn` · subagents attached to the right turn |
| Lag | a truncated final line is ignored · a missing reviewer invocation makes the window run to the end of the file · the lag note appears |
| Drift | a renamed field drops coverage and triggers the warning (quoting `tested_versions`) and exit code · an unknown record type is listed · a stdin mapping restores coverage · a `.zst` file is reported as unsupported |
| Mapping engine | dotted paths · array wildcards · equality matches · no code execution (an injected value stays inert) |
| Redaction | key prefixes · high-entropy strings · emails · home paths shown as `~/…` · applied to `--sample`, `--events` and `--list-turns` |
| Skills | detection via a skill-tool call and via a `SKILL.md` read · deduplication across folders · inventory limited to the task's host |
| Guarantees | no network imports · no file writes |

## 15. Build phases

| Phase | Work | Done when |
|---|---|---|
| 0 | Confirm formats on real Claude Code, Codex and Cursor files | Mappings drafted against real field paths; capability flags confirmed |
| 1 | Tests and fixtures, then the mapping engine, parser, shared code, analysis and redaction | All tests pass in the 3-OS CI matrix |
| 2 | `SKILL.md`, references, template | `SKILL.md` about 120 lines or fewer; every reference linked from it |
| 3 | README, issue templates, `examples/sample-report.md` | Quick start works on a clean install |
| 4 | Dry run on one of your real sessions (in chat, never committed) | Report reviewed by you |
| 5 | LinkedIn draft (under 150 words, one concrete finding, no hype) and a list of anything unverified | Delivered |
| Later | `evals/`: synthetic scenarios with expected findings | — |

## 17. Risks

| Risk | Mitigation |
|---|---|
| Transcript formats are internal and change between releases (Claude Code says so in its docs) | Drift detection, then a drafted mapping (§4.2); `tested_versions`; a fixture per format; drift issue template |
| Same-session audits re-read a large context on every step; may hit compaction when the context is near full (accepted, D19) | Compact script output; small `--events` windows; target of about 15 tool calls or fewer; context size reported |
| The agent grades its own work | Numbers come from the script; every finding cites event indices; self-reported evidence is not allowed (D10) |
| The wrong session is picked when several are open | Session id where available; otherwise ambiguity is listed and the user chooses (§3) |
| Transcripts lag behind the live session | Truncated line ignored; lag note; window runs to the end of the file (§3) |
| Explicit-only isn't enforceable on Kiro, and is reportedly ignored in Cursor's `.agents/skills/` | Discouraging description; install Cursor copies to `.cursor/skills/`; both documented |
| `CODEX_THREAD_ID` is undocumented | Labelled as such; falls back to the newest transcript plus the ambiguity check |
| Reports quote parts of transcripts | Redaction on by default; previews truncated; nothing written to disk |
| A drafted mapping is wrong | Coverage check after the re-run; medium confidence label; mapping shown for review |
| Thin evidence on Cursor or Kiro | Capability flags give explicit "not measurable" lines instead of guesses |
