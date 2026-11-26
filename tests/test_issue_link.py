"""The prefilled GitHub issue link: allowlisted diagnostics only, built offline, opened only by the user."""
import html as htmllib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import SCRIPT
from test_golden import GOLDEN, one
from test_missing_fields import codex_output_renamed, run as run_mutated
from test_report import FINDINGS

import importlib.util

_spec = importlib.util.spec_from_file_location("tm_issue", SCRIPT)
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

ISSUES = "https://github.com/justin-nicky/workflow-reviewer-skill/issues/new"
ALLOWLIST = {"skill_version", "exit_code", "host", "adapter_label", "adapter_kind", "tested_range", "transcript_version",
             "version_position", "health_summary", "health_rows", "not_recorded", "drift_coverage_pct", "unseen_types_count",
             "unseen_types", "os", "python"}
LINK_LINE = re.compile(r"^Report this problem: (\S+)$", re.M)
FOOTER = re.compile(r'<a [^>]*href="([^"]+)"[^>]*>Report a problem with this review</a>')


def decode(url):
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ISSUES
    q = parse_qs(parts.query)
    assert set(q) == {"title", "body"}
    return q["title"][0], q["body"][0]


def block_of(body):
    return re.search(r"```\n(.*?)\n```", body, re.S).group(1)


def body_keys(body):
    return [ln.split(":", 1)[0] for ln in block_of(body).split("\n") if ln and not ln.startswith(" ")]


def footer_href(html):
    m = FOOTER.search(html)
    assert m, "footer link missing"
    return htmllib.unescape(m.group(1))


def report_html(cli, tmp_path, *sel):
    out_dir = tmp_path / "tmp"
    out_dir.mkdir(exist_ok=True)
    cli.env_extra["TMPDIR"] = str(out_dir)
    for old in (out_dir / "workflow-reviewer").glob("review-*.html") if (out_dir / "workflow-reviewer").is_dir() else []:
        old.unlink()
    cli.run(*sel, "--report", "-", "--no-open", stdin=json.dumps(FINDINGS))
    return next((out_dir / "workflow-reviewer").glob("review-*.html")).read_text(encoding="utf-8")


# ---- leak test: sentinels planted in a copy of each host's golden ------------------------------------------------------

SENTINEL_WORDS = ("sentinel", "deadbeef")
TEXT_PLANTS = [  # (kind, old, new): applied to every text of the copy, in this order
    ("prompt", "lorem ipsum", "sentinelprompt ipsum"),
    ("command", "python3 -m pytest -q", "python3 -m pytest -q sentinelcmd.py"),
    ("file path", "~/project/file", "~/project/sentinelfile"),
    ("home path", "~/project", "/Users/sentinelhome/project"),
    ("session id", "00000000-0000-4000-8000-", "deadbeef-5e55-4000-8000-"),
]
MODELS = {"claude-code": "claude-sonnet-5-5", "codex": "gpt-6-luna", "cline": "cline-free/nemotron-3.5-super-vl",
          "devin": "swe-2-flash-medium"}
SESSION_NAMES = {"cline": ("replica_session", "sentinelsession"), "devin": ("golden-devin-subagent", "sentinel-devin-session")}


def plants(host):
    extra = [("model name", MODELS[host], "sentinelmodel-x")] if host in MODELS else []
    if host in SESSION_NAMES:
        extra.append(("session id", *SESSION_NAMES[host]))
    return TEXT_PLANTS + extra


def plant_text(text, host, counts):
    for kind, old, new in plants(host):
        counts[kind] = counts.get(kind, 0) + text.count(old)
        text = text.replace(old, new)
    return text


def plant_tree(src, dst, host, counts):
    """Copy a golden folder, planting sentinels in every file's text and in file and folder names."""
    shutil.copytree(src, dst)
    for root, dirs, files in os.walk(dst, topdown=False):
        for name in files:
            p = Path(root) / name
            text = plant_text(p.read_text(encoding="utf-8"), host, counts)
            with open(p, "w", encoding="utf-8", newline="\n") as f:       # Path.write_text has no newline on Python 3.9
                f.write(text)
            new = plant_text(name, host, {})
            if new != name:
                p.rename(p.with_name(new))
        for name in dirs:
            new = plant_text(name, host, {})
            if new != name:
                (Path(root) / name).rename(Path(root) / new)
    return dst


