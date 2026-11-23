"""--host auto: environment marker, then the host with the most recent session here, else exit 6."""
import json
import os
import re
import shutil
import time

from fixtures import devin as dv
from fixtures.builders import cc_assistant, cc_user, cx_meta, cx_user, usage, write_jsonl

from conftest import ROOT


def claude_session(cli, age=3600, name="s1"):
    folder = cli.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cli.repo))
    path = write_jsonl(folder / f"{name}.jsonl", [cc_user("go", 0), cc_assistant("m", 1, usage=usage(1, 1))])
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def codex_session(cli, age=3600):
    day = cli.home / ".codex" / "sessions" / "2026" / "11" / "01"
    path = write_jsonl(day / "rollout-2026-11-01T10-00-00-abc.jsonl", [cx_meta("abc", cli.repo), cx_user("go", 1)])
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def devin_db(cli, age=3600):
    folder = cli.home / ".local" / "share" / "devin" / "cli"
    folder.mkdir(parents=True)
    db = dv.DevinDb(folder / "sessions.db")
    now = int(time.time())
    chain = db.session("d1", str(cli.repo.resolve()), created=now - age - 100, last=now - age)
    chain.add(dv.system(), dv.rules_loaded(["~/a.md"]), dv.user("go"), dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
    chain.done()
    db.conn.close()                       # Windows cannot delete a database file that is still open


def chosen(out):
    return out["coverage"]["host"], out["coverage"]["host_chosen_by"]


def test_claude_code_declares_its_environment_marker():
    import tomllib
    raw = tomllib.loads((ROOT / "skills/workflow-reviewer/scripts/mappings/claude_code.toml").read_text(encoding="utf-8"))
    assert raw["detect"] == {"env": ["CLAUDECODE"]}
    for other in ("codex", "cursor", "devin"):
        assert "detect" not in tomllib.loads((ROOT / f"skills/workflow-reviewer/scripts/mappings/{other}.toml").read_text(encoding="utf-8"))


def test_an_environment_marker_chooses_the_host(cli):
    claude_session(cli, age=3000)
    cli.env_extra["CLAUDECODE"] = "1"
    code, out = cli.json()
    assert code == 0 and chosen(out) == ("claude-code", "env marker")


def test_the_marker_beats_a_more_recent_session_of_another_host(cli):
    claude_session(cli, age=5000)
    codex_session(cli, age=1000)
    cli.env_extra["CLAUDECODE"] = "1"
    assert chosen(cli.json()[1]) == ("claude-code", "env marker")


def test_only_the_names_of_markers_are_used_never_the_values(cli):
    claude_session(cli)
    cli.env_extra["CLAUDECODE"] = "VALUE-THAT-MUST-NOT-APPEAR"
    proc = cli.run("--no-redact")
    assert "VALUE-THAT-MUST-NOT-APPEAR" not in proc.stdout
    cli.env_extra["CLAUDECODE"] = ""                      # set but empty: not a marker
    codex_session(cli, age=100)
    assert chosen(cli.json()[1]) == ("codex", "data presence")


def test_without_a_marker_the_host_with_the_most_recent_session_here_is_chosen(cli):
    claude_session(cli, age=5000)
    codex_session(cli, age=1500)
    assert chosen(cli.json()[1]) == ("codex", "data presence")


def test_devin_is_found_by_its_database(cli):
    devin_db(cli, age=2000)
    code, out = cli.json()
    assert code == 0 and chosen(out) == ("devin", "data presence")


def test_the_most_recent_of_a_jsonl_host_and_the_devin_database_wins(cli):
    claude_session(cli, age=2000)
    devin_db(cli, age=9000)
    assert chosen(cli.json()[1])[0] == "claude-code"
    shutil.rmtree(cli.home / ".local")
    devin_db(cli, age=100)
    assert chosen(cli.json()[1])[0] == "devin"


def test_an_explicit_host_is_reported_as_such_even_with_a_marker(cli):
    claude_session(cli)
    codex_session(cli)
    cli.env_extra["CLAUDECODE"] = "1"
    out = cli.json("--host", "codex")[1]
    assert chosen(out) == ("codex", "--host")


def test_explicit_paths_with_auto_are_chosen_by_shape(cli):
    path = claude_session(cli)
    assert chosen(cli.json(path)[1]) == ("claude-code", "transcript shape")


def test_nothing_anywhere_is_exit_6_with_the_hosts_that_were_checked(cli):
    code, out = cli.json()
    assert code == 6 and out["mode"] == "degraded" and out["transcript_found"] is False
    assert {"claude-code", "codex", "cursor", "devin"} <= set(out["hosts_checked"])


def test_a_marker_host_with_no_session_here_reports_that_instead_of_guessing(cli):
    codex_session(cli)
    cli.env_extra["CLAUDECODE"] = "1"
    code, out = cli.json()
    assert code == 6 and out["mode"] == "degraded"


def test_two_recent_sessions_of_one_host_are_still_ambiguous(cli):
    claude_session(cli, age=20, name="a")
    claude_session(cli, age=40, name="b")
    cli.env_extra["CLAUDECODE"] = "1"
    code, out = cli.json()
    assert code == 3 and len(out["ambiguous"]) == 2


def test_a_user_adapter_can_declare_its_own_marker_and_a_store_of_sessions(cli, tmp_path):
    sessions = tmp_path / "jd"
    folder = sessions / "s1"
    folder.mkdir(parents=True)
    (folder / "api.json").write_text(json.dumps([{"role": "user", "text": "hi", "ts": 1}]))
    stamp = time.time() - 3000
    os.utime(folder / "api.json", (stamp, stamp))
    adapter = cli.home / "xdg" / "workflow-reviewer" / "hosts"
    adapter.mkdir(parents=True)
    (adapter / "jd.toml").write_text(f'''
host = "jd"
schema_version = 1
type_path = "role"
detect = {{ env = ["JD_HOST_ACTIVE"] }}
[container]
kind = "json_document"
roots = ["{sessions.as_posix()}"]
[[container.documents]]
file = "api.json"
[capabilities]
has_tokens = false
has_timestamps = false
has_tool_results = false
has_ids = false
[[records]]
match = {{ role = "user" }}
role = "user"
text = ["text"]
''')
    cli.env_extra["XDG_CONFIG_HOME"] = cli.env_extra["APPDATA"] = str(cli.home / "xdg")
    assert chosen(cli.json()[1]) == ("jd", "data presence")
    cli.env_extra["JD_HOST_ACTIVE"] = "1"
    claude_session(cli, age=100)
    assert chosen(cli.json()[1]) == ("jd", "env marker")


def test_a_marker_must_be_a_variable_name(cli, tmp_path):
    from test_health import MAPPING
    bad = MAPPING.replace('type_path = "t"', 'type_path = "t"\ndetect = { env = ["lower case value=1"] }')
    path = write_jsonl(tmp_path / "s.jsonl", [{"t": "u", "text": "x"}])
    proc = cli.run(path, "--save-adapter", "hx", stdin=bad)
    assert proc.returncode == 2 and "names only" in json.loads(proc.stdout)["error"] + "".join(json.loads(proc.stdout)["errors"])
