"""--report: the agent sends judgments only; the script computes every number and renders the report."""
import copy
import html.parser
import importlib.util
import io
import json
import os
import re
import stat
from pathlib import Path

import pytest

from conftest import ROOT, SCRIPT

GOLDEN = ROOT / "tests" / "fixtures" / "golden"
FINDINGS = json.loads((ROOT / "tests" / "fixtures" / "findings" / "golden.json").read_text())
CLAUDE = next(iter(sorted((GOLDEN / "claude-code").glob("*.jsonl")))).with_suffix("")
CURSOR = next(iter(sorted((GOLDEN / "cursor-chat1").glob("*/"))))

spec = importlib.util.spec_from_file_location("tm_report", SCRIPT)
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)


def findings(**change):
    data = copy.deepcopy(FINDINGS)
    for key, value in change.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def render(cli, tmp_path, data=None, *args, session=CLAUDE, host="claude-code", fmt="json"):
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return cli.run(session, "--host", host, "--report", "-", "--no-open", *args, stdin=json.dumps(data if data is not None else FINDINGS), fmt=fmt)


def report_file(tmp_path):
    files = list((tmp_path / "tmp" / "workflow-reviewer").glob("review-*.html"))
    assert len(files) == 1
    return files[0]


# ---- validation: exit 7 with a list of errors ------------------------------------------------------------------------

def bad(mutate):
    data = findings()
    mutate(data)
    return data


def six_actions(data):
    data["actions"] = [copy.deepcopy(data["actions"][0]) for _ in range(6)]


def impact_goes_up(data):
    data["actions"][1]["impact"] = "high"
    data["actions"][0]["impact"] = "medium"
    data["actions"][2]["impact"] = "high"


CASES = {
    "unknown top-level key": (bad(lambda d: d.update(metrics={"api_calls": 1})), "metrics"),
    "unknown action key": (bad(lambda d: d["actions"][0].update(numbers=1)), "actions[0]"),
    "more than 5 actions": (bad(six_actions), "actions"),
    "impact goes up": (bad(impact_goes_up), "impact"),
    "closing question without a gap": (bad(lambda d: d.update(skills=[s for s in d["skills"] if s["verdict"] != "gap"])), "closing_question"),
    "bad impact enum": (bad(lambda d: d["actions"][0].update(impact="huge")), "actions[0].impact"),
    "bad verdict enum": (bad(lambda d: d["skills"][0].update(verdict="love")), "skills[0].verdict"),
    "title too long": (bad(lambda d: d.update(title="x" * 81)), "title"),
    "bottom line too long": (bad(lambda d: d.update(bottom_line="x" * 301)), "bottom_line"),
    "more than 3 evidence items": (bad(lambda d: d["actions"][0]["why"].update(evidence=["a", "b", "c", "d"])), "evidence"),
    "missing key": (bad(lambda d: d["actions"][0]["what"].pop("watch_for")), "watch_for"),
    "wrong schema version": (bad(lambda d: d.update(schema_version=2)), "schema_version"),
    "metric note for an unknown row": (bad(lambda d: d.update(metric_notes={"bogus": "x"})), "metric_notes"),
    "wrong type": (bad(lambda d: d.update(bottom_line=5)), "bottom_line"),
}


@pytest.mark.parametrize("name", CASES)
def test_invalid_findings_exit_7_with_errors_the_agent_can_fix(cli, tmp_path, name):
    data, needle = CASES[name]
    proc = render(cli, tmp_path, data)
    out = json.loads(proc.stdout)
    assert proc.returncode == 7 and out["errors"] and any(needle in e for e in out["errors"]), out
    assert not (tmp_path / "tmp" / "workflow-reviewer").exists()          # nothing is written


def test_every_error_is_listed_at_once(cli, tmp_path):
    data = findings(title="x" * 99)
    data["actions"][0]["impact"] = "huge"
    out = json.loads(render(cli, tmp_path, data).stdout)
    assert len(out["errors"]) >= 2


def test_not_json_is_exit_7(cli, tmp_path):
    cli.env_extra["TMPDIR"] = str(tmp_path)
    proc = cli.run(CLAUDE, "--host", "claude-code", "--report", "-", "--no-open", stdin="{not json")
    assert proc.returncode == 7 and "JSON" in json.loads(proc.stdout)["errors"][0]


