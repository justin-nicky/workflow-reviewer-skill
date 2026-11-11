"""One synthetic fixture per host shape and case: normal, empty, error, interrupted, subagent.

Shapes: Claude Code, Codex classic, Codex desktop, Cursor. A case a host cannot show (Cursor has no tool
results) asserts "not measurable" instead of a count.
"""
import json

import pytest
from fixtures.builders import (cc_assistant, cc_bash_call, cc_user, cu_assistant, cu_turn_ended, cu_turn_error, cu_user,
                               cx_aborted, cx_call, cx_meta, cx_output, cx_tokens, cx_user, desktop, dx_aborted,
                               dx_assistant, dx_exec_cmd, dx_exec_output, dx_meta, dx_spawn, dx_task_started, dx_usage,
                               dx_user, tool_use, usage, write_jsonl)

SHAPES = ["claude-code", "codex-classic", "codex-desktop", "cursor"]
HOST = {"claude-code": "claude-code", "codex-classic": "codex", "codex-desktop": "codex", "cursor": "cursor"}
VARIANT = {"claude-code": "default", "codex-classic": "classic", "codex-desktop": "desktop", "cursor": "default"}
LINK = {"claude-code": "tool_use_id", "codex-classic": "parent_thread_id", "codex-desktop": "parent_thread_id",
        "cursor": "markdown_link"}
SUB_ID = "00000000-0000-4000-8000-0000000000a1"
CC_CALL = "toolu_spawn01"


def run(cli, shape, *paths):
    return cli.json(*paths, "--host", HOST[shape])


# ---- builders: each returns the path(s) to pass ----------------------------------------------------------------

def build(shape, case, cli, tmp_path):
    return globals()[f"_{shape.replace('-', '_')}"](case, cli, tmp_path)


def _claude_code(case, cli, tmp_path):
    p = tmp_path / "s.jsonl"
    if case == "normal":
        return [write_jsonl(p, [cc_user("one", 0), *cc_bash_call("m1", "c1", "python3 -m pytest -q", 2),
                                cc_user("two", 20), cc_assistant("m2", 22, usage=usage(1, 5))])]
    if case == "empty":
        return [write_jsonl(p, [{"type": "file-history-snapshot", "timestamp": "2026-11-01T10:00:00.000Z"}])]
    if case == "error":
        return [write_jsonl(p, [cc_user("one", 0), *cc_bash_call("m1", "c1", "false", 2, "boom", True)])]
    if case == "interrupted":
        return [write_jsonl(p, [cc_user("one", 0), cc_assistant("m1", 2, usage=usage(1, 5)),
                                cc_user("[Request interrupted by user]", 5)])]
    main = write_jsonl(tmp_path / "proj" / "s1.jsonl", [
        cc_user("go", 0, uuid="u1"),
        cc_assistant("m1", 5, [tool_use("Agent", {"prompt": "check", "description": "d"}, CC_CALL)], usage(1, 10), uuid="a1")])
    sub_dir = tmp_path / "proj" / "s1" / "subagents"
    write_jsonl(sub_dir / "agent-abc.jsonl", [cc_user("check", 8, uuid="su"), cc_assistant("sm", 9, usage=usage(1, 7), uuid="sa")])
    (sub_dir / "agent-abc.meta.json").write_text(json.dumps({"toolUseId": CC_CALL, "pickedEffortModel": "claude-sub"}))
    return [main]


