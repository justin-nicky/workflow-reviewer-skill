# Workflow review

Fill every section in chat. About 80 lines in total. Do not write this to a file.

1. **Header:** reviewer model and task model(s), host, coverage, drift status (and mapping source), lag note.
2. **Summary:** biggest cost (with its number), biggest quality risk, top three proposals.
3. **Where it went:** compact table of totals (tokens by type, API calls, tool calls by category, verify runs, time split) plus the most expensive turns and actors.
4. **Findings:** at most 5 in full (ID, category, evidence, cause, cost, confidence, pattern/one-off); the rest one line each.
5. **Skills audit:** one line per skill: verdict, evidence, confidence.
6. **Proposals:** at most 3 in full, the rest one line each. Each: diff sketch, quality check (which mechanism it touches and why it still works), host impact, risk and how to notice it failing.
7. **Rejected ideas:** each with the quality reason.
8. **Not measurable here.**
9. **Candidate new patterns** (if any), with evidence.
10. **Drafted mapping** (only if drift happened), in a code block.
11. **Closing:** "Ask for any ID in full." If there is a Gap: "Search for skills or host features that cover G1/G2?"
