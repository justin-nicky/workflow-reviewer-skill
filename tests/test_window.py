from datetime import datetime, timedelta, timezone

from fixtures.builders import (bash, cc_assistant, cc_bash_call, cc_tool_result, cc_user,
                               usage, write_jsonl)

REVIEWER = "<command-message>workflow-reviewer</command-message> /workflow-reviewer"


def three_turns(tmp_path, with_reviewer=True):
    records = [
        cc_user("add rate limiting to /login", 0),
        cc_assistant("m1", 10, usage=usage(1, 100)),
        cc_user("use Redis, not in-memory", 100),
        cc_assistant("m2", 110, usage=usage(1, 200)),
        cc_user("tests fail on CI, fix " + "x" * 120, 200),
        cc_assistant("m3", 210, usage=usage(1, 400)),
    ]
    if with_reviewer:
        records += [cc_user(REVIEWER, 300), cc_assistant("m4", 310, usage=usage(1, 800))]
    return write_jsonl(tmp_path / "s1.jsonl", records)


def test_window_ends_at_the_reviewers_own_invocation(cli, tmp_path):
    code, out = cli.json(three_turns(tmp_path), "--host", "claude-code")
    assert out["tokens"]["output"] == 700
    assert out["window"]["reviewer_invocation_found"] is True
    assert len(out["turns"]) == 3


def test_from_turn_starts_the_window_later(cli, tmp_path):
    _, out = cli.json(three_turns(tmp_path), "--host", "claude-code", "--from-turn", "2")
    assert out["tokens"]["output"] == 600
    assert [t["turn"] for t in out["turns"]] == [2, 3]


def test_list_turns_prints_index_time_and_an_80_char_preview(cli, tmp_path):
    _, out = cli.json(three_turns(tmp_path), "--host", "claude-code", "--list-turns")
    turns = out["turns"]
    assert [t["index"] for t in turns] == [1, 2, 3]
    assert turns[0]["preview"] == "add rate limiting to /login"
    assert len(turns[2]["preview"]) == 80
    assert turns[0]["time"].startswith("2026-11-01T10:00:00")


def test_per_turn_breakdown(cli, tmp_path):
    _, out = cli.json(three_turns(tmp_path), "--host", "claude-code")
    assert [t["tokens"]["output"] for t in out["turns"]] == [100, 200, 400]


def test_missing_reviewer_invocation_runs_to_end_of_file(cli, tmp_path):
    _, out = cli.json(three_turns(tmp_path, with_reviewer=False), "--host", "claude-code")
    assert out["window"]["reviewer_invocation_found"] is False
    assert out["tokens"]["output"] == 700


def test_truncated_final_line_is_ignored(cli, tmp_path):
    path = write_jsonl(tmp_path / "s1.jsonl", [
        cc_user("go", 0), cc_assistant("m1", 5, usage=usage(1, 100)),
        cc_assistant("m2", 9, usage=usage(1, 100)),
    ], truncate_last=True)
    code, out = cli.json(path, "--host", "claude-code")
    assert code == 0
    assert out["coverage"]["truncated_last_line"] is True
    assert out["tokens"]["output"] == 100


def test_lag_note_when_last_record_is_recent(cli, tmp_path):
    now = datetime.now(timezone.utc)
    rec = cc_user("go", 0)
    rec["timestamp"] = (now - timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    path = write_jsonl(tmp_path / "s1.jsonl", [rec])
    _, out = cli.json(path, "--host", "claude-code")
    assert "latest turn may be incomplete" in out["coverage"]["lag_note"]
    assert out["coverage"]["last_record_ts"].startswith(str(now.year))


def test_no_lag_note_for_an_old_transcript(cli, tmp_path):
    _, out = cli.json(three_turns(tmp_path), "--host", "claude-code")
    assert out["coverage"]["lag_note"] is None


def test_time_split_into_active_waiting_and_tool(cli, tmp_path):
    path = write_jsonl(tmp_path / "s1.jsonl", [
        cc_user("go", 0),
        cc_assistant("m1", 5, [bash("sleep", "c1")], usage(1, 1)),
        cc_tool_result("c1", "done", 65),
        cc_assistant("m2", 70, usage=usage(1, 1)),
        cc_user("now the second thing", 400),
        cc_assistant("m3", 410, usage=usage(1, 1)),
    ])
    _, out = cli.json(path, "--host", "claude-code")
    t = out["time"]
    assert t["wall_s"] == 410
    assert t["waiting_s"] == 330
    assert t["tool_s"] == 60
    assert t["active_s"] == 20
    assert "permission" in t["tool_note"]


def test_idle_threshold_is_configurable(cli, tmp_path):
    path = write_jsonl(tmp_path / "s1.jsonl", [
        cc_user("go", 0), cc_assistant("m1", 5, usage=usage(1, 1)),
        cc_user("again", 100), cc_assistant("m2", 105, usage=usage(1, 1)),
    ])
    _, default = cli.json(path, "--host", "claude-code")
    _, tight = cli.json(path, "--host", "claude-code", "--idle-threshold", "60")
    assert default["time"]["waiting_s"] == 0
    assert tight["time"]["waiting_s"] == 95


def test_events_window_is_normalised(cli, tmp_path):
    path = write_jsonl(tmp_path / "s1.jsonl", [
        cc_user("go", 0),
        *cc_bash_call("m1", "c1", "pytest -q", 5),
    ])
    _, out = cli.json(path, "--host", "claude-code", "--events", "1:2")
    assert [e["i"] for e in out["events"]] == [1, 2]
    assert out["events"][0]["calls"][0]["name"] == "Bash"
    assert out["events"][0]["calls"][0]["command"] == "pytest -q"


def test_sample_prints_n_raw_lines(cli, tmp_path):
    _, out = cli.json(three_turns(tmp_path), "--host", "claude-code", "--sample", "2")
    assert len(out["sample"]) == 2
    assert out["sample"][0].startswith("{")
