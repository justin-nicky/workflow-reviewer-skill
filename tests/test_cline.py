"""Cline: a session is a folder of two JSON documents; the built-in mapping reads them with the json_document engine."""
import json
import os
import time

import pytest
from fixtures.cline import (T0, assistant, failed_command, normal_messages, ok_command, other_user_text, tool_result, tool_use,
                            user_input, write_db, write_session)

from conftest import ROOT

GOLDEN = ROOT / "tests" / "fixtures" / "golden" / "cline" / "replica_session"


def run(cli, folder, *args):
    return cli.json(folder, "--host", "cline", *args)


def build(cli, tmp_path, messages, **kw):
    return write_session(tmp_path / "sessions", "s1", cli.repo, messages, **kw)


# ---- the five host cases ----------------------------------------------------------------------------------------------------------

def test_normal(cli, tmp_path):
    code, out = run(cli, build(cli, tmp_path, normal_messages()))
    c = out["coverage"]
    assert code == 0 and c["host"] == "cline" and c["label"] == "verified" and c["variant"] == "default" and c["drift"]["ok"]
    assert out["window"]["turns_total"] == 2 and out["verify"]["total"] == 1 and out["tools"]["by_category"] == {"shell": 1}
    assert out["tools"]["errors"] == 0 and out["interruptions"]["count"] == 0
    assert out["tokens"]["api_calls"] == 3 and out["tokens"]["fresh_input"] == 3 * 40 and out["tokens"]["cache_read"] == 180
    assert out["actors"][0]["models"] == ["test-model"] and c["transcript_version"] == "4.1.26" and out["time"]["wall_s"] == 12


def test_empty(cli, tmp_path):
    code, out = run(cli, build(cli, tmp_path, []))
    assert code == 0 and out["window"]["turns_total"] == 0 and out["tools"]["total"] == 0 and out["interruptions"]["count"] == 0


def test_error(cli, tmp_path):
    msgs = [user_input("fail", 1, 0),
            assistant(1, 1, [tool_use("run_commands", {"commands": ["python3 -c 'import sys; sys.exit(2)'"]}, "call-a")]),
            tool_result("call-a", "run_commands", [failed_command()], i=2, t=2), assistant(3, 3, text="it failed")]
    code, out = run(cli, build(cli, tmp_path, msgs))
    assert code == 0 and out["tools"]["errors"] == 1                                   # the exit code is read from the result text


def test_interrupted(cli, tmp_path):
    msgs = [user_input("first", 1, 0), user_input("second", 2, 5), assistant(3, 6, text="ok")]
    code, out = run(cli, build(cli, tmp_path, msgs))
    assert code == 0 and out["interruptions"] == {"count": 1, "turns": [1]} and out["window"]["turns_total"] == 2


def test_subagent(cli, tmp_path):
    root = cli.home / ".cline" / "data" / "sessions"
    write_session(root, "parent1", cli.repo, [user_input("go", 1, 0), assistant(1, 1, text="spawning"), assistant(2, 2, text="done")])
    write_session(root, "child1", cli.repo, [user_input("sub task", 1, 1), assistant(1, 2, [tool_use("read_files", {"files": [{"path": "a.py"}]}, "call-s")]),
                                             tool_result("call-s", "read_files", text="content", i=2, t=3)], version="4.1.26")
    write_db(cli.home / ".cline" / "data" / "db" / "sessions.db", [("parent1", None, 0), ("child1", "parent1", 1), ("other", "x", 1)])
    code, out = cli.json("--host", "cline", "--session-id", "parent1")
    assert code == 0 and [a["actor"] for a in out["actors"]] == ["main", "subagent:child1"]
    assert out["actors"][1]["link"] == "parent_session_id" and out["actors"][1]["tool_calls"] == 1
    assert out["window"]["turns_total"] == 1 and out["coverage"]["storage"]["subagent_sessions"] == 1


