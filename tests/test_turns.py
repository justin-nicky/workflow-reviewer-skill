import json

from fixtures.builders import (bash, cc_assistant, cc_tool_result, cc_user, usage, write_jsonl)
from test_window import three_turns

SLASH = "<command-message>tdd</command-message>\n<command-name>/tdd</command-name>"
REVIEWER = "<command-message>workflow-reviewer</command-message>\n<command-name>/workflow-reviewer</command-name>"

INJECTED = {
    "isMeta flag": dict(text="<local-command-caveat>Caveat: ignore</local-command-caveat>", isMeta=True),
    "meta image marker": dict(text="[Image #1]", isMeta=True),
    "compaction summary": dict(text="This session is being continued from a previous conversation.",
                               isCompactSummary=True),
    "transcript-only": dict(text="Summary shown to the user only", isVisibleInTranscriptOnly=True),
    "local command output": dict(text="<local-command-stdout>done</local-command-stdout>"),
    "interruption": dict(text="[Request interrupted by user for tool use]"),
    "task notification": dict(text="<task-notification>build finished</task-notification>"),
    "system reminder": dict(text="<system-reminder>be careful</system-reminder>"),
    "user shell": dict(text="<bash-input>ls</bash-input>"),
}


def transcript(tmp_path, extra_records, tail_t=1000):
    records = [
        cc_user("real task one", 0),
        cc_assistant("m1", 10, [bash("pytest", "c1")], usage(1, 100)),
        *extra_records,
        cc_tool_result("c1", "1 failed", 12, is_error=True),
        cc_user(SLASH, tail_t),
        cc_assistant("m2", tail_t + 5, usage=usage(1, 200)),
        cc_user("real task two", tail_t + 100),
        cc_assistant("m3", tail_t + 105, usage=usage(1, 400)),
    ]
    return write_jsonl(tmp_path / "s.jsonl", records)


def test_each_kind_of_injected_record_is_not_a_turn(cli, tmp_path):
    for kind, spec in INJECTED.items():
        spec = dict(spec)
        text = spec.pop("text")
        path = transcript(tmp_path, [cc_user(text, 11, **spec)])
        _, out = cli.json(path, "--host", "claude-code", "--list-turns")
        previews = [t["preview"] for t in out["turns"]]
        assert len(previews) == 3, (kind, previews)
        assert previews[1].startswith("<command-message>tdd"), kind     # a real slash command is a turn


def test_turns_numbering_and_from_turn_ignore_injected_records(cli, tmp_path):
    extras = [cc_user("[Request interrupted by user]", 11), cc_user("<task-notification>x</task-notification>", 13),
              cc_user("summary", 14, isCompactSummary=True)]
    path = transcript(tmp_path, extras)
    _, out = cli.json(path, "--host", "claude-code", "--from-turn", "2")
    assert [t["turn"] for t in out["turns"]] == [2, 3]
    assert out["tokens"]["output"] == 600


def test_tool_results_stay_attached_to_the_main_turn(cli, tmp_path):
    path = transcript(tmp_path, [cc_user("<local-command-stdout>x</local-command-stdout>", 11)])
    _, out = cli.json(path, "--host", "claude-code")
    assert out["tools"]["errors"] == 1
    assert out["turns"][0]["tool_calls"] == 1


def test_waiting_time_ignores_injected_records(cli, tmp_path):
    extras = [cc_user("<task-notification>x</task-notification>", 200),
              cc_user("[Request interrupted by user]", 210)]
    path = transcript(tmp_path, extras, tail_t=400)
    _, out = cli.json(path, "--host", "claude-code")
    # waiting = last agent/tool activity (t=12) to the slash command (t=400) = 388
    assert out["time"]["waiting_s"] == 388


def test_non_turn_records_are_counted_by_reason(cli, tmp_path):
    extras = [cc_user("<task-notification>a</task-notification>", 11),
              cc_user("<task-notification>b</task-notification>", 11),
              cc_user("[Request interrupted by user]", 11),
              cc_user("caveat", 11, isMeta=True),
              cc_user("sum", 11, isCompactSummary=True)]
    _, out = cli.json(transcript(tmp_path, extras), "--host", "claude-code")
    assert out["window"]["non_turn_user_records"] == {
        "prefix:<task-notification>": 2, "prefix:[Request interrupted by user": 1,
        "flag:isMeta": 1, "flag:isCompactSummary": 1}


def test_the_reviewers_own_slash_command_and_its_expansion_end_the_window(cli, tmp_path):
    records = [
        cc_user("real task", 0), cc_assistant("m1", 10, usage=usage(1, 100)),
        cc_user(REVIEWER, 300),
        cc_user("Base directory for this skill: /x/skills/workflow-reviewer", 301, isMeta=True),
        cc_assistant("m2", 310, usage=usage(1, 999)),
    ]
    _, out = cli.json(write_jsonl(tmp_path / "s.jsonl", records), "--host", "claude-code")
    assert out["window"]["reviewer_invocation_found"] is True
    assert out["tokens"]["output"] == 100
    assert len(out["turns"]) == 1


