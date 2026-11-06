---
labels: [ready-for-agent]
title: workflow-reviewer, an agent skill that audits the coding-agent task you just finished
---

## Problem Statement

Developers who run coding agents (Claude Code, Codex, Cursor, Kiro) on real tasks can't easily see where tokens and time went, or which process steps slipped. Transcripts are large, host-specific and change between releases. Asking the agent to describe its own work is unreliable, because it grades itself from recollection. Trimming cost is also risky: a "saving" that removes an independent review, final verification, a fail-closed rule or a human approval gate quietly lowers quality. Developers have no cheap, trustworthy way to ask "what did that task cost, what was wasted, and what should I change?"

## Solution

An open-source, explicit-only agent skill, `workflow-reviewer`, that the developer invokes right after finishing a task, in the same session. A deterministic, read-only, standard-library Python script reads the host's transcript and prints a compact, redacted metrics summary of about 2k tokens. The agent confirms candidate findings with small event windows, then returns a report in chat (about 80 lines) containing:

- findings;
- per-skill verdicts;
- at most three proposals, each naming the quality mechanism it touches and why that mechanism still works.

The audit only reports. It never edits code, skills or config, and it never writes anything to disk. All numbers come from code, and anything not measured is labelled "not measurable on <host>". If a Gap is found, the developer is offered an opt-in search for skills or host features that cover it.

## User Stories

