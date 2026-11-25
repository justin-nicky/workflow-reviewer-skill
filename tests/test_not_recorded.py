"""A number the host never records is "not recorded by <host>": null in JSON, never 0, and never a failure.

Two cases: a token field the mapping does not declare (Devin and Codex leave cache writes out), and tool time from a
declared timing field that no tool result in the window carries."""
import re
import shutil
import sqlite3

from fixtures import devin as dv
from test_golden import GOLDEN, one
from test_health_mutations import edit_messages
from test_missing_fields import run
from test_devin import build, head

TIMING_FIELD = "metadata.extensions.chisel/tool_call_timing.started_at"


def unchanged(conn):
    pass


def drop_timing(conn):
    edit_messages(conn, lambda m: m["metadata"]["extensions"].pop("chisel/tool_call_timing"), lambda m: m["role"] == "tool")


def rows_of(out):
    return out["turns"] + out["actors"]


def legend(html):
    m = re.search(r'<ul class="legend">(.*?)</ul>', html)
    return m.group(1) if m else ""


def health_row(out, metric):
    """The metric's health row; rows that need no attention are left out unless --health-all is given."""
    return next((r for r in out["coverage"]["health"]["rows"] if r["metric"] == metric), {"status": "ok", "core": False, "checks": []})


# ---- a token field the mapping does not declare ----------------------------------------------------------------------

def test_devin_cache_writes_are_not_recorded_in_every_breakdown(cli, tmp_path):
    code, out, md, html = run(cli, tmp_path, "devin", unchanged)
    t = out["tokens"]
    assert code == 0
    assert t["cache_write"] is None and t["not_recorded"] == ["cache_write"]
    assert all(r["tokens"]["cache_write"] is None for r in rows_of(out))
    assert all(isinstance(r["tokens"][k], int) for r in rows_of(out) for k in ("fresh_input", "output", "cache_read", "api_calls"))
    assert isinstance(t["context_at_invocation"], int)
    row = health_row(out, "tokens")                    # expected, not a failure
    assert row["status"] != "broken" and not any(c.get("field") == "cache_write" for c in row["checks"])
    assert re.search(r"Cache read / write: [^\n/]+ / not recorded by Devin", md), md
    assert "None" not in md and "cache write" not in md.lower().split("token composition", 1)[-1].split("\n", 1)[0]
    assert "Cache read / write</th><td>" in html and re.search(r"Cache read / write</th><td>[^<]+ / not recorded by Devin<", html)
    assert 'class="seg-write"' not in html and "Cache write" not in legend(html) and "Cache read" in legend(html)


def test_the_other_bucket_leaves_the_unrecorded_field_out_too(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.assistant("ok", tokens=dv.metrics(10, 100, 1)))
        for n in range(7):
            c.add(dv.user(f"prompt {n}"), dv.assistant("ok", tokens=dv.metrics(10 + n, 100, 1)))
    db = build(cli, tmp_path, fill)
    code, out = cli.json(db, "--host", "devin")
    assert code == 0 and any(r.get("turn") == "other" for r in out["turns"]), [r.get("turn") for r in out["turns"]]
    assert all(r["tokens"]["cache_write"] is None for r in rows_of(out))
    md = cli.run(db, "--host", "devin", fmt="md").stdout
    assert "cache_write: not recorded by Devin" in md and '"cache_write":"not recorded by Devin"' in md
    assert "None" not in md and '"cache_write":0' not in md


def test_codex_cache_writes_are_not_recorded(cli):
    code, out = cli.json(*sorted(GOLDEN.glob("codex-desktop/rollout-*.jsonl")), "--host", "codex")
    assert code == 0 and out["tokens"]["cache_write"] is None and out["tokens"]["not_recorded"] == ["cache_write"]
    assert all(r["tokens"]["cache_write"] is None for r in rows_of(out))
    assert isinstance(out["tokens"]["cache_read"], int)


def test_a_host_that_records_every_field_is_unchanged(cli):
    code, out = cli.json(one("claude-code/*.jsonl").with_suffix(""), "--host", "claude-code")
    assert code == 0 and "not_recorded" not in out["tokens"] and isinstance(out["tokens"]["cache_write"], int)
    assert "not_recorded" not in out["time"] and isinstance(out["time"]["tool_s"], int)


# ---- tool time from a timing field no result carries ------------------------------------------------------------------

def test_the_unmutated_devin_golden_keeps_its_tool_time(cli, tmp_path):
    code, out, md, _ = run(cli, tmp_path, "devin", unchanged)
    assert code == 0 and out["time"]["tool_s"] == 5 and "not_recorded" not in out["time"]
    assert next(a for a in out["actors"] if a["actor"] == "main")["tool_s"] == 5
    assert health_row(out, "time")["status"] != "warn" or not any(c.get("field") == TIMING_FIELD for c in health_row(out, "time")["checks"])
    assert "5 s in tools" in md


def test_devin_tool_time_is_not_recorded_when_no_result_carries_the_timing(cli, tmp_path):
    for sub in ("base", "cut"):
        (tmp_path / sub).mkdir()
    base_code, _, _, _ = run(cli, tmp_path / "base", "devin", unchanged)
    code, out, md, html = run(cli, tmp_path / "cut", "devin", drop_timing)
    assert code == base_code == 0
    assert out["time"]["tool_s"] is None and out["time"]["not_recorded"] == ["tool_s"]
    assert all(a["tool_s"] is None for a in out["actors"])
    row = health_row(out, "time")
    failed = [c for c in row["checks"] if not c["passed"]]
    assert row["status"] == "warn" and not row["core"]
    assert any(c["layer"] == "presence" and c.get("field") == TIMING_FIELD and TIMING_FIELD in c["name"] for c in failed), failed
    assert not any(r["status"] == "broken" for r in out["coverage"]["health"]["rows"])
    assert re.search(r"Time: [^\n]*tool time not recorded by Devin", md), md
    assert "0 s in tools" not in md and "tool time not recorded by Devin" in html
    assert TIMING_FIELD in md and "None" not in md


def test_a_window_without_tool_results_has_a_real_zero(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.assistant("ok", tokens=dv.metrics(10, 100, 1)))
    code, out = cli.json(build(cli, tmp_path, fill), "--host", "devin")
    assert code == 0 and out["time"]["tool_s"] == 0 and "not_recorded" not in out["time"]
    assert not any(c.get("field") == TIMING_FIELD for c in health_row(out, "time")["checks"])


def test_the_metrics_markdown_says_not_recorded(cli, tmp_path):
    db = tmp_path / "sessions.db"
    shutil.copy(GOLDEN / "devin-subagent" / "sessions.db", db)
    conn = sqlite3.connect(db)
    drop_timing(conn)
    conn.commit()
    conn.close()
    md = cli.run(db, "--host", "devin", "--session-id", "golden-devin-subagent", fmt="md").stdout
    assert "- tool_s: not recorded by Devin" in md and '"tool_s":"not recorded by Devin"' in md
    assert "- cache_write: not recorded by Devin" in md and "None" not in md
