import subprocess
import sys

from conftest import SCRIPT
from fixtures.builders import (cc_assistant, cc_bash_call, cc_tool_result, cc_user, tool_use,
                               usage, write_jsonl)

SECRETS = {
    "anthropic": "sk-ant-api03-AbCdEf1234567890abcdefXYZ",
    "github": "ghp_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5",
    "entropy": "Zk3Jd9Qw1Lm8Xr4Tn7Vb2Hc5Ys6Pa0Gf",
    "email": "jane.doe@example.com",
}


def leaky(tmp_path, home):
    text = (f"see src/app.py key {SECRETS['anthropic']} token {SECRETS['github']} blob {SECRETS['entropy']} "
            f"mail {SECRETS['email']} file {home}/secret/app.py end")
    return write_jsonl(tmp_path / "leak.jsonl", [
        cc_user(text, 0),
        *cc_bash_call("m1", "c1", f"cat {home}/secret/app.py", 5),
    ])


def assert_clean(stdout, home):
    for name, secret in SECRETS.items():
        assert secret not in stdout, name
    assert str(home) not in stdout
    assert "src/app.py" in stdout


def test_every_output_mode_is_redacted(cli, tmp_path):
    path = leaky(tmp_path, cli.home)
    for extra in (["--list-turns"], ["--events", "0:2"], ["--sample", "5"]):
        proc = cli.run(path, "--host", "claude-code", *extra)
        assert proc.returncode == 0
        for name, secret in SECRETS.items():
            assert secret not in proc.stdout, (extra, name)
        assert str(cli.home) not in proc.stdout, extra


def test_home_paths_show_as_tilde_and_ordinary_paths_survive(cli, tmp_path):
    proc = cli.run(leaky(tmp_path, cli.home), "--host", "claude-code", "--events", "0:1")
    assert "~/secret/app.py" in proc.stdout
    assert_clean(proc.stdout, cli.home)


def test_markdown_output_is_redacted_too(cli, tmp_path):
    proc = cli.run(leaky(tmp_path, cli.home), "--host", "claude-code", "--events", "0:1", fmt="md")
    assert_clean(proc.stdout, cli.home)


def test_no_redact_flag_disables_masking(cli, tmp_path):
    proc = cli.run(leaky(tmp_path, cli.home), "--host", "claude-code", "--events", "0:0", "--no-redact")
    assert SECRETS["email"] in proc.stdout


# ---- skills --------------------------------------------------------------------------

def skill(root, name, description, body="steps"):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n")


def test_skills_loaded_inventory_and_instruction_files(cli, tmp_path):
    skill(cli.home / ".claude" / "skills", "foo", "Does foo things")
    skill(cli.home / ".claude" / "skills", "bar", "Does bar")
    skill(cli.repo / ".claude" / "skills", "foo", "Does foo things")         # identical copy
    skill(cli.repo / ".cursor" / "skills", "cursor-only", "not for Claude Code")
    (cli.repo / "CLAUDE.md").write_text("x" * 300)
    (cli.repo / "AGENTS.md").write_text("y" * 120)
    foo_md = f"{cli.home}/.claude/skills/foo/SKILL.md"

    def read(mid, cid, path, size, t):
        return [cc_assistant(mid, t, [tool_use("Read", {"file_path": path}, cid)], usage(1, 1)),
                cc_tool_result(cid, "z" * size, t + 1)]
    path = write_jsonl(tmp_path / "s.jsonl", [
        cc_user("go", 0),
        cc_assistant("m0", 2, [tool_use("Skill", {"skill": "tdd"}, "cs")], usage(1, 1)),
        cc_tool_result("cs", "loaded", 3),
        *read("m1", "c1", foo_md, 1234, 10),
        *read("m2", "c2", f"{cli.home}/.claude/skills/foo/references/x.md", 500, 20),
    ])
    _, out = cli.json(path, "--host", "claude-code")
    loaded = {s["name"]: s for s in out["skills"]["loaded"]}
    assert loaded["tdd"]["detected_by"] == "skill-tool"
    assert loaded["foo"]["detected_by"] == "SKILL.md read"
    assert loaded["foo"]["bytes"] == 1734
    names = sorted(s["name"] for s in out["skills"]["inventory"])
    assert names == ["bar", "foo"]                       # deduped, host-limited
    foo = next(s for s in out["skills"]["inventory"] if s["name"] == "foo")
    assert foo["description_chars"] == len("Does foo things")
    files = {f["path"]: f["bytes"] for f in out["skills"]["instruction_files"]}
    assert files["CLAUDE.md"] == 300 and files["AGENTS.md"] == 120


