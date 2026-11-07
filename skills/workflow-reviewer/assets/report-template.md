# Workflow review: <scope in one line; task model and reviewer model>

**Bottom line:** <where the cost went + the single change that matters most; max 2 sentences, with the key number>

## Metrics

| Measure | Value |
|---|---|
| API calls | |
| Output tokens | |
| Cache read / write | |
| Fresh input | |
| Context at invocation | |
| Tool calls | |
| Time (active / waiting / tools) | |
| Verify runs (before last edit) | |
| Subagents | |
| Interruptions | |
| Coverage | <!-- row only on format drift or an unmeasurable metric; omit on a clean run --> |

<!-- Use "not measurable on <host>" where the host lacks the data. -->

## Do these (ranked by impact)

### 1. <action> · impact H/M/L · effort H/M/L · risk H/M/L · confidence H/M/L · <host>

**Why**
- <cause: the quoted instruction or config, or a named gap, as one plain sentence>
- **Cost:** <tokens or time, and what the number covers: this window only, or every session of a role; "not measurable" if so>
- **Evidence:** <numbers and locations: event indices or file:line>

**What to do**
- <change: file + short diff sketch>
- **Safe because:** <the quality mechanism it touches and why it still works>
- **Watch for:** <the failure signal and how to notice it>

<!-- Up to 5 items, same shape. -->

## Skills audit

- `<skill>` · <Keep | Improve | Merge or cut | Gap> · <evidence: event indices, sizes or quoted text>
- G1 · Gap · <evidence>
- No verdict: <N> other skills (not exercised in this task)

Search for skills or host features that cover G1/G2?
<!-- Closing question only when at least one Gap was found. -->