1. As a developer who just finished an agent task, I want to run one explicit command in the same session, so that I get an audit without setting anything up.
2. As a developer, I want the skill to trigger only when I invoke it, so that it never burns tokens on its own.
3. As a developer, I want the report to show total tokens (fresh input, output, cache read, cache write) and API calls, so that I know what the task cost.
4. As a developer, I want token usage deduplicated by message/request id, so that split records are not double counted.
5. As a developer, I want records with no id counted and flagged rather than dropped, so that totals are not understated silently.
6. As a developer, I want the context size at the moment of invocation reported, so that I can see how close I was to compaction.
7. As a developer, I want time split into active, waiting for the human, and tool time, so that I know whether the agent or I was the bottleneck.
8. As a developer, I want tool time labelled as possibly including permission prompts, so that I don't misread it as pure execution.
9. As a Cursor user, I want "not measurable on Cursor" for time and tokens, so that I'm not given invented numbers.
10. As a developer, I want a per-actor breakdown (main agent and each subagent) with model, brief size, tokens, tool calls and test runs, so that I can see which agent was expensive.
11. As a developer, I want a per-turn breakdown, so that one expensive stretch stands out.
12. As a developer, I want tool calls categorised (read, search, edit, shell, agent, skill), so that I see the shape of the work.
13. As a developer, I want counts of test/lint/build/typecheck/verify runs and how many ran before the last code edit, so that I can spot wasted early verification.
14. As a developer, I want verify commands detected inside env-prefixed commands, subshells, `bash -c`, pipelines and `&&`/`||`/`;` chains, so that real runs are counted correctly.
15. As a developer, I want commands that merely mention a verify command (commit messages, heredocs) not counted, so that the numbers are honest.
16. As a developer, I want to add custom verify patterns via a flag or a `.workflow-review.toml`, so that project-specific commands are recognised.
17. As a developer, I want repeated reads per file and edits per file listed, so that churn is visible.
18. As a developer, I want the same failing command retried to be flagged where the host records tool results, so that I find loops.
19. As a developer, I want the largest tool outputs listed by size only, so that I can find context-dumping commands without seeing the content.
20. As a developer, I want tool errors counted, so that I see friction.
21. As a developer, I want the audit window to default to the whole session up to the reviewer's own invocation, so that the reviewer excludes itself.
22. As a developer whose session held several tasks, I want to list user turns with redacted 80-character previews and pick a start with `--from-turn`, so that I audit only the task I mean.
23. As a developer, I want a task split across multiple transcript files merged in time order, so that resumed or forked work is covered.
24. As a developer, I want forked-session duplicates and Cursor subagents that also appear as own sessions counted once, so that totals are not inflated.
25. As a developer, I want the current session found by session id where the host provides one, so that the right transcript is chosen.
26. As a developer, I want the newest-transcript fallback to list candidates and ask me when two or more were modified in the last 10 minutes, so that the wrong session is never guessed.
27. As a developer, I want a truncated final line ignored and a "latest turn may be incomplete" note when the file is near now, so that transcript lag does not corrupt results.
28. As a developer, I want the window to run to end of file when the reviewer's invocation is not yet written, with no retries or waiting, so that the audit stays fast.
29. As a developer, I want a drift warning (coverage, missing required fields, unseen record types, with the mapping's tested versions) when a host changes its format, so that I know numbers may be partial.
30. As a developer, I want the script to continue with the parts that still parse and exit with a distinct code on drift, so that I still get useful output.
31. As a developer, I want the agent to draft an override mapping from a redacted sample and re-run it via stdin, so that drift is repaired without writing files.
32. As a developer, I want metrics from a drafted mapping labelled medium confidence and the mapping shown in the report, so that I can judge and report it.
33. As a developer, I want a `.zst` rollout named and reported as unsupported, so that I know why there are no results.
34. As a developer with no usable transcript, I want a degraded mode from git history, diff and repo records, with confidence capped at medium and coverage stated up front, so that I still get something honest.
35. As a developer, I want the agent's own recollection never used as evidence, so that findings are trustworthy.
36. As a developer, I want each finding to carry an ID, category, evidence (numbers plus event indices or file:line), cause, cost or "not measurable", confidence, and pattern-vs-one-off, so that I can verify it.
37. As a developer, I want at most 5 findings and 3 proposals in full and the rest one line each, so that the report stays readable at about 80 lines.
38. As a developer, I want to ask for any finding or proposal ID in full, so that I can drill in without a wall of text.
39. As a developer, I want every proposal to include a diff sketch, quality check, host impact, and risk and how to notice it failing, so that I can decide safely.
40. As a developer, I want proposals ordered by impact ÷ (effort + risk) and rejected ideas listed with the quality reason, so that I see what was considered.
41. As a developer, I want the skill never to propose removing/merging review roles, dropping final verification, weakening approval gates, forbidding reviewers from reading what they need, host-specific advice without a label, or removing a skill because one task didn't use it, so that quality is protected.
42. As a developer, I want each loaded skill given a verdict (Keep, Improve, Merge or cut, Gap, No verdict) with evidence and confidence, so that I know which skills pay off.
43. As a developer, I want skills detected as loaded both by a skill-tool call and by a read of a `SKILL.md`, with bytes loaded including reference files, so that skill cost is measured.
44. As a developer, I want the skill inventory limited to locations the task's host loads, deduplicated by name and content hash, so that I'm not shown irrelevant skills.
45. As a developer, I want instruction-file sizes (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules`, `.kiro/steering`) reported, so that always-on bloat is visible.
46. As a developer, I want a skill that didn't get exercised to get "No verdict", so that one task never condemns a skill.
47. As a developer, I want an opt-in question about searching for skills or host features/hooks/settings only if a Gap was found, so that research happens only when I want it.
48. As a developer, I want the research to use official docs, changelogs, host skill repos, agentskills.io and licensed, recently active repos, within about 12 fetches, so that suggestions are credible and bounded.
49. As a developer, I want research results to say "nothing suitable found" when true, and never install or enable anything, so that I stay in control.
50. As a developer, I want secrets, tokens, emails and home paths masked in everything printed, so that quoted transcript content can't leak.
51. As a developer, I want guaranteed no network access and no file writes by the script, so that I can trust it with private transcripts.
52. As a developer, I want transcript, repo and web content treated as data, never instructions to the reviewer, so that prompt injection in a transcript can't hijack the audit.
53. As a Claude Code user, I want the session id passed automatically, so that no flags are needed.
54. As a Codex user, I want the thread id environment variable used, labelled undocumented, so that the right rollout is found.
55. As a Cursor user, I want reduced-capability mode with explicit "not measurable" lines, so that gaps are stated, not guessed.
56. As a Kiro user, I want degraded mode with a note that explicit-only invocation can't be enforced, so that I know it may occasionally trigger.
57. As an open-source maintainer, I want mappings as declarative TOML per host with capabilities and `tested_versions`, so that format changes are a data fix.
58. As a maintainer, I want mapping syntax limited to dotted paths, array wildcards and equality matches with no evaluation, so that a drafted mapping is inert.
59. As a maintainer, I want issue templates for format drift (mapping and record types, no content) and new patterns (pattern and evidence summary), so that contributors can report without leaking data.
60. As a maintainer, I want a candidate new pattern merged into the checklist once two independent reports show it, so that the checklist grows from evidence.
61. As a maintainer, I want CI on macOS, Linux and Windows, so that the script works on all three.
62. As a user on any OS, I want to install by copying or symlinking the skill folder or using `npx skills add`, so that no custom installer is needed.
63. As a developer, I want the report header to record the reviewer model and task model(s), host, coverage, drift status and lag, and to recommend a reviewer at least as strong as the task model, so that I can judge the audit's reliability.
64. As a developer, I want the audit to finish in about 15 tool calls or fewer with a summary around 2k tokens, so that auditing inside a full context stays cheap.
65. As a developer, I want the skill's scope to end at the report and a later "apply P1" treated as an ordinary request, ideally in a separate commit or branch, so that the audit never changes my code.

## Implementation Decisions

**Skill package and scope**
- One skill, `workflow-reviewer`, under a `skills/` folder compatible with `npx skills`. It is MIT licensed, with a README (quick start, per-host install, example excerpt, privacy note), a CI workflow and two issue templates.
- Scope is the task just completed, audited in the same session only. There is no fresh-session mode and no comparison with other sessions. Invocation is explicit only: `disable-model-invocation: true` on Claude Code and Cursor; `allow_implicit_invocation: false` in Codex's agent config; a discouraging description plus a README warning on Kiro. Cursor copies install to `.cursor/skills/`.
- Output is in chat only. Nothing is saved, including drafted mappings. Cost is tokens only, with no pricing.
- The skill flow is:
  1. scope;
  2. detect the workflow (five-line summary, ask only if it can't be inferred);
  3. measure (run the script once);
  4. confirm candidates with `--events` windows;
  5. analyse against the checklist and skills rubric;
  6. propose;
  7. deliver the report, then the Gap research question.
- `SKILL.md` stays at about 120 lines. Detail lives in reference documents (checklist, skills audit rubric, host table) and a report template.

**Metrics script (single module, standard library only, Python 3.11+, `pytest` for tests only)**
- Pipeline: detect format → mapping → generic parser → shared code → analysis → redaction → stdout.
- Mappings are declarative TOML, one per host (Claude Code, Codex, Cursor). Each declares capabilities (tokens, timestamps, tool results, ids) and `tested_versions`. Syntax is dotted paths, array wildcards and equality matches only. A mapping can be overridden at run time from a file or from stdin.
- Hosts in v1: Claude Code and Codex in full; Cursor with reduced capabilities (no tokens, timestamps, tool results or ids); Kiro in degraded mode (git and repo records only).
- Drift detection passes only if all of these hold:
  - at least 90% of lines are classified (threshold configurable);
  - required fields are present;
  - unseen record types are listed by name.

  On failure it prints a warning quoting the tested versions, continues with what parses, and exits with a distinct code.
- Shared logic outside mappings:
  - usage dedupe by message/request id;
  - cross-file duplicate handling (forks, Cursor subagents);
  - time split (active, waiting for human, tool time) with a configurable idle threshold;
  - lag handling;
  - robust command detection (env-assignment stripping, splitting on `;`, `&&`, `||`, `|`, descending into `( )` and `bash -c`, ignoring commit-message and heredoc mentions).
- Session discovery: session id where the host provides one (Claude Code passes `${CLAUDE_SESSION_ID}`; Codex reads `CODEX_THREAD_ID`, labelled undocumented). Otherwise the newest transcript for the repo, listing candidates when two or more were modified in the last 10 minutes. Multiple paths merge in time order. Subagents are linked by the spawning call on Claude Code, by brief and time on Cursor, and by parent id or time on Codex.
- Window controls: `--list-turns`, `--from-turn`/`--to-turn`, `--events I:J`, `--sample N`.
- Output is Markdown or JSON on stdout only. The CLI flags are:
  - path arguments;
  - host selection;
  - session id;
  - turn controls;
  - events;
  - sample;
  - mapping;
  - repeatable verify pattern;
  - idle threshold;
  - drift threshold;
  - format;
  - no-redact.

  An optional, read-only `.workflow-review.toml` supplies verify patterns and thresholds.
- Redaction is on by default for everything printed: API-key prefixes, high-entropy strings, emails, and home paths shown as `~/…`.
- Guarantees: read-only, no network module imports, no file writes. Compressed `.zst` rollouts are named and reported as unsupported.
- Reported metrics:
  - coverage and drift status;
  - tokens;
  - time;
  - tool-call totals and verify runs;
  - repeated reads, edits per file, retried failures, largest outputs, errors;
  - per-actor and per-turn breakdowns;
  - skills loaded (detection method, bytes including references);
  - skill inventory with description lengths;
  - instruction-file sizes.

**Analysis and report (agent-side, driven by reference documents)**
- The report follows the 11-section template, capped at about 80 lines.
- Skill verdicts use the five-verdict rubric with cited evidence and a confidence level. In degraded mode only "Merge or cut" on duplicated text and "No verdict" are possible.
- The checklist covers waste, quality risks and process slips, plus optional spec-driven and PR-review packs. It grows from candidate patterns once two independent reports show them.
- Principles: quality first (every proposal names the mechanism it touches), evidence over description, code counts and the LLM judges (the LLM never reads a raw transcript), never invent numbers, the auditor must be cheap, and untrusted data.

## Testing Decisions

- **One seam.** All behaviour is tested through the metrics CLI, run black-box on synthetic fixtures. Tests assert on stdout (JSON for precision, Markdown for the report-facing form) and on exit code. A mapping-engine seam is not created: path, wildcard, equality and inert-injection behaviour is exercised by running the CLI with fixture mappings via file or stdin.
- **Good test:** it asserts only external behaviour (the printed numbers, flags, warnings, exit codes, and absence of side effects), never internal function names or parse structure. It uses small synthetic transcripts built to isolate one behaviour.
- **Fixtures:** synthetic only, one set per host (Claude Code, Codex, Cursor). Real transcripts are used only in phase 0 to confirm formats and are never committed.
- **Test-first, covering these areas:**
  - command detection (env prefixes, subshells, `bash -c`, `uv run pytest`, mentions in commit messages and heredocs not counted, custom verify patterns);
  - double counting (split records sharing an id, records with no id flagged, forked sessions, Cursor subagent listed twice);
  - time (idle gap counted as waiting, tool time labelled, "not measurable" without timestamps);
  - session and window (session-id selection, Codex thread-id fallback, ambiguity listed not guessed, self-exclusion, `--from-turn`, subagents attached to the right turn);
  - lag (truncated final line ignored, missing invocation runs to end of file, lag note);
  - drift (renamed field triggers warning and exit code, unknown record types listed, stdin mapping restores coverage, `.zst` reported unsupported);
  - mapping engine behaviour via the CLI (injected values stay inert);
  - redaction (key prefixes, high-entropy strings, emails, home paths, applied to sample, events and list-turns);
  - skills (detection by skill-tool call and by `SKILL.md` read, dedupe across folders, inventory limited to the task's host);
  - guarantees (no network imports; no file writes, checked by running in a read-only or watched directory).
- **Prior art:** none in this project (greenfield). Fixtures and the CI matrix (pytest on macOS, Linux, Windows) are created as part of the work.
- The agent-side flow (`SKILL.md` behaviour, report quality) is not unit-tested here. It is covered by later `evals/` with synthetic scenarios and expected findings, plus a manual dry run on one real session reviewed by the author.

## Out of Scope

- Analysis of human intervention vs approval gates.
- Checking whether past proposals saved anything.
- The auditor reporting its own cost.
- Saved or persisted reports, or any file output.
- Pricing, dollars or a price config.
- Comparison with other or recent sessions.
- A fresh-session mode.
- Self-reported evidence from the agent.
- A lite mode when the context is near full.
- `.zst` (compressed rollout) support.
- Applying proposals: a later "apply P1" is an ordinary request outside the skill.
- Automatic installation or enabling of researched skills or features.
- A custom installer.
- The `evals/` suite (phase 2).
- Full Kiro transcript parsing (its storage is undocumented).

## Further Notes

- **Open items before the mappings can be finalised (phase 0):**
  - confirm real formats from one Codex rollout, one Cursor agent transcript, and one multi-turn Claude Code session with subagents (also used for the dry run);
  - verify whether Claude Code compaction keeps writing to the same file;
  - verify Codex subagent linking (parent id or time);
  - verify Windows transcript paths for all three hosts;
  - verify the forum report that Cursor ignores `disable-model-invocation` in `.agents/skills/`.
- Claude Code documents its transcript format as internal and changing between versions, so the drift path is expected to run in normal use.
- `CODEX_THREAD_ID` is undocumented and may change.
- Build phases: 0 verify formats; 1 tests/fixtures then engine, parser, shared code, analysis, redaction; 2 `SKILL.md`, references, template; 3 README, issue templates, sample report; 4 dry run on a real session (in chat, never committed); 5 a short LinkedIn draft plus a list of anything unverified.
- Source: `workflow-reviewer-plan.md`, agreed 2026-11-06 after brainstorming and two grilling rounds.
