from fixtures.builders import (bash, cc_assistant, cc_tool_result, cc_user, usage, write_jsonl)

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
