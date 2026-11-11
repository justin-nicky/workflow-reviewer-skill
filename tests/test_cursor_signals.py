"""What Cursor transcripts can show: interruptions, turn errors, coarse time, subagent links, skill reads."""
from fixtures.builders import (cu_assistant, cu_turn_ended, cu_turn_error, cu_user, write_jsonl)


def run(cli, tmp_path, *records, name="c.jsonl"):
    return cli.json(write_jsonl(tmp_path / name, list(records)), "--host", "cursor")[1]


def test_user_stopped_generation_is_an_interruption_in_that_turn(cli, tmp_path):
    out = run(cli, tmp_path, cu_user("one"), cu_assistant("a"), cu_turn_ended(),
              cu_user("two"), cu_assistant("b"), cu_turn_error("user_stopped_generation"))
    assert out["interruptions"] == {"count": 1, "turns": [2]}
    assert out["turn_errors"] == {"count": 0, "turns": []}


def test_other_turn_errors_are_turn_errors_not_interruptions(cli, tmp_path):
    out = run(cli, tmp_path, cu_user("one"), cu_assistant("a"), cu_turn_error("model_overloaded"),
              cu_user("two"), cu_assistant("b"), cu_turn_error("user_stopped_generation"))
    assert out["interruptions"] == {"count": 1, "turns": [2]}
    assert out["turn_errors"] == {"count": 1, "turns": [1]}


def test_cursor_tool_errors_stay_not_measurable(cli, tmp_path):
    out = run(cli, tmp_path, cu_user("one"), cu_assistant("a", [("Shell", {"command": "false"})]), cu_turn_error("boom"))
    assert out["tools"]["errors"] is None
    assert any("tool results" in n for n in out["coverage"]["not_measurable"])


def test_hosts_without_turn_error_records_do_not_get_the_key(cli, tmp_path):
    from fixtures.builders import cc_assistant, cc_user, usage
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), cc_assistant("m", 1, usage=usage(1, 1))])
    _, out = cli.json(path, "--host", "claude-code")
    assert "turn_errors" not in out
