from fixtures.builders import (bash, cc_assistant, cc_tool_result, cc_user, tool_use, usage,
                               write_jsonl)


def test_subagent_time_is_not_added_to_the_main_agents_tool_time(cli, tmp_path):
    main = write_jsonl(tmp_path / "s1.jsonl", [
        cc_user("go", 0, uuid="u1"),
        cc_assistant("m1", 5, [tool_use("Task", {"prompt": "review"}, "c1")], usage(1, 1), uuid="a1"),
        cc_tool_result("c1", "reviewed", 105, uuid="r1"),
        cc_assistant("m2", 110, usage=usage(1, 1), uuid="a2"),
    ])
    write_jsonl(tmp_path / "s1" / "subagents" / "agent-1.jsonl", [
        cc_user("review", 6, uuid="su1"),
        cc_assistant("sm1", 10, [bash("make verify", "s1")], usage(1, 1), uuid="sa1"),
        cc_tool_result("s1", "ok", 100, uuid="sr1"),
    ])
    _, out = cli.json(main, "--host", "claude-code")
    assert out["time"]["tool_s"] == 100
    assert out["time"]["active_s"] == 10
    sub = next(a for a in out["actors"] if a["actor"] == "subagent:agent-1")
    assert sub["tool_s"] == 90


def test_parallel_calls_are_merged_before_summing(cli, tmp_path):
    path = write_jsonl(tmp_path / "s1.jsonl", [
        cc_user("go", 0),
        cc_assistant("m1", 10, [bash("sleep 50", "c1"), bash("sleep 50", "c2")], usage(1, 1)),
        cc_tool_result("c1", "ok", 60),
        cc_tool_result("c2", "ok", 60),
        cc_assistant("m2", 70, usage=usage(1, 1)),
    ])
    _, out = cli.json(path, "--host", "claude-code")
    assert out["time"]["tool_s"] == 50
    assert out["time"]["active_s"] == 20


def test_context_ignores_id_less_records_and_subagents(cli, tmp_path):
    no_id = cc_assistant("x", 3, usage=usage(5, 0))
    del no_id["message"]["id"]
    main = write_jsonl(tmp_path / "s1.jsonl", [
        cc_user("go", 0, uuid="u1"),
        no_id,
        cc_assistant("m1", 5, usage=usage(10, 5, 150000, 0), uuid="a1"),
    ])
    write_jsonl(tmp_path / "s1" / "subagents" / "agent-1.jsonl", [
        cc_user("brief", 6, uuid="su1"),
        cc_assistant("sm1", 9, usage=usage(1, 1, 7, 0), uuid="sa1"),
    ])
    _, out = cli.json(main, "--host", "claude-code")
    assert out["tokens"]["context_at_invocation"] == 150010