def test_no_actions_and_no_skills_are_valid(cli, tmp_path):
    proc = render(cli, tmp_path, findings(actions=[], skills=[], closing_question=None, metric_notes=None))
    out = json.loads(proc.stdout)
    assert proc.returncode == 0 and out["top_action"] is None
    assert "No changes recommended" in report_file(tmp_path).read_text()


def test_report_and_mapping_cannot_both_read_stdin(cli, tmp_path):
    proc = cli.run(CLAUDE, "--host", "claude-code", "--report", "-", "--mapping", "-", stdin="{}")
    assert proc.returncode == 2


# ---- output ---------------------------------------------------------------------------------------------------------

def test_stdout_has_the_bottom_line_the_top_action_and_the_path(cli, tmp_path):
    proc = render(cli, tmp_path)
    out = json.loads(proc.stdout)
    assert proc.returncode == 0 and set(out) == {"bottom_line", "top_action", "report_path"}
    assert out["top_action"] == "Trim `CURRENT_STATE.md`" and Path(out["report_path"]) == report_file(tmp_path)
    text = render_text(cli, tmp_path)
    assert text.count("\n") == 3 and "Report: " in text


def render_text(cli, tmp_path):
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp2")
    (tmp_path / "tmp2").mkdir(exist_ok=True)
    return cli.run(CLAUDE, "--host", "claude-code", "--report", "-", "--no-open", stdin=json.dumps(FINDINGS), fmt="md").stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_file_is_private(cli, tmp_path):
    render(cli, tmp_path)
    path = report_file(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert re.fullmatch(r"review-\d{8}T\d{6}Z(-\d+)?\.html", path.name)


def test_two_reports_in_one_second_do_not_overwrite(cli, tmp_path):
    for _ in range(2):
        render(cli, tmp_path)
    assert len(list((tmp_path / "tmp" / "workflow-reviewer").glob("review-*.html"))) == 2


class Tags(html.parser.HTMLParser):
    ALLOWED = {"html", "head", "meta", "title", "style", "body", "main", "header", "h1", "p", "strong", "code", "figure",
               "div", "span", "ul", "li", "figcaption", "section", "h2", "h3", "table", "tbody", "tr", "th", "td", "ol",
               "details", "summary", "aside", "i", "b", "ul"}

    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.opened = [], [], 0

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [(tag, k, v) for k, v in attrs]
        if tag == "details" and any(k == "open" for k, _ in attrs):
            self.opened += 1


def parsed(text):
    parser = Tags()
    parser.feed(text)
    return parser


def test_the_page_is_self_contained(cli, tmp_path):
    render(cli, tmp_path)
    text = report_file(tmp_path).read_text(encoding="utf-8")
    assert "http://" not in text and "https://" not in text and "//" not in re.sub(r"/\*.*?\*/", "", text, flags=re.S).replace("<!--", "")
    assert '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">' in text
    p = parsed(text)
    assert "script" not in p.tags and "link" not in p.tags and "img" not in p.tags and set(p.tags) <= Tags.ALLOWED
    assert not [a for a in p.attrs if a[1].startswith("on") or a[1] in ("href", "src")]
    assert p.opened == 0 and p.tags.count("details") == 2 * len(FINDINGS["actions"])
    assert "sample-note" not in text and "anatomy-note" not in text


def test_the_page_follows_the_sample_order_and_meaning(cli, tmp_path):
    render(cli, tmp_path)
    text = report_file(tmp_path).read_text(encoding="utf-8")
    order = ["<h1>", 'class="scope"', 'class="bottom-line"', 'class="bar"', "<h2 id=\"h-metrics\">", "Do these, ranked by impact",
             "Skills audit", 'class="closing"']
    positions = [text.index(o) for o in order]
    assert positions == sorted(positions)
    assert text.count('class="tag warn"') == 3     # high risk, low confidence, high effort
    assert "G1: state-doc pruning" in text and '<code>CURRENT_STATE.md</code>' in text
    assert "Claude Code session" in text and re.search(r"reviewed on \d{1,2} \w{3} \d{4}", text)
    assert "prefers-color-scheme: dark" in text and "@media print" in text


# ---- escaping and redaction -----------------------------------------------------------------------------------------

HOSTILE = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "</style><style>*{display:none}</style>",
           "' onmouseover='alert(1)", "<details open>", "javascript:alert(1)"]


