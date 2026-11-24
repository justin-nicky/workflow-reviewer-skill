# The adapter flow (the agent's procedure)

Use this when the host has no built-in adapter, or when a supported host's format has changed and the health table shows a broken core metric (exit 2). It builds a mapping from the structure of the host's own data, checks it, and saves it as a user adapter. It never installs anything, never opens a pull request, never pushes.

## When to start

- **Unsupported host** (no built-in mapping, or `--host auto` finds no known host): pause. Say the host is not supported out of the box and offer two choices: (a) degraded mode (working tree and repo records only), or (b) build an adapter, which takes about five minutes. Continue only if the user picks (b).
- **Supported host, breaking health:** `coverage.health` has a core row that is `broken` (exit 2). Run the same flow; the result is saved as a user *variant* of that host, chosen by shape next to the built-in ones. It never replaces them.
- Pause for nothing else. Warn rows and `one_way` rows continue with exit 0.

## The flow

1. **Probe.** `transcript_metrics.py --probe PATH` on the host's session file, session folder or database. Use `--format json`. The output is structure only (paths, types, counts, short type values, timestamp shapes, array lengths), so it is safe to read and to quote, but do not quote more of it than you need. Never open the host's settings or credential files (for Cline, nothing under `~/.cline/data/settings` or `secrets.json`).
2. **Draft a mapping** in the shape of `scripts/mappings/*.toml`. [mapping-keys.md](mapping-keys.md) lists every key the engine accepts, so never read the script's source for this: `schema_version = 1`, a `[container]` (`jsonl`, `sqlite` or `json_document`), `[capabilities]`, `[fields]`, `[[records]]`, `[[skill_folders]]` where known, and `[[checks]]` (second counts) for the core metrics wherever the data allows it. Declarative keys only; an unknown key is refused. Mark a metric the host does not record as not measurable: never guess a number, an offered-skills list or an instruction file.
3. **Save and check.** Pipe the draft to `transcript_metrics.py <same selection arguments as the metrics run> --save-adapter NAME < draft.toml`. The script validates it, runs it on this session and writes the file only if no core metric is broken (exit 2 and a list of what is broken otherwise; fix the draft and repeat). It never overwrites without `--replace`. Do not print the drafted mapping in the report.
4. **Health.** Re-run with `--host NAME --health-all`. Core metrics must be `ok` or `one_way`. The adapter is labelled `unproven` in the output and in the report.
5. **Full report.** Produce the normal review with the new adapter. Say in the Coverage row that the metrics come from an adapter made in this session (medium confidence).
6. **A polite PR request,** only if the conditions below hold and the user agrees. The request only gives commands.

## The PR request

Ask whether the user wants to share the adapter so it can become a built-in. Offer it only when all of this is true:

- the local host version is **newer** than the mapping's `tested_versions` (`coverage.health.version.position` is `newer`), or the host has no built-in adapter at all;
- you have checked upstream `main` of the official repository (`justin-nicky/workflow-reviewer-skill`, nowhere else) with your own web tool and found no adapter or fix for this host and version. If one exists, tell the user to update the skill instead and stop;
- the version is known. When `coverage.health.version.position` is `unknown`, ask the user for the host version first.

If the user agrees:

1. `--make-replica` (only at the user's explicit choice) writes a synthetic replica of this session to `<tempdir>/workflow-reviewer/replica-<host>-<time>/`: record types, order, nesting and counts kept; all text, ids, paths and names replaced; numbers kept; timestamps shifted. A leak check runs first and nothing is written if any source string of six or more characters would survive. Tell the user the path and that it is synthetic, and suggest they look through it before sharing.
2. `--pr-bundle` prints the adapter path, the replica path, the expected numbers for the golden test, and the exact `gh` and `git` commands (fork, branch, copy the files, commit, push, `gh pr create` against the official repository). It runs nothing. Give the commands to the user as they are; the user runs them.

Never run those commands yourself, never push, never create a fork or a pull request, and never install anything.
