"""The review reads nothing after the window: its own turn, and anything written while it runs, change no number or check."""
import json

import pytest
from fixtures.builders import (cc_assistant, cc_bash_call, cc_user, cx_line, cx_tokens, desktop, dx_assistant, dx_exec_cmd, dx_exec_output,
                               dx_meta, dx_task_started, dx_usage, dx_user, usage, write_jsonl)


def claude_session(in_progress):
    records = [cc_user("first task", 0), *cc_bash_call("m1", "c1", "python3 -m pytest -q", 2), cc_assistant("m2", 4, usage=usage(5, 20)),
               cc_user("second task", 10), *cc_bash_call("m3", "c2", "ls", 12), cc_assistant("m4", 14, usage=usage(5, 30)),
               cc_user("/workflow-reviewer", 20)]
    if in_progress:          # the review's own turn, still running: reads, a failing command, usage, a new record type, a cut line
        records += [*cc_bash_call("m5", "c3", "python3 transcript_metrics.py", 22, "boom", True), cc_assistant("m6", 24, usage=usage(900, 4000)),
                    {"type": "brand_new_tool_record", "timestamp": "2026-01-01T00:00:30Z"}, '{"type": "assistant", "message": {"id": "m7"']
    return records


def codex_session(in_progress):
    records = [dx_meta("p1", "/repo"),
               dx_task_started("t1", 1), dx_user("first task", 2), dx_exec_cmd("pytest -q", "k1", 3), dx_exec_output("k1", 4, "ok", 0),
               dx_usage("r1", 100, 0, 30, 5), dx_assistant("done", 6), cx_tokens(100, 0, 30, 7, total={"output_tokens": 30}),
               dx_task_started("t2", 8), dx_user("second task", 9), dx_usage("r2", 100, 0, 20, 10), dx_assistant("done", 11),
               cx_tokens(100, 0, 20, 12, total={"output_tokens": 50}),
               dx_task_started("t3", 13), dx_user("$workflow-reviewer", 14)]
    if in_progress:          # the running review: usage records whose running total lags behind
        records += [dx_exec_cmd("python3 transcript_metrics.py", "k9", 15), dx_exec_output("k9", 16, "x", 0),
                    dx_usage("r3", 5000, 0, 800, 17), cx_tokens(100, 0, 10, 18, total={"output_tokens": 60})]
    return desktop(records)


def run(cli, tmp_path, host, records, *args):
    path = write_jsonl(tmp_path / ("rollout-x.jsonl" if host == "codex" else "s.jsonl"), records)
    if host == "codex":
        records = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        records[0]["payload"]["cwd"] = str(cli.repo)
        write_jsonl(path, records)
    return cli.run(path, "--host", host, "--health-all", *args)


@pytest.mark.parametrize("host, build", [("claude-code", claude_session), ("codex", codex_session)])
@pytest.mark.parametrize("args", [(), ("--from-turn", "1", "--to-turn", "2"), ("--from-turn", "2", "--to-turn", "2")])
def test_records_after_the_window_change_nothing(cli, tmp_path, host, build, args):
    without = run(cli, tmp_path / "a", host, build(False), *args)
    with_extra = run(cli, tmp_path / "b", host, build(True), *args)
    assert without.returncode == 0, without.stdout
    assert with_extra.returncode == without.returncode and with_extra.stdout == without.stdout


def test_a_to_turn_ends_the_window_at_the_end_of_that_turn(cli, tmp_path):
    out = json.loads(run(cli, tmp_path, "claude-code", claude_session(True), "--to-turn", "1").stdout)
    assert out["window"]["turns_total"] == 1 and out["tools"]["total"] == 1 and out["tokens"]["output"] == 21


# ---- window-scoped second counts ---------------------------------------------------------------------------------------------------

def rows(out):
    return {r["metric"]: r for r in out["coverage"]["health"]["rows"]}


def tokens_check(out):
    return next(c for c in rows(out)["tokens"]["checks"] if c["layer"] == "second count")


def test_a_lagging_running_total_after_the_window_passes(cli, tmp_path):
    out = json.loads(run(cli, tmp_path, "codex", codex_session(True)).stdout)
    check = tokens_check(out)
    assert check["passed"] and check["first"] == check["second"] == 50 and rows(out)["tokens"]["status"] == "ok"


def test_a_window_that_starts_later_compares_only_its_own_part_of_the_running_total(cli, tmp_path):
    out = json.loads(run(cli, tmp_path, "codex", codex_session(True), "--from-turn", "2", "--to-turn", "2").stdout)
    check = tokens_check(out)
    assert check["passed"] and check["first"] == check["second"] == 20

