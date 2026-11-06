# Workflow review (synthetic example)

Reviewer: claude-opus-x. Task model: claude-sonnet-x (main), claude-opus-x (1 subagent). Host: Claude Code. Coverage: full, no drift. Last record 4 min old.

## Summary
- Biggest cost: 11 full `make verify` runs, 7 of them before the last code edit (about 38% of tool time).
- Biggest quality risk: F2, review run by the same model that implemented.
- Top proposals: P1 run the targeted tests while iterating, full verify once at the end. P2 review on a stronger model. P3 trim `CLAUDE.md` always-on section.

## Where it went
| | |
|---|---|
| Tokens | fresh 41k, output 22k, cache read 3.1M, cache write 190k, 212 API calls |
| Tool calls | read 58, search 21, edit 34, shell 47, agent 2 |
| Time | wall 52m: active 19m, waiting for you 24m, tools 9m (may include permission prompts) |
| Costly turn | turn 3 "tests fail on CI": 61% of output tokens |

## Findings
- **F1 waste (pattern, high):** `make verify` 11x, 7 before the last edit; events 88, 97, 104. Cause: instruction "run make verify after every change". Cost: about 3.4 min of tool time.
- **F2 quality risk (one-off, medium):** reviewer subagent ran on the implementer's model. No instruction names a reviewer model.
- F3 waste: `src/limiter.py` read 6 times (events 40 to 120).

## Skills audit
- tdd: Keep (loaded, red-green steps visible, events 30 to 70).
- release-notes: No verdict (not exercised).
- Gap G1: no skill covered test selection; matching description absent.

## Proposals
- **P1 (host: all):** in `CLAUDE.md`, replace "run make verify after every change" with "run the targeted test while iterating; run make verify once before reporting done". Quality check: final verification still runs and gates completion. Risk: a regression found later; notice it by CI failing after a green local report.
- P2: set the reviewer to a stronger model. P3: move the release section out of `CLAUDE.md`.

## Rejected
- Dropping the reviewer subagent: removes the independent review.

## Not measurable here
- Nothing: the host records timestamps, tokens and tool results.

Ask for any ID in full. Search for skills or host features that cover G1?
