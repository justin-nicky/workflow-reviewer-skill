# Adding or fixing an adapter (contributor guide)

This is contributor work, done outside a review: in its own session, or by hand. A review never builds or saves an adapter. When a review cannot measure a core number it says which one, offers degraded mode or stopping, and points to the issues page (https://github.com/justin-nicky/workflow-reviewer-skill/issues). Use this guide when you want to add a host that has no built-in adapter, or to fix a supported host whose format changed.

Nothing here installs anything, opens a pull request or pushes. You run those commands yourself.

## The steps

1. **Probe.** `transcript_metrics.py --probe PATH --format json` on the host's session file, session folder or database. The output is structure only (paths, types, counts, short type values, timestamp shapes, array lengths), never content. Never open the host's settings or credential files (for Cline, nothing under `~/.cline/data/settings` or `secrets.json`).
2. **Draft a mapping** in the shape of `scripts/mappings/*.toml`. [mapping-keys.md](mapping-keys.md) lists every key the engine accepts, so there is no need to read the script's source: `schema_version = 1`, a `[container]` (`jsonl`, `sqlite` or `json_document`), `[capabilities]`, `[fields]`, `[[records]]`, `[[skill_folders]]` where known, and `[[checks]]` (second counts) for the core metrics wherever the data allows it. Mark a metric the host does not record as not measurable: never guess a number, an offered-skills list or an instruction file.
3. **Save and check.** `transcript_metrics.py <selection arguments for a session to check it on> --save-adapter NAME < draft.toml`. The script validates the draft, runs it on that session and writes the file only if no core number is missing. It never overwrites without `--replace`.
   - A mapping for a host that already has a built-in one becomes a variant next to the built-in ones, chosen by shape. It may add record mappings, but it must keep every built-in check of that host with the same or a smaller tolerance and slack, the same or a higher severity, and no new way to skip it. Otherwise `--save-adapter` refuses with exit 2: "an adapter may add mappings, not remove or loosen checks".
   - Never remove, loosen or skip a check to make a session pass. A check that fails is what the adapter has to explain.
4. **Health.** Re-run with `--host NAME --health-all`. Core metrics must be `ok` or `one_way`. A user adapter is labelled `unproven`.
5. **Fixtures.** `--make-replica` writes a synthetic replica of the session to `<tempdir>/workflow-reviewer/replica-<host>-<time>/`: record types, order, nesting and counts kept; all text, ids, paths and names replaced; numbers kept; timestamps shifted. A leak check runs first and nothing is written if any source string of six or more characters would survive. Look through it before sharing. Then add the mutation rows for the host in `tests/test_health_mutations.py` ([adding-a-host](adding-a-host.md)).
6. **Pull request.** `--pr-bundle` prints the adapter path, the replica path, the expected numbers for the golden test, and the `gh` and `git` commands (fork, branch, copy the files, commit, push, `gh pr create` against the official repository). It runs nothing.

## Before opening a pull request

- Check upstream `main` of the official repository (`justin-nicky/workflow-reviewer-skill`) for an adapter or fix for this host and version first. If one exists, update the skill instead.
- Know the host version. When `coverage.health.version.position` is `unknown`, find it before proposing the adapter.
