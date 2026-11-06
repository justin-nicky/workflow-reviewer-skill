# Checklist

Match findings against these patterns. Anything else is a **candidate new pattern**: flag it with evidence; it is merged here once two independent reports show it (new-pattern issue template).

## Waste

- Repeated full test or verify runs while iterating (script: `verify.total`, `before_last_edit`).
- Orchestrator context dominated by cache reads (`tokens.cache_read` vs fresh input; `context_at_invocation`).
- Repeated reads of the same file (`tools.repeated_reads`).
- Large tool outputs dumped into context (`tools.largest_outputs`).
- The same failing command retried (`tools.retried_failures`).
- Bookkeeping churn: edits to status or log files on every step.
- Test-file churn: the same test file edited many times (`tools.edits_per_file`).
- Always-on instruction bloat (`skills.instruction_files`, inventory description sizes).

## Quality risks

- Dependent agents spawned in parallel.
- A subagent running `git restore`, `stash` or `reset` on uncommitted work.
- A reviewer on the same or a weaker model than the implementer (`actors[].models`).
- Thin reviews: a reviewer with a small brief and few tool calls.
- Records stating unverified facts as verified.
- Reviewer-invented requirements.
- Broad auto-approve or bypass settings.
- Untrusted content (web, files) reaching an agent that holds tools.

## Process slips

- Rework from an ambiguous spec (same area edited and verified again after a user correction).
- A skill loaded but its steps skipped.
- A skill that should have triggered but did not (a Gap).

## Optional packs

- **Spec-driven / task-gated:** task records, acceptance checks and gates actually run in order.
- **PR review:** review comments addressed, CI run before merge.

## Cost entries

Cost is tokens, time or tool calls from the script, or "not measurable on <host>". Never convert to money.