# ---- the format ---------------------------------------------------------------------------------------------------------------------

def test_a_user_message_without_the_user_input_wrapper_is_not_a_turn(cli, tmp_path):
    msgs = [user_input("one", 1, 0), assistant(1, 1, text="a"), other_user_text("injected context", 2, 2), assistant(3, 3, text="b")]
    _, out = run(cli, build(cli, tmp_path, msgs))
    assert out["window"]["turns_total"] == 1 and out["interruptions"]["count"] == 0


def test_tool_results_between_prompts_are_not_prompts_and_a_reply_in_between_is_no_interruption(cli, tmp_path):
    msgs = [user_input("a", 1, 0), assistant(1, 1, [tool_use("read_files", {"files": []}, "c1")]), tool_result("c1", "read_files", text="x", i=2, t=2),
            assistant(3, 3, text="r"), user_input("b", 4, 4), assistant(5, 5, text="r")]
    _, out = run(cli, build(cli, tmp_path, msgs))
    assert out["window"]["turns_total"] == 2 and out["interruptions"]["count"] == 0


def test_each_command_of_a_run_commands_call_is_its_own_call(cli, tmp_path):
    msgs = [user_input("go", 1, 0), assistant(1, 1, [tool_use("run_commands", {"commands": ["ls", "python3 -m pytest -q", "ls -la"]}, "c1")]),
            tool_result("c1", "run_commands", [("ls", "x", True), ok_command(), ("ls -la", "y", True)], i=2, t=2), assistant(3, 3, text="done")]
    code, out = run(cli, build(cli, tmp_path, msgs))
    assert code == 0 and out["tools"]["by_category"] == {"shell": 3} and out["verify"]["total"] == 1
    assert out["coverage"]["health"]["rows"] == []                                    # one call id, one result: still counted once


def test_tool_names_set_the_categories(cli, tmp_path):
    uses = [tool_use("read_files", {"files": [{"path": "a.py"}]}, "c1"), tool_use("editor", {"path": "a.py", "old_text": "a", "new_text": "b"}, "c2"),
            tool_use("skills", {"skill": "hello"}, "c3"), tool_use("something_new", {}, "c4")]
    msgs = [user_input("go", 1, 0), assistant(1, 1, uses)] + [tool_result(u["id"], u["name"], text="x", i=2 + k, t=2) for k, u in enumerate(uses)] + [assistant(9, 5, text="ok")]
    _, out = run(cli, build(cli, tmp_path, msgs))
    assert out["tools"]["by_category"] == {"read": 1, "edit": 1, "skill": 1, "other": 1}
    assert out["skills"]["used"] == ["hello"] and out["skills"]["loaded"][0]["detected_by"] == "skill tool call"
    assert out["skills"]["offered"] == {"not_measurable": "the host records no list of skills offered to the model"}


def test_fresh_input_is_the_input_minus_the_cache_reads(cli, tmp_path):
    msgs = [user_input("go", 1, 0), assistant(1, 1, text="a", usage=(1000, 50, 900, 20))]
    _, out = run(cli, build(cli, tmp_path, msgs))
    t = out["tokens"]
    assert (t["fresh_input"], t["cache_read"], t["cache_write"], t["output"]) == (100, 900, 20, 50)


def test_the_second_counts_agree_with_the_session_metadata(cli, tmp_path):
    _, out = run(cli, build(cli, tmp_path, normal_messages()), "--health-all")
    rows = {r["metric"]: r for r in out["coverage"]["health"]["rows"]}
    assert rows["tokens"]["status"] == "ok" and rows["tool_calls"]["status"] == "ok"
    names = {c["name"] for c in rows["tokens"]["checks"] if c["layer"] == "second count"}
    assert len(names) == 4 and all(c["passed"] for c in rows["tokens"]["checks"])