# ---- config, markdown, guarantees ------------------------------------------------------

def test_workflow_review_toml_supplies_verify_patterns(cli, tmp_path):
    (cli.repo / ".workflow-review.toml").write_text('verify_patterns = ["\\\\./ci-check"]\n')
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), *cc_bash_call("m", "c", "./ci-check --fast", 5)])
    _, out = cli.json(path, "--host", "claude-code")
    assert out["verify"]["total"] == 1


def test_markdown_report_is_compact(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), *cc_bash_call("m", "c", "pytest", 5)])
    proc = cli.run(path, "--host", "claude-code", fmt="md")
    assert proc.returncode == 0
    assert "## Tokens" in proc.stdout and "## Coverage" in proc.stdout
    assert len(proc.stdout) < 8000


def test_degraded_mode_uses_git_when_there_is_no_transcript(cli):
    def git(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cli.repo,
                       check=True, capture_output=True)
    git("init", "-q")
    (cli.repo / "a.py").write_text("1")
    git("add", "."); git("commit", "-q", "-m", "one")
    (cli.repo / "a.py").write_text("2")
    git("commit", "-q", "-am", "two")
    code, out = cli.json("--host", "kiro")
    assert code == 0
    assert out["mode"] == "degraded"
    assert out["confidence_cap"] == "medium"
    assert out["git"]["commits"] == 2
    assert "a.py" in out["git"]["files"]
    assert "recollection" in out["evidence_rule"]


def test_script_imports_no_network_modules(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0)])
    proc = subprocess.run([sys.executable, "-X", "importtime", str(SCRIPT), str(path), "--format", "json"],
                          capture_output=True, text=True, env=cli.env(), cwd=str(cli.repo))
    imported = {line.split("|")[-1].strip() for line in proc.stderr.splitlines() if "|" in line}
    network = {"socket", "_socket", "ssl", "ftplib", "smtplib", "requests", "urllib.request", "urllib3"}
    assert not imported & network
    assert not {m for m in imported if m == "http" or m.startswith("http.")}


def test_script_writes_nothing(cli, tmp_path):
    path = leaky(tmp_path, cli.home)
    snap = lambda: sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                          for p in tmp_path.rglob("*"))
    before = snap()
    for extra in ([], ["--list-turns"], ["--events", "0:1"], ["--sample", "2"]):
        cli.run(path, "--host", "claude-code", *extra)
    assert snap() == before


def test_tool_call_ids_survive_redaction_so_calls_can_be_tied_to_results(cli, tmp_path):
    call_id = "toolu_01A9bC3dE5fG7hJ2kL4mN6pQ"
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), *cc_bash_call("m", call_id, "ls", 5)])
    proc = cli.run(path, "--host", "claude-code", "--events", "1:2")
    assert call_id in proc.stdout


def test_windows_style_skill_paths_are_detected(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        cc_user("go", 0),
        cc_assistant("m", 2, [tool_use("Read", {"file_path": "C:\\Users\\a\\.claude\\skills\\foo\\SKILL.md"}, "c1")],
                     usage(1, 1)),
        cc_tool_result("c1", "z" * 40, 3),
    ])
    _, out = cli.json(path, "--host", "claude-code")
    assert [s["name"] for s in out["skills"]["loaded"]] == ["foo"]


def test_low_entropy_secrets_in_key_value_and_url_credentials_are_masked(cli, tmp_path):
    text = ("export PASSWORD=hunter2 and api_key: abc123 and MY_SECRET_TOKEN = 'tiny' "
            "db postgres://admin:S3cretPass@db.internal:5432/app done")
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user(text, 0)])
    proc = cli.run(path, "--host", "claude-code", "--events", "0:0")
    for leaked in ("hunter2", "abc123", "tiny", "S3cretPass"):
        assert leaked not in proc.stdout, leaked
    assert "db.internal" in proc.stdout and "admin" in proc.stdout


def test_redaction_keeps_the_json_report_valid(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), cc_assistant("m", 1, usage=usage(1, 1))])
    code, out = cli.json(path, "--host", "claude-code")
    assert isinstance(out["tokens"], dict)


def test_json_style_secret_fields_are_masked(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user('config {"password": "p4ss word", "ok": 1}', 0)])
    proc = cli.run(path, "--host", "claude-code", "--events", "0:0")
    assert "p4ss" not in proc.stdout