def plant_devin(tmp_path, counts):
    db = tmp_path / "sessions.db"
    shutil.copy(GOLDEN / "devin-subagent" / "sessions.db", db)
    conn = sqlite3.connect(db)
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'").fetchall():
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})") if c[2].upper() in ("TEXT", "") or "CHAR" in c[2].upper()]
        for col in cols:
            for kind, old, new in plants("devin"):
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?", (f"%{old}%",)).fetchone()[0]
                counts[kind] = counts.get(kind, 0) + n
                conn.execute(f"UPDATE {table} SET {col} = replace({col}, ?, ?)", (old, new))
    conn.commit()
    conn.close()
    return [db, "--host", "devin", "--session-id", SESSION_NAMES["devin"][1]]


def planted(host, tmp_path, counts):
    if host == "devin":
        return plant_devin(tmp_path, counts)
    if host == "claude-code":
        folder = plant_tree(GOLDEN / "claude-code", tmp_path / "cc", host, counts)
        return [next(folder.glob("*.jsonl")).with_suffix(""), "--host", "claude-code"]
    if host == "codex":
        folder = plant_tree(GOLDEN / "codex-desktop", tmp_path / "cx", host, counts)
        return [*sorted(folder.glob("rollout-*.jsonl")), "--host", "codex"]
    if host == "cursor":
        folder = plant_tree(GOLDEN / "cursor-chat1", tmp_path / "cu", host, counts)
        return [next(p for p in folder.iterdir() if p.is_dir()), "--host", "cursor"]
    folder = plant_tree(GOLDEN / "cline", tmp_path / "cl", host, counts)
    return [next(p for p in folder.iterdir() if p.is_dir()), "--host", "cline"]


def assert_clean(text, where):
    low = text.lower()
    for word in SENTINEL_WORDS:
        assert word not in low, f"{word} leaked into the {where}"


@pytest.mark.parametrize("host", ["claude-code", "codex", "cursor", "devin", "cline"])
def test_no_planted_sentinel_reaches_the_link(cli, tmp_path, host):
    counts = {}
    sel = planted(host, tmp_path, counts)
    expected = {"prompt", "command", "file path", "home path", "session id"} | ({"model name"} if host in MODELS else set())
    assert {k for k, n in counts.items() if n} >= expected, counts       # every sentinel really is in the copy
    code, out = cli.json(*sel, "--health-all")
    assert "sentinel" in json.dumps(out).lower()                        # the run read the planted data
    url = out["coverage"]["issue_url"]
    title, body = decode(url)
    for text, where in ((url, "url"), (title, "title"), (body, "body")):
        assert_clean(text, where)
    md = cli.run(*sel, fmt="md").stdout
    for link in LINK_LINE.findall(md):
        assert_clean(link, "markdown link")
        assert_clean("\n".join(decode(link)), "markdown link, decoded")
    href = footer_href(report_html(cli, tmp_path, *sel))
    assert_clean(href, "HTML href")
    assert_clean("\n".join(decode(href)), "HTML href, decoded")


# ---- allowlist ---------------------------------------------------------------------------------------------------------

def test_the_body_block_holds_exactly_the_allowlist(cli):
    _, out = cli.json(*sorted(GOLDEN.glob("codex-desktop/rollout-*.jsonl")), "--host", "codex")
    title, body = decode(out["coverage"]["issue_url"])
    keys = body_keys(body)
    assert set(keys) == ALLOWLIST and len(keys) == len(ALLOWLIST), keys
    assert title.startswith("Review problem: codex ")
    after = body.split("```", 2)[2]
    for prompt in ("What were you doing?", "What did you expect?", "Anything else?"):
        assert prompt in after


def test_the_body_names_its_values(cli):
    _, out = cli.json(*sorted(GOLDEN.glob("codex-desktop/rollout-*.jsonl")), "--host", "codex")
    lines = dict(ln.split(": ", 1) for ln in block_of(decode(out["coverage"]["issue_url"])[1]).split("\n")
                 if ln and not ln.startswith(" ") and ": " in ln)
    assert lines["host"] == "codex" and lines["adapter_kind"] == "built-in" and lines["adapter_label"] == "verified"
    assert lines["exit_code"] == "0" and lines["not_recorded"] == "cache_write"
    assert re.fullmatch(r"\d+\.\d+", lines["python"]) and lines["os"]
    assert lines["skill_version"] == re.search(r'version: "([^"]+)"', (SCRIPT.parent.parent / "SKILL.md").read_text()).group(1)


