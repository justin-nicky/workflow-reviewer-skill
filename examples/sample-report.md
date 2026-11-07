# Workflow review: add rate limiting to /login (task on claude-sonnet-x, reviewed by claude-opus-x)

**Bottom line:** Most tool time went to repeated full verify runs (7 of 11 ran before the last edit, about 3.4 of 9 minutes of tool time). The change that matters most is the reviewer running on the implementer's own model; fix that first.

## Metrics

| Measure | Value |
|---|---|
| API calls | 212 |
| Output tokens | 22k |
| Cache read / write | 3.1M / 190k |
| Fresh input | 41k |
| Context at invocation | 148k |
| Tool calls | 162 (read 58, search 21, edit 34, shell 47, agent 2) |
| Time (active / waiting / tools) | 19m / 24m / 9m (wall 52m) |
| Verify runs (before last edit) | 11 (7) |
| Subagents | 1 (reviewer, claude-sonnet-x) |
| Interruptions | 0 |

## Do these (ranked by impact)

### 1. Run the reviewer on a stronger model than the implementer · impact M · effort L · risk L · confidence M · Claude Code

**Why**
- No instruction names a reviewer model, so the review subagent inherited the implementer's model.
- **Cost:** quality risk, not tokens: applies to every session that uses the reviewer role. Not measurable in tokens.
- **Evidence:** actor `subagent:agent-1` ran on claude-sonnet-x, the main agent's model; brief 1.2 KB, 6 tool calls (events 131 to 160).

**What to do**
- In the reviewer's agent file, add `model: opus` to the front matter.
- **Safe because:** it strengthens the independent review and leaves its read-only role, brief and gate untouched.
- **Watch for:** review cost per session rising; compare the reviewer's tokens before and after.

### 2. Run targeted tests while iterating; run `make verify` once before reporting done · impact M · effort L · risk L · confidence H · all hosts

**Why**
- `CLAUDE.md` says "run make verify after every change".
- **Cost:** about 3.4 min of tool time in this window (7 early runs at about 29 s each); the instruction applies to every session in this repo.
- **Evidence:** verify runs 11, 7 before the last code edit (events 88, 97, 104, ...).

**What to do**
- In `CLAUDE.md`, replace the line with "run the targeted test while iterating; run make verify once before reporting done".
- **Safe because:** final verification still runs and still gates "done"; only the intermediate repeats go.
- **Watch for:** a regression that only the full run catches; notice it by CI failing after a green local report.

### 3. Move the release section out of the always-on `CLAUDE.md` · impact L · effort L · risk L · confidence M · all hosts

**Why**
- The 1,840-byte release section is in `CLAUDE.md` and duplicated by the `release-notes` skill description.
- **Cost:** about 98k cache-read tokens in this window (about 460 tokens on each of 212 API calls, 3% of cache reads); every session pays it.
- **Evidence:** instruction file 1,840 bytes of 6,200; `release-notes` inventory entry 1,840 bytes; the task never used either.

**What to do**
- Delete the section from `CLAUDE.md` and keep it in the `release-notes` skill only.
- **Safe because:** it touches no review, verification or approval gate; the skill still loads when releasing.
- **Watch for:** a release done without the checklist; the skill's description should still match "release".

## Skills audit

- `tdd` · Keep · loaded at event 30; red-green steps visible, events 30 to 70
- `release-notes` · Merge or cut · its 1,840-byte description duplicates the `CLAUDE.md` release section; see item 3
- G1 · Gap · no loaded skill covers selecting affected tests; events 88 to 104 repeat the full suite
- No verdict: 11 other skills (not exercised in this task)

Search for skills or host features that cover G1?
