"""Devin (desktop/CLI) sessions in a SQLite database, read through mappings/devin.toml."""
import json

import pytest
from fixtures import devin as dv


def build(cli, tmp_path, fill, sid="sess-one", **kw):
    db = dv.DevinDb(tmp_path / "sessions.db")
    chain = db.session(sid, str(cli.repo.resolve()), **kw)
    fill(chain)
    chain.done()
    return db.path


def run(cli, path, *args):
    return cli.json(path, "--host", "devin", *args)


def head(chain, names=("hello", "other")):
    chain.add(dv.system(), dv.rules_loaded(["~/a.md", "~/b.md"], 1385), dv.user("one"), dv.skills_loaded(names, hidden=["explicit-only"]))


def normal(chain):
    head(chain)
    chain.add(dv.assistant("", [dv.read("c1"), dv.read("c2")], dv.metrics(100, 1000, 10), "tool_calls"),
              dv.tool_result("c1"), dv.tool_result("c2"),
              dv.assistant("", [dv.edit("c3")], dv.metrics(120, 1100, 12), "tool_calls"), dv.tool_result("c3", kind="edit"),
              dv.assistant("", [dv.shell("c4")], dv.metrics(130, 1200, 14), "tool_calls"),
              dv.tool_result("c4", kind="execute", exit_code=0,
                             timing=("2026-01-05T10:00:00.000Z", "2026-01-05T10:00:04.000Z", 4000)),
              dv.assistant("done", tokens=dv.metrics(140, 1300, 16)),
              dv.user("two", slash="/hello"),
              dv.assistant("", [dv.skill_call("c5")], dv.metrics(10, 100, 1), "tool_calls"), dv.tool_result("c5", kind="other"),
              dv.assistant("ok", tokens=dv.metrics(10, 200, 2)))


def test_normal_session(cli, tmp_path):
    code, out = run(cli, build(cli, tmp_path, normal))
    cov = out["coverage"]
    assert code == 0 and cov["host"] == "devin" and cov["container"] == "sqlite" and cov["drift"]["ok"]
    assert out["window"]["turns_total"] == 2 and out["verify"]["total"] == 1
    assert out["tools"]["by_category"] == {"read": 2, "edit": 1, "shell": 1, "skill": 1}   # the skill's "command" field is not a shell command
    assert out["tools"]["errors"] == 0 and out["interruptions"]["count"] == 0
    assert out["tokens"]["api_calls"] == 6
    assert (out["tokens"]["fresh_input"], out["tokens"]["cache_read"], out["tokens"]["output"], out["tokens"]["cache_write"]) == (510, 4900, 55, 0)
    assert out["actors"][0]["models"] == ["swe-test"]


def test_skills_offered_used_and_instruction_files(cli, tmp_path):
    _, out = run(cli, build(cli, tmp_path, normal))
    sk = out["skills"]
    assert sk["offered"] == {"total": 2, "names": ["hello", "other"]}               # explicit-only is hidden from the model
    assert sk["used"] == ["hello"] and sk["used_not_offered"] == []
    hello = sk["loaded"][0]
    assert hello["detected_by"] == "slash command"
    assert hello["detections"] == [{"by": "slash command", "turn": 2}, {"by": "skill tool call", "turn": 2}]
    assert [f["path"] for f in sk["instruction_files"]] == ["~/a.md", "~/b.md"]
    assert all(f["always_on"] for f in sk["instruction_files"]) and sk["instruction_bytes_always_on"] == 1385