@pytest.mark.parametrize("payload", HOSTILE)
def test_hostile_text_comes_out_inert(cli, tmp_path, payload):
    data = findings(title=payload, bottom_line=payload)
    data["actions"][0]["title"] = payload
    data["actions"][0]["why"]["cause"] = payload
    data["actions"][0]["why"]["evidence"] = [payload]
    data["skills"][0]["name"] = payload
    data["skills"][0]["evidence"] = payload
    data["metric_notes"] = {"verify": payload}
    assert render(cli, tmp_path, data).returncode == 0
    text = report_file(tmp_path).read_text(encoding="utf-8")
    p = parsed(text)
    assert p.tags.count("style") == 1 and "script" not in p.tags and "img" not in p.tags and p.opened == 0
    assert set(p.tags) <= Tags.ALLOWED and not [a for a in p.attrs if a[1].startswith("on")]
    assert payload not in text or payload == "javascript:alert(1)"
    assert "&lt;" in text or "&#x27;" in text or payload == "javascript:alert(1)"


def test_only_backticks_format_text(cli, tmp_path):
    data = findings(bottom_line="Use `a<b>` and _not italic_ and [link](x).")
    render(cli, tmp_path, data)
    text = report_file(tmp_path).read_text(encoding="utf-8")
    assert "<code>a&lt;b&gt;</code>" in text and "_not italic_" in text and "<a " not in text


SECRET = "API_KEY=sk_" "live_abcdefghijklmnop0123456789"
TOKEN = "Zx9Qw3Er7Ty1Ui5Op2As6Df0Gh4Jk8Lm"


def planted():
    data = findings(bottom_line=f"Leaked {SECRET} here")
    data["actions"][0]["why"]["evidence"] = [f"token {TOKEN} seen"]
    data["skills"][0]["evidence"] = f"/Users/someone/private/file with {SECRET}"
    return data


def test_secrets_in_the_findings_are_redacted_in_html(cli, tmp_path):
    proc = render(cli, tmp_path, planted())
    text = report_file(tmp_path).read_text(encoding="utf-8")
    for leak in (SECRET, "sk_" "live_abcdefghijklmnop0123456789", TOKEN, "/Users/someone"):
        assert leak not in text and leak not in proc.stdout


def test_secrets_in_the_findings_are_redacted_in_markdown(cli, tmp_path):
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    proc = cli.run(CLAUDE, "--host", "claude-code", "--report", "-", "--report-format", "md", stdin=json.dumps(planted()))
    for leak in (SECRET, "sk_" "live_abcdefghijklmnop0123456789", TOKEN, "/Users/someone"):
        assert leak not in proc.stdout
    assert proc.returncode == 0


# ---- markdown ---------------------------------------------------------------------------------------------------------

def test_markdown_report_writes_no_file_and_stays_short(cli, tmp_path):
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    proc = cli.run(CLAUDE, "--host", "claude-code", "--report", "-", "--report-format", "md", stdin=json.dumps(FINDINGS))
    assert proc.returncode == 0 and not (tmp_path / "tmp").exists()
    lines = proc.stdout.rstrip("\n").split("\n")
    assert len(lines) <= 80 and lines[0].startswith("# ") and "Do these, ranked by impact" in proc.stdout
    assert "G1: state-doc pruning" in proc.stdout and "Trim `CURRENT_STATE.md`" in proc.stdout


def test_markdown_report_at_the_limits_is_still_80_lines_or_fewer(cli, tmp_path):
    data = findings()
    data["actions"] = [copy.deepcopy(data["actions"][0]) for _ in range(5)]
    for a in data["actions"]:
        a["why"]["evidence"] = ["one", "two", "three"]
    data["skills"] = [{"name": f"skill-{i}", "verdict": "gap" if i == 0 else "keep", "evidence": "e"} for i in range(20)]
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    proc = cli.run(CLAUDE, "--host", "claude-code", "--report", "-", "--report-format", "md", stdin=json.dumps(data))
    assert proc.returncode == 0 and len(proc.stdout.rstrip("\n").split("\n")) <= 80