# ---- where it appears --------------------------------------------------------------------------------------------------

def test_the_link_shows_on_a_pause_and_not_on_a_clean_run(cli, tmp_path):
    (tmp_path / "bad").mkdir()
    code, out, report_md, html = run_mutated(cli, tmp_path / "bad", "codex", codex_output_renamed)
    assert code == 2
    lines = LINK_LINE.findall(report_md)
    assert len(lines) == 1 and decode(lines[0])[1] and "exit_code: 2" in decode(lines[0])[1]
    assert FOOTER.search(html) and 'class="report-problem prominent"' in html

    clean = sorted(GOLDEN.glob("codex-desktop/rollout-*.jsonl"))
    md = cli.run(*clean, "--host", "codex", fmt="md").stdout
    assert not LINK_LINE.search(md)
    html = report_html(cli, tmp_path / "bad", *clean, "--host", "codex")
    assert FOOTER.search(html) and "prominent" not in FOOTER.search(html).group(0)


def test_the_metrics_markdown_has_the_line_on_a_pause(cli, tmp_path):
    (tmp_path / "bad").mkdir()
    run_mutated(cli, tmp_path / "bad", "codex", codex_output_renamed)
    files = sorted((tmp_path / "bad").glob("**/rollout-*.jsonl"))
    md = cli.run(*files, "--host", "codex", fmt="md").stdout
    assert len(LINK_LINE.findall(md)) == 1


def test_the_report_markup_keeps_its_csp_and_opens_links_safely(cli, tmp_path):
    html = report_html(cli, tmp_path, one("claude-code/*.jsonl").with_suffix(""), "--host", "claude-code")
    assert '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">' in html
    tag = FOOTER.search(html).group(0)
    assert 'target="_blank"' in tag and 'rel="noopener noreferrer"' in tag


# ---- length --------------------------------------------------------------------------------------------------------------

def every_type_unknown(rs):
    for r in rs:
        if "type" in r:
            r["type"] = "zz_unknown"
    return rs


def test_a_session_with_every_row_broken_stays_under_the_limit(cli, tmp_path):
    code, out, _, _ = run_mutated(cli, tmp_path, "codex", every_type_unknown)
    rows = out["coverage"]["health"]["rows"]
    assert code == 2 and all(r["status"] in ("warn", "broken") for r in rows if r["status"] != "not_measurable")
    url = out["coverage"]["issue_url"]
    title, body = decode(url)
    assert len(url) < 7000 and "host: codex" in body and "exit_code: 2" in body and "transcript_version: " in body


def long_out(n_checks):
    rows = [{"metric": m, "status": "broken" if m in tm.CORE_METRICS else "warn", "core": m in tm.CORE_METRICS,
             "checks": [{"name": f"check number {i} of {m} " + "x" * 40, "passed": False, "layer": "structure"} for i in range(n_checks)]}
            for m in tm.HEALTH_METRICS]
    return {"tokens": {}, "coverage": {
        "host": "codex", "label": "verified", "mapping_source": "builtin", "tested_versions": "0.128.0", "transcript_version": "0.128.0",
        "health": {"summary": {"broken": 3, "warn": 6}, "version": {"position": "within"}, "rows": rows},
        "drift": {"ok": False, "coverage_pct": 12.5, "unseen_types": {"zz_unknown": 40}}}}


def test_a_long_body_drops_low_priority_check_names_first(cli):
    url = tm.issue_url(long_out(20), 2)
    title, body = decode(url)
    assert len(url) < 7000 and "(truncated)" in body
    assert "host: codex" in body and "exit_code: 2" in body and "transcript_version: 0.128.0" in body
    rows = [ln for ln in block_of(body).split("\n") if ln.startswith("  - ")]
    assert len(rows) == len(tm.HEALTH_METRICS)                          # every row is still named, with its status
    kept = {ln.split(":")[0].strip("- ").strip(): ln for ln in rows}
    assert "check number 0 of turns" in kept["turns"] and "check number" not in kept["subagents"]   # turns ranks first, subagents last
    for ln in rows:                                                      # never a check name cut in half
        names = ln.split("; ", 1)[1].split(" | ") if "; " in ln else []
        assert all(re.fullmatch(r"check number \d+ of \w+ x{40}|\(truncated\)", n) for n in names), ln


