"""A rule file is always-on only when the host loads it at the start: in the first turn (or before it) and before the
agent's first tool call. A file that arrives later, typically because a tool call touched its folder, is loaded on demand,
with its turn, and its bytes are not part of the always-on total."""
from fixtures import devin as dv
from fixtures.builders import at, cc_assistant, cc_bash_call, cc_user, write_jsonl
from test_devin import build, run

START = ["~/rules/AGENTS.md", "~/rules/style.md"]
LATE = ["/opt/homebrew/AGENTS.md", "/opt/homebrew/CLAUDE.md"]


def files(out):
    return {f["path"]: f for f in out["skills"]["instruction_files"]}


def mid_session(chain):
    chain.add(dv.system(), dv.rules_loaded(START, 1385), dv.user("one"),
              dv.assistant("", [dv.shell("c1", "ls /opt/homebrew/bin")], dv.metrics(10, 100, 1), "tool_calls"),
              dv.tool_result("c1", kind="execute", exit_code=0),
              dv.rules_loaded(LATE, 7577),                                   # Devin injects the folder's rule files
              dv.assistant("done", tokens=dv.metrics(10, 100, 1)))


def test_a_rule_file_injected_after_a_tool_call_is_on_demand(cli, tmp_path):
    code, out = run(cli, build(cli, tmp_path, mid_session))
    f = files(out)
    assert code == 0
    for p in START:
        assert f[p]["always_on"] is True and "on_demand_turn" not in f[p]
    for p in LATE:
        assert f[p]["always_on"] is False and f[p]["on_demand_turn"] == 1, f[p]
    assert out["skills"]["instruction_bytes_always_on"] == 1385                # never the 7,577 of the injected files


def test_a_rule_file_first_seen_in_a_later_turn_is_on_demand_even_without_a_tool_call(cli, tmp_path):
    def fill(c):
        c.add(dv.system(), dv.rules_loaded(START, 1385), dv.user("one"), dv.assistant("ok", tokens=dv.metrics(10, 100, 1)),
              dv.user("two"), dv.rules_loaded(LATE, 7577), dv.assistant("ok", tokens=dv.metrics(10, 100, 1)))
    _, out = run(cli, build(cli, tmp_path, fill))
    f = files(out)
    assert all(f[p]["always_on"] is False and f[p]["on_demand_turn"] == 2 for p in LATE)
    assert out["skills"]["instruction_bytes_always_on"] == 1385


def test_only_on_demand_rule_files_give_no_always_on_total(cli, tmp_path):
    def fill(c):
        c.add(dv.system(), dv.user("one"), dv.assistant("", [dv.shell("c1", "ls /opt/homebrew")], dv.metrics(10, 100, 1), "tool_calls"),
              dv.tool_result("c1", kind="execute", exit_code=0), dv.rules_loaded(LATE, 7577), dv.assistant("done", tokens=dv.metrics(10, 100, 1)))
    _, out = run(cli, build(cli, tmp_path, fill))
    assert "instruction_bytes_always_on" not in out["skills"]
    assert all(files(out)[p]["always_on"] is False for p in LATE)


def test_the_devin_goldens_keep_their_start_of_session_rule_files_always_on(cli):
    from test_golden import devin
    for name in ("subagent", "error-interrupted", "explicit-only"):
        _, out = devin(cli, name)
        host = [f for f in out["skills"]["instruction_files"] if "on_demand_turn" in f or f.get("always_on") is False]
        assert not host, (name, host)


def instructions(paths, t):
    return {"type": "attachment", "timestamp": at(t), "sessionId": "s1",
            "attachment": {"type": "instructions", "files": [{"path": p, "content": "placeholder rule text"} for p in paths]}}


def test_claude_code_follows_the_same_rule(cli, tmp_path):
    records = [cc_user("go", 0), instructions(["/proj/CLAUDE.md"], 1), *cc_bash_call("m1", "c1", "ls sub", 2),
               instructions(["/proj/sub/CLAUDE.md"], 5), cc_assistant("m2", 6)]
    path = write_jsonl(tmp_path / "s.jsonl", records)
    _, out = cli.json(path, "--host", "claude-code")
    f = files(out)
    assert f["/proj/CLAUDE.md"]["always_on"] is True and "on_demand_turn" not in f["/proj/CLAUDE.md"]
    assert f["/proj/sub/CLAUDE.md"]["always_on"] is False and f["/proj/sub/CLAUDE.md"]["on_demand_turn"] == 1