# ---- the numbers come from the metrics run ----------------------------------------------------------------------------

def test_the_reports_numbers_equal_the_metrics_run(cli, tmp_path):
    _, metrics = cli.json(CLAUDE, "--host", "claude-code")
    render(cli, tmp_path)
    text = report_file(tmp_path).read_text(encoding="utf-8")
    t = metrics["tokens"]
    row = lambda label: re.search(rf"<th scope=\"row\">{label}</th><td>(.*?)</td>", text).group(1)
    assert row("API calls") == str(t["api_calls"])
    assert row("Output tokens") == tm.fmt_tokens(t["output"])
    assert row("Cache read / write") == f"{tm.fmt_tokens(t['cache_read'])} / {tm.fmt_tokens(t['cache_write'])}"
    assert row("Fresh input") == tm.fmt_tokens(t["fresh_input"])
    assert row("Verify runs").startswith(str(metrics["verify"]["total"]))
    assert row("Tool calls").startswith(str(metrics["tools"]["total"]))
    total = t["cache_read"] + t["cache_write"] + t["output"] + t["fresh_input"]
    assert f"{tm.fmt_tokens(total)} tokens" in text
    assert f"Turns 1–{metrics['window']['turns_total']} " in text


def test_a_host_without_tokens_has_no_bar_and_shows_a_coverage_row(cli, tmp_path):
    render(cli, tmp_path, session=CURSOR, host="cursor")
    text = report_file(tmp_path).read_text(encoding="utf-8")
    assert 'class="bar"' not in text and "Coverage" in text and "not measurable" in text.lower()


def test_a_clean_run_has_no_coverage_row(cli, tmp_path):
    render(cli, tmp_path)
    assert "<th scope=\"row\">Coverage</th>" not in report_file(tmp_path).read_text(encoding="utf-8")


def test_the_golden_report_html_is_stable(cli, tmp_path):
    render(cli, tmp_path)
    text = report_file(tmp_path).read_text(encoding="utf-8")
    text = re.sub(r"reviewed on \d{1,2} \w{3} \d{4}", "reviewed on DATE", text)
    assert text == (GOLDEN / "claude-code-report.html").read_text(encoding="utf-8")


# ---- opening ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env, platform, expect", [
    ({"DISPLAY": ":0"}, "linux", True),
    ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", True),
    ({}, "linux", False),
    ({"DISPLAY": ":0", "SSH_CONNECTION": "1 2 3 4"}, "linux", False),
    ({"DISPLAY": ":0", "SSH_TTY": "/dev/pts/1"}, "linux", False),
    ({"DISPLAY": ":0", "CI": "true"}, "linux", False),
    ({}, "darwin", True),
    ({"SSH_CLIENT": "x"}, "darwin", False),
    ({"GITHUB_ACTIONS": "true"}, "darwin", False),
    ({}, "win32", True),
])
def test_when_the_report_may_be_opened(env, platform, expect):
    assert tm.can_open(env, platform) is expect


def run_in_process(monkeypatch, tmp_path, repo, *args, env=None):
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tm.tempfile, "tempdir", None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(FINDINGS)))
    monkeypatch.chdir(repo)
    return tm.main([str(CLAUDE), "--host", "claude-code", "--report", "-", "--format", "json", *args])