def test_metadata_that_disagrees_with_the_messages_breaks_tokens(cli, tmp_path):
    folder = build(cli, tmp_path, normal_messages(), usage_override={"inputTokens": 999, "outputTokens": 30, "cacheReadTokens": 180, "cacheWriteTokens": 0, "totalCost": 0})
    code, out = run(cli, folder, "--health-all")
    assert code == 2 and not out["coverage"]["drift"]["ok"]
    assert {r["metric"]: r["status"] for r in out["coverage"]["health"]["rows"]}["tokens"] == "broken"


def test_a_session_is_chosen_by_its_working_directory_and_recency(cli, tmp_path):
    root = cli.home / ".cline" / "data" / "sessions"
    write_session(root, "mine", cli.repo, normal_messages())
    write_session(root, "elsewhere", tmp_path / "other", normal_messages()[:2])
    for f in (root / "mine").iterdir():
        os.utime(f, (time.time() - 3000,) * 2)
    code, out = cli.json("--host", "cline")
    assert code == 0 and out["window"]["turns_total"] == 2 and out["coverage"]["host_chosen_by"] == "--host"


def test_the_environment_marker_chooses_cline(cli, tmp_path):
    root = cli.home / ".cline" / "data" / "sessions"
    write_session(root, "mine", cli.repo, normal_messages())
    for f in (root / "mine").iterdir():
        os.utime(f, (time.time() - 3000,) * 2)
    cli.env_extra["CLINE_ACTIVE"] = "1"
    code, out = cli.json()
    assert code == 0 and out["coverage"]["host"] == "cline" and out["coverage"]["host_chosen_by"] == "env marker"


def test_declared_marker_and_label():
    import tomllib
    raw = tomllib.loads((ROOT / "skills/workflow-reviewer/scripts/mappings/cline.toml").read_text())
    assert raw["detect"] == {"env": ["CLINE_ACTIVE"]} and raw["tested_versions"] == "4.1.26" and "made_by" not in raw


# ---- the golden replica of the real session ------------------------------------------------------------------------------------------

def test_the_golden_replica_reproduces_the_real_sessions_numbers(cli):
    code, out = cli.json(GOLDEN, "--host", "cline", "--health-all")
    c, t = out["coverage"], out["tokens"]
    assert code == 0 and c["drift"]["ok"] and c["drift"]["coverage_pct"] == 100.0 and c["label"] == "verified"
    assert (out["window"]["turns_total"], out["interruptions"]) == (12, {"count": 1, "turns": [6]})
    assert out["tools"]["by_category"] == {"read": 2, "skill": 1, "edit": 2, "shell": 8} and out["tools"]["total"] == 13
    assert out["tools"]["errors"] == 1 and out["verify"]["total"] == 1
    assert out["skills"]["used"] == ["hello"] and out["skills"]["loaded"][0]["detections"] == [{"by": "skill tool call", "turn": 2}]
    assert "not_measurable" in out["skills"]["offered"]
    assert {k: t[k] for k in ("api_calls", "fresh_input", "cache_read", "cache_write", "output")} == \
        {"api_calls": 23, "fresh_input": 95309, "cache_read": 134416, "cache_write": 0, "output": 3402}
    assert out["actors"][0]["models"] == ["cline-free/nemotron-3.5-super-vl"] and c["transcript_version"] == "4.1.26"
    assert out["time"]["wall_s"] == 537 and len(out["actors"]) == 1
    rows = {r["metric"]: r["status"] for r in c["health"]["rows"]}
    assert rows["tokens"] == "ok" and rows["tool_calls"] == "ok" and rows["turns"] == "one_way"
    second = [x for r in c["health"]["rows"] for x in r["checks"] if x["layer"] == "second count"]
    assert len(second) == 5 and all(x["passed"] and x["first"] == x["second"] for x in second)


def test_the_golden_replica_holds_nothing_from_the_real_session():
    text = "".join(p.read_text() for p in GOLDEN.iterdir())
    for token in ("alex", "/Users/", "@"):
        assert token not in text