def _codex_classic(case, cli, tmp_path):
    p = tmp_path / "rollout-c.jsonl"
    head = [cx_meta("i", cli.repo)]
    if case == "normal":
        return [write_jsonl(p, head + [cx_user("one", 1), cx_call("python3 -m pytest -q", "k1", 2), cx_output("k1", "ok", 0, 3),
                                       cx_tokens(10, 4, 2, 4), cx_user("two", 5), cx_tokens(12, 4, 2, 6, total={"input_tokens": 99})])]
    if case == "empty":
        return [write_jsonl(p, head)]
    if case == "error":
        return [write_jsonl(p, head + [cx_user("one", 1), cx_call("false", "k1", 2), cx_output("k1", "boom", 2, 3), cx_tokens(1, 0, 1, 4)])]
    if case == "interrupted":
        return [write_jsonl(p, head + [cx_user("one", 1), cx_tokens(1, 0, 1, 2), cx_aborted("interrupted", 3)])]
    parent = write_jsonl(tmp_path / "rollout-parent.jsonl", [cx_meta("p1", cli.repo), cx_user("go", 1), cx_tokens(1, 0, 1, 2)])
    meta = cx_meta("s1", cli.repo)
    meta["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "p1"}}}
    sub = write_jsonl(tmp_path / "rollout-sub.jsonl", [meta, cx_user("brief", 3), cx_tokens(1, 0, 4, 4)])
    return [parent, sub]


def _codex_desktop(case, cli, tmp_path):
    p = tmp_path / "rollout-d.jsonl"
    head = [dx_meta("p1", cli.repo)]
    if case == "normal":
        return [write_jsonl(p, desktop(head + [dx_task_started("t1", 1), dx_user("one", 2), dx_exec_cmd("python3 -m pytest -q", "k1", 3),
                                               dx_exec_output("k1", 4, "ok", 0), dx_usage("resp_1", 10, 4, 2, 5),
                                               dx_task_started("t2", 6), dx_user("two", 7), dx_usage("resp_2", 12, 4, 2, 8)]))]
    if case == "empty":
        return [write_jsonl(p, desktop(head))]
    if case == "error":
        return [write_jsonl(p, desktop(head + [dx_task_started("t1", 1), dx_user("one", 2), dx_exec_cmd("false", "k1", 3),
                                               dx_exec_output("k1", 4, "boom", 2), dx_usage("resp_1", 1, 0, 1, 5)]))]
    if case == "interrupted":
        return [write_jsonl(p, desktop(head + [dx_task_started("t1", 1), dx_user("one", 2), dx_usage("resp_1", 1, 0, 1, 3),
                                               dx_aborted(4)]))]
    parent = write_jsonl(p, desktop(head + [dx_task_started("t1", 1), dx_user("go", 2), dx_spawn("sp1", 3, "/root/w", "do it"),
                                            dx_usage("resp_p", 1, 0, 1, 4)]))
    sub = write_jsonl(tmp_path / "rollout-sub.jsonl", desktop([
        dx_meta("s1", cli.repo, parent="p1", agent_path="/root/w"), dx_task_started("s-t1", 5), dx_user("do it", 6),
        dx_usage("resp_s", 1, 0, 4, 7)]))
    return [parent, sub]


def _cursor(case, cli, tmp_path):
    p = tmp_path / "c.jsonl"
    if case == "normal":
        return [write_jsonl(p, [cu_user("one"), cu_assistant("a", [("Shell", {"command": "python3 -m pytest -q"})]), cu_turn_ended(),
                                cu_user("two"), cu_assistant("b"), cu_turn_ended()])]
    if case == "empty":
        return [write_jsonl(p, [cu_turn_ended()])]
    if case == "error":
        return [write_jsonl(p, [cu_user("one"), cu_assistant("a", [("Shell", {"command": "false"})]), cu_turn_error("model_error")])]
    if case == "interrupted":
        return [write_jsonl(p, [cu_user("one"), cu_assistant("a"), cu_turn_error("user_stopped_generation")])]
    sid = tmp_path / "00000000-0000-4000-8000-0000000000b2"
    write_jsonl(sid / f"{sid.name}.jsonl", [cu_user("go"), cu_assistant(f"done: [Explore]({SUB_ID})"), cu_turn_ended()])
    write_jsonl(sid / "subagents" / f"{SUB_ID}.jsonl", [cu_user("explore"), cu_assistant("found"), cu_turn_ended()])
    return [sid]


# ---- the cases ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shape", SHAPES)
def test_normal(cli, tmp_path, shape):
    code, out = run(cli, shape, *build(shape, "normal", cli, tmp_path))
    assert code == 0 and out["coverage"]["variant"] == VARIANT[shape]
    assert out["coverage"]["drift"]["ok"] and out["window"]["turns_total"] == 2
    assert out["verify"]["total"] == 1 and out["tools"]["by_category"] == {"shell": 1}
    assert out["interruptions"].get("count", 0) == 0
    if shape == "cursor":
        assert out["tokens"] == {"not_measurable": "host records no token usage"}
    else:
        assert out["tokens"]["api_calls"] >= 2


@pytest.mark.parametrize("shape", SHAPES)
def test_empty(cli, tmp_path, shape):
    code, out = run(cli, shape, *build(shape, "empty", cli, tmp_path))
    assert code == 0
    assert out["window"]["turns_total"] == 0 and out["tools"]["total"] == 0 and out["verify"]["total"] == 0
    assert out["interruptions"].get("count", 0) == 0


@pytest.mark.parametrize("shape", SHAPES)
def test_error(cli, tmp_path, shape):
    code, out = run(cli, shape, *build(shape, "error", cli, tmp_path))
    assert code == 0
    if shape == "cursor":                                   # Cursor records no tool results
        assert out["tools"]["errors"] is None
        assert any("tool results" in n for n in out["coverage"]["not_measurable"])
        assert out["turn_errors"] == {"count": 1, "turns": [1]}
    else:
        assert out["tools"]["errors"] == 1


@pytest.mark.parametrize("shape", SHAPES)
def test_interrupted(cli, tmp_path, shape):
    code, out = run(cli, shape, *build(shape, "interrupted", cli, tmp_path))
    assert code == 0 and out["interruptions"] == {"count": 1, "turns": [1]}


@pytest.mark.parametrize("shape", SHAPES)
def test_subagent(cli, tmp_path, shape):
    code, out = run(cli, shape, *build(shape, "subagent", cli, tmp_path))
    assert code == 0 and [a["actor"].split(":")[0] for a in out["actors"]] == ["main", "subagent"]
    assert out["actors"][1]["link"] == LINK[shape]
    assert out["window"]["turns_total"] == 1               # a subagent's records never become main turns