def test_a_prompt_that_only_mentions_the_slash_command_does_not_end_the_window(cli, tmp_path):
    records = [
        cc_user("later I will run /workflow-reviewer on this", 0), cc_assistant("m1", 10, usage=usage(1, 100)),
        cc_user("also see skills docs for the workflow-reviewer skill", 50), cc_assistant("m2", 60, usage=usage(1, 200)),
    ]
    _, out = cli.json(write_jsonl(tmp_path / "s.jsonl", records), "--host", "claude-code")
    assert out["window"]["reviewer_invocation_found"] is False
    assert out["tokens"]["output"] == 300


def test_a_plain_slash_command_prompt_ends_the_window(cli, tmp_path):
    records = [cc_user("real task", 0), cc_assistant("m1", 10, usage=usage(1, 100)),
               cc_user("/workflow-reviewer", 50), cc_assistant("m2", 60, usage=usage(1, 999))]
    _, out = cli.json(write_jsonl(tmp_path / "s.jsonl", records), "--host", "claude-code")
    assert out["window"]["reviewer_invocation_found"] is True
    assert out["tokens"]["output"] == 100


def audited_twice(tmp_path):
    return write_jsonl(tmp_path / "s.jsonl", [
        cc_user("first task", 0), cc_assistant("m1", 10, usage=usage(1, 100)),
        cc_user("/workflow-reviewer", 100),                       # turn 2: an earlier audit
        cc_assistant("m2", 110, usage=usage(1, 5000)),
        cc_user("second task", 200), cc_assistant("m3", 210, usage=usage(1, 200)),
        cc_user("third task", 300), cc_assistant("m4", 310, usage=usage(1, 300)),
        cc_user("/workflow-reviewer", 400),                       # the current audit
        cc_assistant("m5", 410, usage=usage(1, 9999)),
    ])


def test_default_window_starts_after_an_earlier_review(cli, tmp_path):
    _, out = cli.json(audited_twice(tmp_path), "--host", "claude-code")
    assert out["tokens"]["output"] == 500                           # second and third task only
    assert [t["turn"] for t in out["turns"]] == [3, 4]
    assert out["window"]["from_turn"] == 3
    assert "turn 2" in out["coverage"]["window_note"]
    assert "--from-turn" in out["coverage"]["window_note"]


def test_from_turn_overrides_the_default_start(cli, tmp_path):
    _, out = cli.json(audited_twice(tmp_path), "--host", "claude-code", "--from-turn", "1")
    assert out["window"]["from_turn"] == 1
    assert out["tokens"]["output"] == 5600
    assert out["coverage"]["window_note"] is None


def test_no_note_when_there_was_no_earlier_review(cli, tmp_path):
    _, out = cli.json(three_turns(tmp_path), "--host", "claude-code")
    assert out["window"]["from_turn"] == 1
    assert out["coverage"]["window_note"] is None


def interrupted(tmp_path):
    return write_jsonl(tmp_path / "s.jsonl", [
        cc_user("task one", 0), cc_user("[Request interrupted by user]", 5),
        cc_assistant("m1", 10, usage=usage(1, 1)),
        cc_user("task two", 100), cc_assistant("m2", 110, usage=usage(1, 1)),
        cc_user("task three", 200),
        cc_user("[Request interrupted by user for tool use]", 205),
        cc_user("[Request interrupted by user for tool use]", 210),
        cc_assistant("m3", 220, usage=usage(1, 1)),
    ])


def test_interruptions_are_counted_with_the_turn_each_falls_in(cli, tmp_path):
    _, out = cli.json(interrupted(tmp_path), "--host", "claude-code")
    assert out["interruptions"] == {"count": 3, "turns": [1, 3, 3]}


def test_interruptions_outside_the_window_are_not_counted(cli, tmp_path):
    _, out = cli.json(interrupted(tmp_path), "--host", "claude-code", "--from-turn", "2")
    assert out["interruptions"] == {"count": 2, "turns": [3, 3]}


def test_zero_interruptions_is_reported_as_zero(cli, tmp_path):
    _, out = cli.json(write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0)]), "--host", "claude-code")
    assert out["interruptions"] == {"count": 0, "turns": []}


def test_hosts_without_a_known_marker_say_so(cli, tmp_path):
    mapping = ('host = "x"\ntype_path = "kind"\n[capabilities]\nhas_tokens = false\n'
               '[[records]]\nmatch = { kind = "u" }\nrole = "user"\ntext = ["t"]\n')
    path = write_jsonl(tmp_path / "x.jsonl", [{"kind": "u", "t": "go"}])
    _, out = cli.json(path, "--host", "cursor", "--mapping", "-", stdin=mapping)
    assert "not_measurable" in out["interruptions"]


def test_prompts_that_mention_the_skill_path_are_not_invocations(cli, tmp_path):
    records = [
        cc_user("task one", 0), cc_assistant("m1", 10, usage=usage(1, 100)),
        cc_user("symlink skills/workflow-reviewer into ~/.claude/skills and check it", 100),
        cc_assistant("m2", 110, usage=usage(1, 200)),
        cc_user("task three", 200), cc_assistant("m3", 210, usage=usage(1, 300)),
    ]
    _, out = cli.json(write_jsonl(tmp_path / "s.jsonl", records), "--host", "claude-code")
    assert out["window"]["reviewer_invocation_found"] is False
    assert out["tokens"]["output"] == 600


