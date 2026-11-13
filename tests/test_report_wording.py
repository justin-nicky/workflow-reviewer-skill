"""The report says things in plain words: coarse times as minutes, what a host does not record, no internal field names."""
import html
import json
import re

from fixtures.builders import cu_assistant, cu_turn_ended, cu_user, write_jsonl
from test_report import FINDINGS, findings, render, report_file


def stamped(text, clock):
    return cu_user(f"<timestamp>Wednesday, Nov 11, 2026, {clock} (UTC+4)</timestamp>\n<user_query>\n{text}\n</user_query>")


def cursor_report(cli, tmp_path, records, fmt="json", *args):
    path = write_jsonl(tmp_path / "c.jsonl", records)
    proc = render(cli, tmp_path, findings(), *args, session=path, host="cursor", fmt=fmt)
    assert proc.returncode == 0, proc.stdout
    return html.unescape(report_file(tmp_path).read_text(encoding="utf-8"))


def test_a_coarse_time_is_minutes_with_its_range_in_the_html(cli, tmp_path):
    text = cursor_report(cli, tmp_path, [stamped("a", "11:00 AM"), cu_assistant("x"), cu_turn_ended(),
                                         stamped("b", "11:10 AM"), cu_assistant("y"), cu_turn_ended()])
    assert "about 10 min (between 9–11 min)" in text and "last turn's duration unknown" in text
    assert "wall clock (coarse)" not in text and re.search(r"about \d+ s wall", text) is None


def test_the_same_minute_is_under_a_minute(cli, tmp_path):
    text = cursor_report(cli, tmp_path, [stamped("a", "11:52 AM"), cu_assistant("x"), stamped("b", "11:52 AM"), cu_assistant("y")])
    assert "under 1 min" in text and "about 0 s" not in text


def test_a_window_that_ends_at_the_next_prompt_says_so(cli, tmp_path):
    text = cursor_report(cli, tmp_path, [stamped("a", "11:00 AM"), cu_assistant("x"), stamped("b", "11:30 AM"), cu_assistant("y")],
                         "json", "--from-turn", 1, "--to-turn", 1)
    assert "about 30 min (between 29–31 min)" in text and "until the next prompt" in text


def test_the_coverage_row_says_what_the_host_does_not_record(cli, tmp_path):
    text = cursor_report(cli, tmp_path, [cu_user("go"), cu_assistant("ok"), cu_turn_ended()])
    assert "Cursor doesn't record: tokens, tool timing, tool results, message ids" in text
    assert "has_" not in text and "not measurable:" not in text


def test_the_markdown_report_has_the_same_plain_row(cli, tmp_path):
    path = write_jsonl(tmp_path / "c.jsonl", [cu_user("go"), cu_assistant("ok"), cu_turn_ended()])
    proc = render(cli, tmp_path, findings(), "--report-format", "md", session=path, host="cursor", fmt=None)
    assert proc.returncode == 0
    assert "Cursor doesn't record: tokens, tool timing, tool results, message ids" in proc.stdout and "has_" not in proc.stdout


def test_the_metrics_list_uses_plain_labels_too(cli, tmp_path):
    path = write_jsonl(tmp_path / "c.jsonl", [cu_user("go"), cu_assistant("ok")])
    _, out = cli.json(path, "--host", "cursor")
    assert out["coverage"]["not_measurable"] == ["tokens", "tool timing", "tool results", "message ids"]


# ---- the script renders the "other installed skills" row itself ----------------------------------------------

def test_the_agent_may_not_write_an_other_installed_skills_row(cli, tmp_path):
    for name in ("other installed skills", "Other Installed Skills", "  OTHER installed skills "):
        data = findings()
        data["skills"] = data["skills"] + [{"name": name, "verdict": "none", "evidence": "not used"}]
        proc = render(cli, tmp_path, data)
        out = json.loads(proc.stdout)
        assert proc.returncode == 7 and any("skills[" in e and "other installed skills" in e.lower() for e in out["errors"]), out