def test_a_short_body_is_not_truncated(cli):
    url = tm.issue_url(long_out(1), 2)
    assert len(url) < 7000 and "(truncated)" not in decode(url)[1]


# ---- what never goes in ---------------------------------------------------------------------------------------------------

def test_unseen_type_names_pass_the_probe_key_rule():
    out = long_out(1)
    out["coverage"]["drift"]["unseen_types"] = {"tool_summary": 2, "/Users/x/secret.txt": 1, "deadbeef00112233": 1,
                                                "chisel/new_ext": 3, "a prompt with spaces": 1}
    body = decode(tm.issue_url(out, 0))[1]
    assert "unseen_types_count: 5" in body
    line = next(ln for ln in block_of(body).split("\n") if ln.startswith("unseen_types:"))
    assert "tool_summary" in line and "chisel/new_ext" in line
    assert "Users" not in body and "deadbeef" not in body and "prompt with spaces" not in body


def test_a_user_adapter_is_named_only_by_kind():
    out = long_out(1)
    out["coverage"].update(mapping_source="user", label="unproven", tested_versions="/Users/x/adapters/my-secret.toml",
                           host="my_secret_host")
    title, body = decode(tm.issue_url(out, 0))
    assert "adapter_kind: user" in body and "adapter_label: unproven" in body
    assert "my-secret" not in body and "/Users" not in body


def test_a_bare_schema_number_is_named_as_a_schema_in_the_title(cli):
    _, out = cli.json(GOLDEN / "devin-subagent" / "sessions.db", "--host", "devin", "--session-id", "golden-devin-subagent")
    title, body = decode(out["coverage"]["issue_url"])
    assert out["coverage"]["transcript_version"] == "20"
    assert title == "Review problem: devin schema 20, ok"
    assert "transcript_version: 20\n" in body                       # the body keeps the value as the transcript writes it
    _, out = cli.json(*sorted(GOLDEN.glob("codex-desktop/rollout-*.jsonl")), "--host", "codex")
    assert "schema" not in decode(out["coverage"]["issue_url"])[0]  # a dotted version stays as it is


@pytest.mark.parametrize("plat, family", [("win32", "Windows"), ("cygwin", "Windows"), ("darwin", "Darwin"), ("linux", "Linux"),
                                          ("freebsd14", "FreeBSD"), ("sunos5", "other")])
def test_the_os_family_comes_from_sys_platform(plat, family):
    """platform.system() would import socket on Windows (platform.uname looks up the host name): the script never calls it."""
    assert tm.os_family(plat) == family
    assert "import platform" not in SCRIPT.read_text(encoding="utf-8")


def cursor_link(cli):
    _, out = cli.json(next(p for p in (GOLDEN / "cursor-chat1").iterdir() if p.is_dir()), "--host", "cursor")
    return out, decode(out["coverage"]["issue_url"])


def test_a_host_with_no_version_says_so_in_the_title(cli):
    out, (title, body) = cursor_link(cli)
    assert out["coverage"]["transcript_version"] is None
    assert title.startswith("Review problem: cursor (version not recorded), ") and "cursor none" not in title


def test_not_recorded_in_the_body_lists_what_the_host_never_records(cli):
    out, (title, body) = cursor_link(cli)
    line = next(ln for ln in block_of(body).split("\n") if ln.startswith("not_recorded:"))
    assert line == "not_recorded: " + ", ".join(out["coverage"]["not_measurable"])
    assert out["coverage"]["not_measurable"] == ["tokens", "tool timing", "tool results", "message ids"]
    assert "not_recorded" not in out["tokens"]                         # the JSON keeps its own field as it was


def test_codex_lists_its_unrecorded_token_field_and_nothing_else(cli):
    _, out = cli.json(*sorted(GOLDEN.glob("codex-desktop/rollout-*.jsonl")), "--host", "codex")
    line = next(ln for ln in block_of(decode(out["coverage"]["issue_url"])[1]).split("\n") if ln.startswith("not_recorded:"))
    assert out["coverage"]["not_measurable"] == [] and line == "not_recorded: cache_write"