def test_a_mention_does_not_move_the_default_window_start(cli, tmp_path):
    records = [
        cc_user("task one", 0), cc_assistant("m1", 10, usage=usage(1, 100)),
        cc_user("copy skills/workflow-reviewer somewhere", 100), cc_assistant("m2", 110, usage=usage(1, 200)),
        cc_user("/workflow-reviewer", 200), cc_assistant("m3", 210, usage=usage(1, 999)),
    ]
    _, out = cli.json(write_jsonl(tmp_path / "s.jsonl", records), "--host", "claude-code")
    assert out["window"]["from_turn"] == 1
    assert out["coverage"]["window_note"] is None
    assert out["tokens"]["output"] == 300


def test_codex_dollar_syntax_invocation_ends_the_window(cli, tmp_path):
    from fixtures.builders import cx_meta, cx_tokens, cx_user
    records = [cx_meta("i", cli.repo), cx_user("real task", 1), cx_tokens(1, 0, 100, 2),
               cx_user("$workflow-reviewer", 50), cx_tokens(1, 0, 999, 51)]
    _, out = cli.json(write_jsonl(tmp_path / "rollout-x.jsonl", records), "--host", "codex")
    assert out["window"]["reviewer_invocation_found"] is True
    assert out["tokens"]["output"] == 100


# ---- subagent records never create main turns, on any host ------------------------------------

def cursor_session(tmp_path):
    from fixtures.builders import cu_assistant, cu_turn_ended, cu_user
    sid = tmp_path / "abc123"
    main = write_jsonl(sid / "abc123.jsonl", [
        cu_user("first task"), cu_assistant("ok"), cu_turn_ended(),
        cu_user("second task"),
        cu_assistant(calls=[("CallDynamicTool", {"toolName": "Task", "prompt": "run the checks please"})]),
        cu_turn_ended(),
    ])
    sub = write_jsonl(sid / "subagents" / "sub-1.jsonl", [
        cu_user("run the checks please"), cu_assistant(calls=[("Shell", {"command": "npm test"})])])
    return main, sub


def assert_two_main_turns(out):
    assert out["window"]["turns_total"] == 2
    assert out["verify"]["total"] == 1
    assert [a["actor"] for a in out["actors"]] == ["main", "subagent:sub-1"]


def test_cursor_subagent_brief_is_not_a_main_turn_however_the_files_are_passed(cli, tmp_path):
    main, sub = cursor_session(tmp_path)
    for args in ([main], [main, sub], [sub, main], [main.parent]):
        _, out = cli.json(*args, "--host", "cursor")
        assert_two_main_turns(out)


def test_cursor_subagent_twin_outside_the_subagents_folder_is_still_not_a_main_turn(cli, tmp_path):
    main, sub = cursor_session(tmp_path)
    twin = write_jsonl(tmp_path / "twin.jsonl", [json.loads(l) for l in sub.read_text().splitlines()])
    for args in ([main, twin], [twin, main]):
        _, out = cli.json(*args, "--host", "cursor")
        assert out["window"]["turns_total"] == 2, args
        assert out["verify"]["total"] == 1


def test_claude_code_subagent_file_passed_explicitly_is_not_a_main_turn(cli, tmp_path):
    main = write_jsonl(tmp_path / "proj" / "s1.jsonl", [
        cc_user("first task", 0, uuid="u1"), cc_assistant("m1", 1, usage=usage(1, 1), uuid="a1"),
        cc_user("second task", 20, uuid="u2"), cc_assistant("m2", 21, usage=usage(1, 1), uuid="a2")])
    sub = write_jsonl(tmp_path / "proj" / "s1" / "subagents" / "sub-1.jsonl", [
        cc_user("sub brief", 22, uuid="su"), cc_assistant("sm", 23, usage=usage(1, 5), uuid="sa")])
    for args in ([main], [main, sub], [sub, main]):
        _, out = cli.json(*args, "--host", "claude-code")
        assert out["window"]["turns_total"] == 2, args
        assert out["tokens"]["output"] == 7, args           # the subagent counted once


def test_codex_subagent_rollout_is_not_a_main_turn(cli, tmp_path):
    from fixtures.builders import cx_line, cx_meta, cx_tokens, cx_user
    main = write_jsonl(tmp_path / "rollout-main.jsonl", [
        cx_meta("p", cli.repo), cx_user("first task", 1), cx_user("second task", 20), cx_tokens(1, 0, 1, 21)])
    sub_meta = cx_line("session_meta", {"id": "c", "cwd": str(cli.repo), "cli_version": "0.142.2",
                                        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}}, 22)
    sub = write_jsonl(tmp_path / "rollout-sub.jsonl", [sub_meta, cx_user("sub brief", 23), cx_tokens(1, 0, 5, 24)])
    for args in ([main], [main, sub], [sub, main]):
        _, out = cli.json(*args, "--host", "codex")
        assert out["window"]["turns_total"] == 2, args
