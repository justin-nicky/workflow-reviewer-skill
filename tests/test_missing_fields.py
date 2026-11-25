"""A core field that is missing from every record that should carry it is a missing number: null, "not measurable", exit 2. Never 0."""
import json
import re
import shutil
import sqlite3

import pytest
from test_health_mutations import GOLDEN, MUTATIONS, devin_usage_field, devin_turn_marker, edit_messages, files_of, read, rename, write
from test_report import FINDINGS


def run(cli, tmp_path, host, mutate, *args):
    """Mutated golden of a host: (exit code, metrics json, markdown report, html report)."""
    if host == "devin":
        db = tmp_path / "sessions.db"
        shutil.copy(GOLDEN / "devin-subagent" / "sessions.db", db)
        conn = sqlite3.connect(db)
        mutate(conn)
        conn.commit()
        conn.close()
        sel = [db, "--host", "devin", "--session-id", "golden-devin-subagent"]
    else:
        to_mutate, sel = files_of(host, tmp_path)
        for f in to_mutate:
            write(f, mutate(read(f)))
        sel = [*sel, "--host", host]
    code, out = cli.json(*sel, "--health-all")
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir(exist_ok=True)
    md = cli.run(*sel, "--report", "-", "--no-open", "--report-format", "md", stdin=json.dumps(FINDINGS)).stdout
    cli.run(*sel, "--report", "-", "--no-open", stdin=json.dumps(FINDINGS))
    html = next((tmp_path / "tmp" / "workflow-reviewer").glob("review-*.html")).read_text(encoding="utf-8")
    return code, out, md, html


def rows(out):
    return {r["metric"]: r for r in out["coverage"]["health"]["rows"]}


def reasons(out, metric):
    return [c["name"] for c in rows(out)[metric]["checks"] if not c["passed"]]


def codex_output_renamed(rs):
    return MUTATIONS["codex"]["usage field"](rs)


def claude_output_renamed(rs):
    for r in rs:
        if r.get("type") == "assistant" and isinstance(r.get("message", {}).get("usage"), dict):
            rename(r["message"]["usage"], "output_tokens", "output_tokens_x")
    return rs


CASES = {
    # host, what is renamed -> (the number that is gone, its row)
    "codex output": ("codex", codex_output_renamed, ("tokens", "output"), "tokens"),
    "claude output": ("claude-code", claude_output_renamed, ("tokens", "output"), "tokens"),
    "devin input": ("devin", devin_usage_field, ("tokens", "fresh_input"), "tokens"),
    "devin turn marker": ("devin", devin_turn_marker, ("window", "turns_total"), "turns"),
    "cursor turn marker": ("cursor", MUTATIONS["cursor"]["turn marker"], ("window", "turns_total"), "turns"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_missing_core_field_is_null_broken_and_never_printed_as_zero(cli, tmp_path, case):
    host, mutate, (section, key), metric = CASES[case]
    code, out, md, html = run(cli, tmp_path, host, mutate)
    assert code == 2, case
    assert out[section][key] is None, (case, out[section][key])
    assert rows(out)[metric]["status"] == "broken"
    assert "not measurable" in md.lower() and "not measurable" in html.lower()
    if metric == "tokens":
        label = {"output": "Output tokens", "fresh_input": "Fresh input"}[key]
        assert re.search(rf"{label}[^\n]*not measurable", md), md
        assert not re.search(rf"{label}\W+0\b", md) and not re.search(rf"{label}</th><td>0<", html)
    else:
        assert "No turns" not in md and "No turns" not in html and "Turns 1–0" not in md


def test_the_reason_says_which_field_is_missing_from_how_many_records(cli, tmp_path):
    _, out, _, _ = run(cli, tmp_path, "codex", codex_output_renamed)
    assert any(re.fullmatch(r"output token counts missing from all \d+ usage records", r) for r in reasons(out, "tokens")), reasons(out, "tokens")


def test_a_field_the_host_never_records_is_not_checked(cli, tmp_path):
    """Codex records no cache writes, and its mapping does not declare them: cache_write stays 0 and nothing breaks."""
    code, out, _, _ = run(cli, tmp_path, "codex", lambda rs: rs)
    assert code == 0 and out["tokens"]["cache_write"] == 0 and rows(out)["tokens"]["status"] in ("ok", "one_way")