def test_the_opener_is_called_once_when_allowed(cli, tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(tm, "open_file", lambda path: calls.append(path))
    monkeypatch.setattr(tm.sys, "platform", "darwin")
    assert run_in_process(monkeypatch, tmp_path, cli.repo) == 0
    assert len(calls) == 1 and calls[0].endswith(".html") and json.loads(capsys.readouterr().out)["report_path"] == calls[0]


@pytest.mark.parametrize("args, env", [(["--no-open"], {}), ([], {"SSH_CONNECTION": "1"}), ([], {"CI": "1"})])
def test_the_opener_is_not_called_when_not_allowed(cli, tmp_path, monkeypatch, capsys, args, env):
    calls = []
    monkeypatch.setattr(tm, "open_file", lambda path: calls.append(path))
    monkeypatch.setattr(tm.sys, "platform", "darwin")
    assert run_in_process(monkeypatch, tmp_path, cli.repo, *args, env=env) == 0
    assert calls == [] and json.loads(capsys.readouterr().out)["report_path"]


def test_a_failing_opener_is_not_an_error(cli, tmp_path, monkeypatch, capsys):
    def boom(path):
        raise OSError("no opener")
    monkeypatch.setattr(tm, "open_file", boom)
    monkeypatch.setattr(tm.sys, "platform", "darwin")
    assert run_in_process(monkeypatch, tmp_path, cli.repo) == 0


def test_the_opener_never_uses_a_shell(monkeypatch):
    seen = {}
    monkeypatch.setattr(tm.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    monkeypatch.setattr(tm.sys, "platform", "linux")
    tm.open_file("/tmp/x.html")
    assert seen["cmd"] == ["xdg-open", "/tmp/x.html"] and not seen["kw"].get("shell")
    monkeypatch.setattr(tm.sys, "platform", "darwin")
    tm.open_file("/tmp/x.html")
    assert seen["cmd"] == ["open", "/tmp/x.html"]


def test_the_script_still_imports_no_network_module():
    source = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+(socket|urllib|http|requests|ssl|ftplib|smtplib)\b", source, re.M)


# ---- step 3b: visual-check fixes ---------------------------------------------------------------------------------------

def test_the_disclosure_marker_points_down_when_open(cli, tmp_path):
    render(cli, tmp_path)
    css = report_file(tmp_path).read_text(encoding="utf-8")
    closed = re.search(r"summary::before\{content:\"(\\[0-9A-F]+)\"", css).group(1)
    opened = re.search(r"details\[open\]>summary::before\{([^}]*)\}", css).group(1)
    assert closed == "\\25B8" and 'content:"\\25BE"' in opened and "rotate" not in css


def test_applies_to_is_a_plain_tag_after_confidence(cli, tmp_path):
    render(cli, tmp_path)
    text = report_file(tmp_path).read_text(encoding="utf-8")
    assert text.count('<li class="tag">All hosts</li>') == 1 and text.count('<li class="tag">Claude Code</li>') == 1
    first = text[text.index('<li class="tag impact">High impact'):]
    first = first[:first.index("</ul>")]
    assert first.index("confidence</li>") < first.index("All hosts</li>")
    _, md = None, cli.run(CLAUDE, "--host", "claude-code", "--report", "-", "--report-format", "md", stdin=json.dumps(FINDINGS)).stdout
    assert "medium confidence, All hosts" in md and "low confidence, Claude Code" in md


@pytest.mark.parametrize("value", ["mars", "All hosts", "", 5, None])
def test_applies_to_must_be_all_hosts_or_a_known_host(cli, tmp_path, value):
    data = findings()
    data["actions"][0]["applies_to"] = value
    proc = render(cli, tmp_path, data)
    assert proc.returncode == 7 and any("actions[0].applies_to" in e for e in json.loads(proc.stdout)["errors"])


@pytest.mark.parametrize("value", ["all hosts", "claude-code", "codex", "cursor", "devin", "kiro"])
def test_applies_to_accepts_all_hosts_and_known_hosts(value):
    data = findings()
    data["actions"][0]["applies_to"] = value
    assert tm.validate_findings(data) == []


def test_at_most_one_bold_span_and_only_in_the_bottom_line(cli, tmp_path):
    ok = findings(bottom_line="A **bold** claim and `code`.")
    render(cli, tmp_path, ok)
    text = report_file(tmp_path).read_text(encoding="utf-8")
    assert '<p class="bottom-line">A <strong>bold</strong> claim and <code>code</code>.</p>' in text
    assert tm.validate_findings(findings(bottom_line="**one** and **two**")) and \
        "bottom_line" in tm.validate_findings(findings(bottom_line="**one** and **two**"))[0]
    for field in ("title",):
        assert any(field in e for e in tm.validate_findings(findings(title="a **b** c")))
    data = findings()
    data["actions"][0]["why"]["cause"] = "a **b** c"
    data["skills"][0]["evidence"] = "x **y** z"
    errors = tm.validate_findings(data)
    assert any("actions[0].why.cause" in e for e in errors) and any("skills[0].evidence" in e for e in errors)


def test_evidence_items_may_be_240_characters(cli, tmp_path):
    data = findings()
    data["actions"][0]["why"]["evidence"] = ["e" * 240]
    assert tm.validate_findings(data) == []
    data["actions"][0]["why"]["evidence"] = ["e" * 241]
    assert any("evidence[0]" in e and "240" in e for e in tm.validate_findings(data))


@pytest.mark.parametrize("seconds, text", [(0, "0 s"), (166, "166 s"), (599, "599 s"), (600, "10 min"), (3599, "60 min"),
                                           (3600, "about 1.0 h"), (24840, "about 6.9 h")])
def test_time_units(seconds, text):
    assert tm.fmt_secs(seconds) == text


def test_all_three_parts_of_the_time_row_use_the_same_rule(cli, tmp_path):
    out = {"time": {"active_s": 166, "tool_s": 700, "waiting_s": 24840}}
    full = {"tokens": {"not_measurable": "x"}, "tools": {"total": 0, "by_category": {}, "errors": 0}, "time": out["time"],
            "verify": {"total": 0, "before_last_edit": 0}, "actors": [], "interruptions": {"count": 0}}
    row = next(v for rid, _, v in tm.metric_rows(full) if rid == "time")
    assert row == "166 s active, 12 min in tools, about 6.9 h waiting on you"
    full["time"] = {"wall_s": 24840, "wall_range_s": [24780, 24900], "precision": tm.COARSE_LABEL}      # a coarse time is minutes and a range
    assert next(v for rid, _, v in tm.metric_rows(full) if rid == "time") == \
        "about 414 min (between 413–415 min), last turn's duration unknown"


def install_skills(cli, names):
    for name in names:
        folder = cli.home / ".claude" / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\ndescription: d\n---\nbody\n")


def test_other_installed_skills_row_counts_what_the_findings_do_not_mention(cli, tmp_path):
    install_skills(cli, ["hello", "code-review", "extra-a", "extra-b", "extra-c"])
    _, metrics = cli.json(CLAUDE, "--host", "claude-code")
    assert metrics["skills"]["inventory_total"] == 5 and "_inventory_names" not in metrics["skills"]
    render(cli, tmp_path)                                  # the findings mention hello and code-review
    text = report_file(tmp_path).read_text(encoding="utf-8")
    assert '<span class="skill-name muted">Other installed skills</span>' in text and "Not exercised in this task (3 skills)" in text
    assert text.index("Not exercised") > text.index("G1: state-doc pruning")
    md = cli.run(CLAUDE, "--host", "claude-code", "--report", "-", "--report-format", "md", stdin=json.dumps(FINDINGS)).stdout
    assert "- Other installed skills: No verdict. Not exercised in this task (3 skills)" in md


def test_the_other_row_comes_from_the_metrics_run_not_the_agent(cli, tmp_path):
    install_skills(cli, ["hello", "code-review", "x1"])
    render(cli, tmp_path)
    assert "(1 skill)" in report_file(tmp_path).read_text(encoding="utf-8")


def test_the_other_row_is_left_out_when_zero_or_unknown(cli, tmp_path):
    install_skills(cli, ["hello", "code-review", "pr-review", "tdd"])      # all mentioned (or none left over)
    render(cli, tmp_path)
    assert "Other installed skills" not in report_file(tmp_path).read_text(encoding="utf-8")
    assert tm.other_skill_count(None, FINDINGS) is None and tm.other_skill_count(["a"], {"skills": [{"name": "a"}]}) is None
    assert tm.other_skill_count(["a", "b"], {"skills": [{"name": "a"}]}) == 1


def test_installed_skills_in_the_inventory_beyond_the_ten_shown_are_counted(cli, tmp_path):
    install_skills(cli, [f"s{i:02d}" for i in range(14)])
    render(cli, tmp_path)
    assert "Not exercised in this task (14 skills)" in report_file(tmp_path).read_text(encoding="utf-8")