def test_an_explicit_only_skill_is_used_but_not_offered(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.user("two", slash="/explicit-only"), dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
    _, out = run(cli, build(cli, tmp_path, fill))
    assert out["skills"]["used"] == ["explicit-only"] and out["skills"]["used_not_offered"] == ["explicit-only"]


def test_a_prompt_that_only_mentions_a_slash_is_not_a_skill(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.user("see /hello in the text"), dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
    _, out = run(cli, build(cli, tmp_path, fill))
    assert out["skills"]["used"] == []


def test_exit_code_beats_the_success_flag(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.assistant("", [dv.shell("c1", "false")], dv.metrics(1, 1, 1), "tool_calls"),
              dv.tool_result("c1", kind="execute", exit_code=2, success=True), dv.assistant("x", tokens=dv.metrics(1, 1, 1)))
    _, out = run(cli, build(cli, tmp_path, fill))
    assert out["tools"]["errors"] == 1


def test_interrupted(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.assistant("", tokens=dv.metrics(1, 1, 1), finish=None), dv.interrupted())
    _, out = run(cli, build(cli, tmp_path, fill))
    assert out["interruptions"] == {"count": 1, "turns": [1]}


def test_empty(cli, tmp_path):
    def fill(c):
        c.add(dv.system(), dv.rules_loaded(["~/a.md"]))
    code, out = run(cli, build(cli, tmp_path, fill))
    assert code == 0 and out["window"]["turns_total"] == 0 and out["tools"]["total"] == 0 and out["interruptions"]["count"] == 0


def subagent(chain):
    head(chain)
    chain.add(dv.assistant("", [dv.spawn("s1", "x" * 40)], dv.metrics(10, 100, 1), "tool_calls"),
              dv.tool_result("s1", kind="other", **{"subagent__agent_id": "ag1", "subagent__model": "Sub Model", "subagent__chain_node_id": 0}),
              dv.assistant("ok", tokens=dv.metrics(10, 200, 2)))
    chain.subagent("ag1", dv.system(), dv.user("brief"),
                   dv.assistant("", [dv.search("q1"), dv.search("q2")], dv.metrics(7, 0, 3), "tool_calls"),
                   dv.tool_result("q1", kind="search"), dv.tool_result("q2", kind="search"))
    chain.draft(dv.user("abandoned"), dv.user("abandoned too"))


def test_subagent(cli, tmp_path):
    code, out = run(cli, build(cli, tmp_path, subagent))
    assert code == 0 and [a["actor"] for a in out["actors"]] == ["main", "subagent:ag1"]
    main, sub = out["actors"]
    assert out["window"]["turns_total"] == 1
    assert sub["link"] == "subagent_heads" and sub["models"] == ["Sub Model"] and sub["brief_bytes"] == 40
    assert sub["tool_calls"] == 2 and sub["tokens"]["fresh_input"] == 7
    assert main["models"] == ["swe-test"]
    storage = out["coverage"]["storage"]
    assert storage["off_chain_nodes"] == 2 and storage["subagent_chains"] == 1 and storage["nodes_shared_with_main"] == 0


def test_token_cross_check_matches(cli, tmp_path):
    _, out = run(cli, build(cli, tmp_path, normal))
    assert out["actors"][0]["tokens"]["context_check"] == {"calls": 6, "context_input_total": 5410, "mismatches": 0}
    assert "warnings" not in out["coverage"]


def test_token_cross_check_mismatch_is_a_warning(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.assistant("ok", tokens=dv.metrics(10, 100, 1, context=999)))
    _, out = run(cli, build(cli, tmp_path, fill))
    assert out["tokens"]["context_check"]["mismatches"] == 1
    assert any("context_input_tokens" in w for w in out["coverage"]["warnings"])


def test_a_repeated_message_id_counts_once(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.assistant("a", tokens=dv.metrics(10, 100, 1), message_id="same"),
              dv.assistant("b", tokens=dv.metrics(10, 100, 1), message_id="same"))
    _, out = run(cli, build(cli, tmp_path, fill))
    assert out["tokens"]["api_calls"] == 1 and out["tokens"]["fresh_input"] == 10


def test_tool_time_comes_from_the_hosts_own_timing(cli, tmp_path):
    _, out = run(cli, build(cli, tmp_path, normal))
    assert out["time"]["tool_s"] == 4 and out["coverage"]["capabilities"]["has_timestamps"]


def test_cache_write_is_cache_creation_tokens(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.assistant("ok", tokens=dv.metrics(10, 100, 1, cache_write=50, context=110)))
    _, out = run(cli, build(cli, tmp_path, fill))
    assert out["tokens"]["cache_write"] == 50


def test_the_search_tables_are_never_read(cli, tmp_path, monkeypatch, capsys):
    import importlib.util
    from conftest import SCRIPT
    spec = importlib.util.spec_from_file_location("tm_devin", SCRIPT)
    tm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tm)
    path = build(cli, tmp_path, normal)
    seen, real = [], tm.open_sqlite_readonly

    def traced(p):
        conn = real(p)
        conn.set_trace_callback(seen.append)
        return conn
    monkeypatch.setattr(tm, "open_sqlite_readonly", traced)
    monkeypatch.chdir(cli.repo)
    assert tm.main([str(path), "--host", "devin", "--format", "json"]) == 0
    out = capsys.readouterr().out
    assert not [s for s in seen if "message_search" in s] and dv.SECRET not in out


def test_the_schema_version_is_reported(cli, tmp_path):
    _, out = run(cli, build(cli, tmp_path, normal))
    assert out["coverage"]["transcript_version"] == "20"
    assert out["coverage"]["storage"]["schema_version"] == 20


def test_session_choice_follows_the_sqlite_rules(cli, tmp_path):
    db = dv.DevinDb(tmp_path / "sessions.db")
    import time
    now = int(time.time())
    for sid, last in (("old", now - 7200), ("newer", now - 3600)):
        chain = db.session(sid, str(cli.repo.resolve()), created=last - 100, last=last)
        head(chain)
        chain.add(dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
        chain.done()
    code, out = run(cli, db.path)
    assert code == 0 and out["window"]["turns_total"] == 1
    code, out = run(cli, db.path, "--session-id", "old")
    assert code == 0
    for sid in ("r1", "r2"):
        chain = db.session(sid, str(cli.repo.resolve()), created=now - 50, last=now - 10)
        head(chain)
        chain.done()
    code, out = run(cli, db.path)
    assert code == 3 and {a["session"] for a in out["ambiguous"]} == {"r1", "r2"}


def test_an_interrupted_reply_with_empty_metrics_still_counts_as_a_call(cli, tmp_path):
    def fill(c):
        head(c)
        c.add(dv.assistant("a", tokens=dv.metrics(10, 100, 1)),
              dv.assistant("", tokens={"input_tokens": None, "output_tokens": 0, "cache_read_tokens": None,
                                       "cache_creation_tokens": None, "context_input_tokens": None}, finish=None))
    _, out = run(cli, build(cli, tmp_path, fill))
    assert out["tokens"]["api_calls"] == 2 and out["tokens"]["fresh_input"] == 10 and out["tokens"]["context_check"]["calls"] == 1


def test_a_rule_file_the_repo_scan_already_found_is_listed_once(cli, tmp_path):
    (cli.home / ".claude").mkdir()
    (cli.home / ".claude" / "CLAUDE.md").write_text("x")
    def fill(c):
        c.add(dv.rules_loaded([str(cli.home / ".claude" / "CLAUDE.md"), "~/b.md"]), dv.user("one"), dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
    _, out = run(cli, build(cli, tmp_path, fill))
    paths = [f["path"] for f in out["skills"]["instruction_files"]]
    assert paths.count("~/.claude/CLAUDE.md") == 1 and "~/b.md" in paths


@pytest.mark.parametrize("schema, want", [(5, "old"), (20, "new")])
def test_the_database_schema_number_breaks_a_variant_tie(cli, tmp_path, schema, want):
    from conftest import ROOT
    text = (ROOT / "skills/workflow-reviewer/scripts/mappings/devin.toml").read_text()
    text += ('\n[[variants]]\nname = "old"\nversions = "schema 1 to 10"\n'
             '\n[[variants]]\nname = "new"\nversions = "schema 11 and newer"\n')
    db = dv.DevinDb(tmp_path / "s.db", schema=schema)
    chain = db.session("s1", str(cli.repo.resolve()))
    head(chain)
    chain.add(dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
    chain.done()
    code, out = cli.json(db.path, "--mapping", "-", stdin=text)
    assert code == 0 and out["coverage"]["variant"] == want
    assert out["coverage"]["variant_choice"]["reason"] == "version range tiebreak"


def test_model_names_that_match_after_normalising_are_reported_in_the_id_form(cli, tmp_path):
    def fill(c):
        subagent(c)
    path = build(cli, tmp_path, fill, model="swe-test-medium")
    import sqlite3
    conn = sqlite3.connect(path)
    rows = conn.execute("select node_id, chat_message from message_nodes").fetchall()
    for nid, cm in rows:
        if "Sub Model" in cm:
            conn.execute("update message_nodes set chat_message=? where node_id=? and session_id='sess-one'", (cm.replace("Sub Model", "SWE Test_Medium"), nid))
    conn.commit()
    conn.close()
    _, out = run(cli, path)
    assert [a["models"] for a in out["actors"]] == [["swe-test-medium"], ["swe-test-medium"]]


def test_different_model_names_stay_different(cli, tmp_path):
    _, out = run(cli, build(cli, tmp_path, subagent))
    assert [a["models"] for a in out["actors"]] == [["swe-test"], ["Sub Model"]]
