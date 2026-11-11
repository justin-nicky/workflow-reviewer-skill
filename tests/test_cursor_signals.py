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


# ---- coarse time from prompt timestamps ------------------------------------------------------------

LABEL = "coarse: prompt timestamps only, minute precision; last turn's duration unknown"


def stamped(text, clock, tz="UTC+4", day="Wednesday, Nov 11, 2026"):
    return cu_user(f"<timestamp>{day}, {clock} ({tz})</timestamp>\n<user_query>\n{text}\n</user_query>")


def test_wall_time_runs_from_the_first_prompt_to_the_last(cli, tmp_path):
    out = run(cli, tmp_path, stamped("a", "11:40 AM"), cu_assistant("x"), cu_turn_ended(),
              stamped("b", "11:41 AM"), cu_assistant("y"), cu_turn_ended(),
              stamped("c", "11:43 AM"), cu_assistant("z"), cu_turn_ended())
    t = out["time"]
    assert t["wall_s"] == 180 and t["precision"] == LABEL
    assert "not_measurable" in t["tool_s"] and "not_measurable" in t["waiting_s"]


def test_pm_and_midnight_are_read_correctly(cli, tmp_path):
    out = run(cli, tmp_path, stamped("a", "11:58 PM"), cu_assistant("x"), stamped("b", "12:02 AM", day="Thursday, Nov 12, 2026"))
    assert out["time"]["wall_s"] == 240


def test_a_single_or_unstamped_prompt_is_not_measurable(cli, tmp_path):
    assert "not_measurable" in run(cli, tmp_path, stamped("a", "11:40 AM"), cu_assistant("x"))["time"]
    assert "not_measurable" in run(cli, tmp_path / "b", cu_user("plain"), cu_user("plain two"))["time"]


def test_only_the_part_that_is_measurable_leaves_the_not_measurable_list(cli, tmp_path):
    out = run(cli, tmp_path, stamped("a", "11:40 AM"), stamped("b", "11:41 AM"))
    listed = out["coverage"]["not_measurable"]
    assert not any(n.startswith("time ") for n in listed)
    assert any("tool time and waiting time" in n for n in listed)
