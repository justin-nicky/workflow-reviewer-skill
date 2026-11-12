# Checklist

Match what the metrics show against these patterns. Anything outside them is still an action if the evidence supports it. Each match becomes one action in the findings ([findings](findings.md)): the pattern is the `why.cause`, the script's number is the `why.cost`, and turn or event numbers are the `why.evidence`.

## Waste (tokens and time that bought no quality)

- Repeated full test or verify runs while iterating (`verify.total`, `verify.before_last_edit`).
- Orchestrator context dominated by cache reads (`tokens.cache_read` against `tokens.fresh_input`; `tokens.context_at_invocation`).
- Repeated reads of the same file (`tools.repeated_reads`).
- Large tool outputs dumped into context (`tools.largest_outputs`).
- The same failing command retried (`tools.retried_failures`).
- Bookkeeping churn: edits to status or log files on every step.
- Test-file churn: the same test file edited many times (`tools.edits_per_file`).
- Always-on instruction bloat (`skills.instruction_files`, inventory description sizes) and duplicated instructions (`skills.overlaps`, see [skills-audit](skills-audit.md)).
- A hook that matches every call of a common tool (`skills.overlaps.hooks`: `every_call_of`).

## Quality risks

- Dependent agents spawned in parallel.
- A subagent running `git restore`, `stash` or `reset` on uncommitted work.
- A reviewer on the same or a weaker model than the implementer (`actors[].models`).
- Thin reviews: a reviewer with a small brief and few tool calls (`actors[].brief_bytes`, `tool_calls`).
- Records stating unverified facts as verified.
- Reviewer-invented requirements.
- Broad auto-approve or bypass settings.
- Untrusted content (web, files) reaching an agent that holds tools.
- Interruptions and turn errors (`interruptions`, `turn_errors`): ask what the user had to correct.

## Process slips

- Rework from an ambiguous spec (the same area edited and verified again after a user correction).
- A skill loaded but its steps skipped.
- A skill that should have triggered but did not (a gap).
- Two instructions that disagree and were both in context (`skills.overlaps.directive_pairs` with `judge` `eligible`).

## Optional packs

- **Spec-driven or task-gated:** task records, acceptance checks and gates actually run in order.
- **PR review:** review comments addressed, CI run before merge.

## Cost entries

Cost is tokens, time or tool calls from the script, or "not measurable on <host>". Never convert it to money. State what the number covers: this window only, or every session of a role.

## Trust in the numbers

Read `coverage.health` before leaning on a number. `ok` means a second, independent count agrees. `one_way` is normal (only one way to count it). `warn` means a structural check failed: say so in `metric_notes` and do not build an action on that metric alone. `not_measurable` means the host does not record it. A host label of `unproven` (an adapter made in this session) caps confidence at medium.

## Evidence

Cite turns as "turn 4" and events as "events 212 to 230" (the numbering of `--events`). Quote at most a few words of any text, redacted. A claim with no evidence is not reported.
