"""The default window sits between the previous reviewer invocation and the latest one, for every host's invocation form."""
import pytest
from fixtures import devin as dv
from fixtures.builders import (cc_assistant, cc_user, cu_assistant, cu_turn_ended, cu_user, cx_meta, cx_tokens, cx_user, usage,
                               write_jsonl)
from fixtures.cline import assistant as cl_assistant
from fixtures.cline import user_input, write_session

ATTACHED = ("<manually_attached_skills>\nSkill Name: workflow-reviewer\nPath: /x/.cursor/skills/workflow-reviewer/SKILL.md\n"
            "steps\n</manually_attached_skills>\n<user_query>\nreview it\n</user_query>")


def build(cli, tmp_path, host, spec):
    """A session whose turns follow spec: "work" or "review" (the host's own invocation form)."""
    if host == "claude-code":
        records = []
        for n, kind in enumerate(spec):
            records += [cc_user("/workflow-reviewer" if kind == "review" else f"task {n}", n * 100),
                        cc_assistant(f"m{n}", n * 100 + 10, usage=usage(1, 10))]
        return write_jsonl(tmp_path / "s.jsonl", records)
    if host == "codex":
        records = [cx_meta("i", cli.repo)]
        for n, kind in enumerate(spec):
            records += [cx_user("$workflow-reviewer" if kind == "review" else f"task {n}", n * 100 + 1), cx_tokens(1, 0, 10, n * 100 + 2)]
        return write_jsonl(tmp_path / "rollout-x.jsonl", records)
    if host == "cursor":
        records = []
        for n, kind in enumerate(spec):
            records += [cu_user(ATTACHED if kind == "review" else f"task {n}"), cu_assistant("ok"), cu_turn_ended()]
        return write_jsonl(tmp_path / "c.jsonl", records)
    if host == "devin":
        db = dv.DevinDb(tmp_path / "sessions.db")
        chain = db.session("sess-one", str(cli.repo.resolve()))
        chain.add(dv.system())
        for n, kind in enumerate(spec):
            chain.add(dv.user("expanded skill text", slash="/workflow-reviewer") if kind == "review" else dv.user(f"task {n}"),
                      dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
        chain.done()
        return db.path
    messages = []                                                       # cline
    for n, kind in enumerate(spec):
        messages += [user_input("use the workflow-reviewer skill" if kind == "review" else f"task {n}", 2 * n + 1, n * 10),
                     cl_assistant(2 * n + 2, n * 10 + 1)]
    return write_session(tmp_path / "sessions", "s1", cli.repo, messages)


HOSTS = ["claude-code", "codex", "cursor", "devin", "cline"]
W, R = "work", "review"


def window(cli, tmp_path, host, spec, *args):
    code, out = cli.json(build(cli, tmp_path, host, spec), "--host", host, *args)
    assert code == 0, out
    return [t["turn"] for t in out["turns"]], out


@pytest.mark.parametrize("host", HOSTS)
def test_one_invocation_at_the_end_reviews_everything_before_it(cli, tmp_path, host):
    turns, out = window(cli, tmp_path, host, [W, W, R])
    assert turns == [1, 2] and out["window"]["reviewer_invocation_found"] is True


@pytest.mark.parametrize("host", HOSTS)
def test_a_second_invocation_reviews_the_work_between_them(cli, tmp_path, host):
    turns, out = window(cli, tmp_path, host, [W, R, W, W, R])
    assert turns == [3, 4] and out["window"]["from_turn"] == 3


@pytest.mark.parametrize("host", HOSTS)
def test_a_straight_rerun_reviews_the_original_task_again(cli, tmp_path, host):
    turns, out = window(cli, tmp_path, host, [W, W, R, R])
    assert turns == [1, 2] and out["coverage"]["window_note"]


@pytest.mark.parametrize("host", HOSTS)
def test_no_invocation_means_the_whole_session(cli, tmp_path, host):
    turns, out = window(cli, tmp_path, host, [W, W, W])
    assert turns == [1, 2, 3] and out["window"]["reviewer_invocation_found"] is False


@pytest.mark.parametrize("host", HOSTS)
def test_work_after_the_only_invocation_is_not_in_the_window(cli, tmp_path, host):
    turns, _ = window(cli, tmp_path, host, [W, R, W, W])
    assert turns == [1]


@pytest.mark.parametrize("spec,expected", [
    ([W, R, W, R, R], [3]),                 # the re-run repeats the second window
    ([W, W, R, R, R], [1, 2]),
    ([W, R, R, W, W, R], [4, 5]),           # a re-run earlier in the chat does not matter once there is new work
    ([R, W], []),                           # nothing before the only invocation
])
def test_repeated_invocations_in_longer_chats(cli, tmp_path, spec, expected):
    turns, _ = window(cli, tmp_path, "claude-code", spec)
    assert turns == expected


@pytest.mark.parametrize("host", HOSTS)
def test_the_reviewer_turn_and_its_reply_are_never_in_a_window(cli, tmp_path, host):
    _, out = window(cli, tmp_path, host, [W, R, W, R])
    assert out["window"]["turns_total"] == 1 and out["actors"][0]["tool_calls"] == 0


@pytest.mark.parametrize("host", HOSTS)
def test_explicit_turn_bounds_override_the_rule(cli, tmp_path, host):
    turns, _ = window(cli, tmp_path, host, [W, R, W, W, R], "--from-turn", 1, "--to-turn", 4)
    assert turns == [1, 2, 3, 4]


def test_a_rerun_note_names_the_invocation_it_repeats(cli, tmp_path):
    _, out = window(cli, tmp_path, "claude-code", [W, W, R, R])
    assert "re-run" in out["coverage"]["window_note"] and "turn 3" in out["coverage"]["window_note"]


@pytest.mark.parametrize("host", HOSTS)
def test_list_turns_marks_the_reviewer_turns_and_names_the_default_window(cli, tmp_path, host):
    _, out = cli.json(build(cli, tmp_path, host, [W, R, W, W, R]), "--host", host, "--list-turns", "--all")
    assert [t["index"] for t in out["turns"]] == [1, 2, 3, 4, 5]
    assert [bool(t.get("reviewer")) for t in out["turns"]] == [False, True, False, False, True]
    assert out["default_window"] == {"from_turn": 3, "to_turn": 4}


def test_list_turns_with_no_invocation_has_no_marks_and_the_whole_session(cli, tmp_path):
    _, out = cli.json(build(cli, tmp_path, "claude-code", [W, W]), "--host", "claude-code", "--list-turns")
    assert not any(t.get("reviewer") for t in out["turns"]) and out["default_window"] == {"from_turn": 1, "to_turn": 2}
