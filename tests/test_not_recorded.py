"""A number the host never records is "not recorded by <host>": null in JSON, never 0, and never a failure.

Two cases: a token field the mapping does not declare (Devin and Codex leave cache writes out), and tool time from a
declared timing field that no tool result in the window carries."""
import re

from fixtures import devin as dv
from test_golden import GOLDEN, one
from test_missing_fields import run
from test_devin import build, head

TIMING_FIELD = "metadata.extensions.chisel/tool_call_timing.started_at"


def unchanged(conn):
    pass


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




def test_a_window_without_tool_results_has_a_real_zero(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.assistant("ok", tokens=dv.metrics(10, 100, 1)))
    code, out = cli.json(build(cli, tmp_path, fill), "--host", "devin")
    assert code == 0 and out["time"]["tool_s"] == 0 and "not_recorded" not in out["time"]
    assert not any(c.get("field") == TIMING_FIELD for c in health_row(out, "time")["checks"])
