# The findings JSON

The agent sends judgments only; the script works out every number. Pipe one JSON object into `--report -`. It is checked strictly: an unknown key, a bad value, a missing key or a limit exceeded is exit 7 with a list of errors (each names its path, for example `actions[1].impact`). Fix them and send it again.

| Field | Rule |
|---|---|
| `schema_version` | `1` |
| `title` | the task reviewed, up to 80 characters |
| `bottom_line` | up to 300 characters; at most one `**bold**` span, nothing else may use `**` |
| `actions` | 0 to 5, in rank order; `impact` must never go up later in the list (high, then medium, then low) |
| `actions[].title` | up to 100 characters |
| `actions[].impact`, `effort`, `risk` | `high`, `medium` or `low` (for effort and risk, `low` is the good end) |
| `actions[].confidence` | `high`, `medium` or `low` |
| `actions[].applies_to` | optional: `all hosts` or one host name (`claude-code`, `codex`, `cursor`, `devin`, `cline`, `kiro`) |
| `actions[].why` | `cause` and `cost` (up to 300 characters each), `evidence` (up to 3 items of up to 240 characters) |
| `actions[].what` | `change`, `safe_because`, `watch_for` (up to 300 characters each) |
| `skills` | up to 20: `name`, `verdict`, `evidence` (up to 240 characters), optional `overlaps_with` (up to 5 names) |
| `skills[].verdict` | `keep`, `improve`, `merge`, `gap`, `overlap` or `none`. The renderer labels gaps G1, G2, … in order |
| `closing_question` | allowed only when at least one skill has the verdict `gap` |
| `metric_notes` | optional short notes keyed by a metric row id: `calls`, `output`, `cache`, `fresh`, `context`, `tools`, `time`, `verify`, `subagents`, `interruptions`, `tokens` |

Only `` `backticks` `` format text (they become code). Every string is redacted like the metrics, so a secret that slips in is masked, but do not put one there. Do not send numbers: the report's table, bar and counts come from the metrics run, and a `metrics` key is an unknown-key error.

## Example

```json
{
  "schema_version": 1,
  "title": "Add rate limiting to /login",
  "bottom_line": "Most of this task's cost is re-reading a **154K-token context** on every call; `CURRENT_STATE.md` is about 17K tokens of it.",
  "actions": [
    {
      "title": "Trim `CURRENT_STATE.md`",
      "impact": "high", "effort": "low", "risk": "low", "confidence": "medium",
      "applies_to": "all hosts",
      "why": {
        "cause": "`CLAUDE.md` loads it and nothing says to prune it, so closed entries stay.",
        "cost": "About 11% of the context, re-read on all 21 calls of this window.",
        "evidence": ["68,252 bytes loaded in turn 1", "re-cached after the gaps in turns 4 and 5 (events 212 to 230)"]
      },
      "what": {
        "change": "Move closed entries into `HISTORY.md`, read on demand.",
        "safe_because": "No gate, role or approval changes; reviewers can still read the history file.",
        "watch_for": "An open carry-over moved by mistake; move only entries marked closed."
      }
    },
    {
      "title": "Keep one review skill",
      "impact": "medium", "effort": "low", "risk": "high", "confidence": "low",
      "applies_to": "claude-code",
      "why": {
        "cause": "Both descriptions match a review request, so both load and their checklists disagree.",
        "cost": "About 9K extra tokens in context from turn 3.",
        "evidence": ["both loaded in turn 3 (skills.overlaps: judge eligible)"]
      },
      "what": {
        "change": "Keep `code-review`; narrow the other description to summarising pull requests.",
        "safe_because": "The independent review role and its blocking rule stay.",
        "watch_for": "A summary you relied on disappearing."
      }
    }
  ],
  "skills": [
    {"name": "code-review", "verdict": "overlap", "evidence": "Loaded with its twin in turn 3.", "overlaps_with": ["pr-review"]},
    {"name": "pr-review", "verdict": "merge", "evidence": "See action 2."},
    {"name": "state-doc pruning", "verdict": "gap", "evidence": "Nothing prunes `CURRENT_STATE.md` at close-out."}
  ],
  "closing_question": "Search for skills or host features that cover G1? Reply yes and the review will check official sources only.",
  "metric_notes": {"verify": "expected: this task only reads"}
}
```
