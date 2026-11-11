"""Codex Desktop (0.162.x) writes a different rollout shape; it is its own mapping variant."""
from fixtures.builders import (cx_meta, cx_tokens, cx_user, desktop, dx_agent_message, dx_assistant, dx_inter_agent,
                               dx_item_completed, dx_meta, dx_settings, dx_task_complete, dx_task_started,
                               dx_turn_context, dx_user, dx_world_state, write_jsonl)


def session(cli, tmp_path, *records, name="rollout-d.jsonl"):
    return write_jsonl(tmp_path / name, desktop([dx_meta("p1", cli.repo), *records]))


def basic(cli, tmp_path):
    return session(cli, tmp_path,
                   dx_task_started("t1", 1), dx_world_state(2), dx_turn_context(3),
                   dx_user("<environment_context>cwd</environment_context>", 4), dx_user("say hi", 5),
                   dx_item_completed(6), dx_assistant("hi", 7), dx_agent_message("note", 8), dx_inter_agent(9),
                   dx_settings(10), cx_tokens(100, 40, 5, 11), dx_task_complete("t1", 12))


def test_a_desktop_rollout_is_chosen_by_shape_and_fully_classified(cli, tmp_path):
    code, out = cli.json(basic(cli, tmp_path), "--host", "codex")
    cov = out["coverage"]
    assert code == 0 and cov["variant"] == "desktop"
    assert cov["drift"]["ok"] and cov["drift"]["coverage_pct"] == 100.0 and cov["drift"]["unseen_types"] == {}
    assert cov["variant_choice"]["scores"]["desktop"] > cov["variant_choice"]["scores"]["classic"]


def test_the_new_record_types_are_ignored_by_name(cli, tmp_path):
    _, out = cli.json(basic(cli, tmp_path), "--host", "codex")
    ignored = out["coverage"]["ignored_types"]
    for kind in ("world_state", "inter_agent_communication_metadata", "event_msg/item_completed",
                 "event_msg/thread_settings_applied"):
        assert ignored[kind] == 1, kind


def test_agent_messages_are_assistant_text(cli, tmp_path):
    _, out = cli.json(basic(cli, tmp_path), "--host", "codex", "--events", "0:20")
    rows = [e for e in out["events"] if e["text"] == "note"]
    assert rows and rows[0]["role"] == "assistant"


def test_the_classic_shape_still_picks_the_classic_variant(cli, tmp_path):
    path = write_jsonl(tmp_path / "rollout-c.jsonl", [cx_meta("i", cli.repo), cx_user("go", 1), cx_tokens(1, 0, 1, 2)])
    _, out = cli.json(path, "--host", "codex")
    assert out["coverage"]["variant"] == "classic" and out["coverage"]["drift"]["coverage_pct"] == 100.0


def test_a_tie_between_shapes_is_broken_by_the_version_range(cli, tmp_path):
    only_meta = write_jsonl(tmp_path / "rollout-m.jsonl", desktop([dx_meta("p1", cli.repo)]))
    _, out = cli.json(only_meta, "--host", "codex")
    assert out["coverage"]["variant"] == "desktop"
    assert out["coverage"]["variant_choice"]["reason"] == "version range tiebreak"


def test_desktop_version_is_a_tested_version_and_needs_no_note(cli, tmp_path):
    _, out = cli.json(basic(cli, tmp_path), "--host", "codex")
    assert out["coverage"]["transcript_version"] == "0.162.0-alpha.17.2"
    assert "0.162.0-alpha.17.2" in out["coverage"]["tested_versions"]
    assert out["coverage"]["version_note"] is None


# ---- turns: one per task_started ---------------------------------------------------------------

ENV = "<environment_context>cwd</environment_context>"
PAGE = "<external_codex_apps_open_page>page</external_codex_apps_open_page>"
SKILL = "<skill><name>hello</name><path>/x/SKILL.md</path>steps</skill>"


def test_a_turn_is_one_task_started_and_injected_context_is_counted_apart(cli, tmp_path):
    path = session(cli, tmp_path,
                   dx_task_started("t1", 1), dx_user(ENV, 2), dx_user(PAGE, 3), dx_user("first prompt", 4),
                   dx_assistant("a", 5), dx_task_complete("t1", 6),
                   dx_task_started("t2", 7), dx_user("[$hello](/x/SKILL.md) go", 8), dx_user(SKILL, 9),
                   dx_assistant("b", 10), dx_task_complete("t2", 11))
    _, out = cli.json(path, "--host", "codex")
    assert out["window"]["turns_total"] == 2
    assert [t["turn"] for t in out["turns"]] == [1, 2]
    assert out["window"]["non_turn_user_records"] == {
        "prefix:<environment_context>": 1, "prefix:<external_codex_apps_open_page>": 1, "prefix:<skill>": 1}


def test_a_task_started_without_a_prompt_still_opens_a_turn(cli, tmp_path):
    path = session(cli, tmp_path,
                   dx_task_started("t1", 1), dx_user("one", 2), dx_assistant("a", 3),
                   dx_task_started("t2", 4), dx_assistant("woken by an agent", 5),
                   dx_task_started("t3", 6), dx_user("three", 7), dx_assistant("c", 8))
    _, out = cli.json(path, "--host", "codex")
    assert out["window"]["turns_total"] == 3


def test_turn_previews_show_the_prompt_not_the_injected_context(cli, tmp_path):
    path = session(cli, tmp_path, dx_task_started("t1", 1), dx_user(ENV, 2), dx_user("fix the bug", 3),
                   dx_assistant("ok", 4))
    _, out = cli.json(path, "--host", "codex", "--list-turns")
    assert [t["preview"] for t in out["turns"]] == ["fix the bug"]


def test_the_classic_variant_still_counts_user_messages(cli, tmp_path):
    path = write_jsonl(tmp_path / "rollout-c.jsonl", [cx_meta("i", cli.repo), cx_user("a", 1), cx_user("b", 2)])
    _, out = cli.json(path, "--host", "codex")
    assert out["window"]["turns_total"] == 2


def test_the_reviewer_invocation_turn_is_cut_whole_including_its_task_started(cli, tmp_path):
    path = session(cli, tmp_path,
                   dx_task_started("t1", 1), dx_user("build it", 2), dx_assistant("done", 3),
                   dx_task_started("t2", 4), dx_user(ENV, 5), dx_user("$workflow-reviewer", 6), dx_assistant("x", 7))
    _, out = cli.json(path, "--host", "codex")
    assert out["window"]["reviewer_invocation_found"] and out["window"]["turns_total"] == 1


# ---- commands: JavaScript sent to the exec tool ------------------------------------------------

from fixtures.builders import dx_exec, dx_exec_cmd, dx_exec_output  # noqa: E402


def run_cmds(cli, tmp_path, *pairs):
    """pairs: (exec record, output record) in order, inside one turn."""
    records = [dx_task_started("t1", 1), dx_user("go", 2)]
    for pair in pairs:
        records += list(pair)
    return cli.json(session(cli, tmp_path, *records, dx_assistant("done", 90)), "--host", "codex")[1]


def test_exec_javascript_yields_its_cmd_as_a_shell_command(cli, tmp_path):
    out = run_cmds(cli, tmp_path,
                   (dx_exec_cmd("ls -la", "c1", 3), dx_exec_output("c1", 4, "x")),
                   (dx_exec_cmd("python3 -m pytest -q", "c2", 5), dx_exec_output("c2", 6, "1 passed", 0)))
    assert out["tools"]["by_category"] == {"shell": 2}
    assert out["verify"]["total"] == 1 and out["verify"]["by_kind"] == {"test": 1}


def test_a_cmd_with_escaped_quotes_and_newlines_is_decoded(cli, tmp_path):
    cmd = 'python3 -c "print(1)"\npytest -q'
    out = run_cmds(cli, tmp_path, (dx_exec_cmd(cmd, "c1", 3), dx_exec_output("c1", 4)))
    _, ev = cli.json(session(cli, tmp_path / "e", dx_task_started("t1", 1), dx_exec_cmd(cmd, "c1", 3),
                             dx_exec_output("c1", 4)), "--host", "codex", "--events", "0:9")
    call = next(c for e in ev["events"] for c in e["calls"])
    assert call["command"] == cmd and out["verify"]["total"] == 1


def test_an_exec_without_a_cmd_is_a_tool_call_but_not_a_command(cli, tmp_path):
    patch = dx_exec('const patch = "*** Begin Patch";\ntext(await tools.apply_patch(patch));\n', "c1", 3)
    out = run_cmds(cli, tmp_path, (patch, dx_exec_output("c1", 4, "{}")),
                   (dx_exec_cmd("ls", "c2", 5), dx_exec_output("c2", 6)))
    assert out["tools"]["total"] == 2 and out["tools"]["by_category"] == {"other": 1, "shell": 1}


def test_exit_codes_come_from_the_output_text_and_nonzero_is_an_error(cli, tmp_path):
    out = run_cmds(cli, tmp_path,
                   (dx_exec_cmd("true", "c1", 3), dx_exec_output("c1", 4, "", 0)),
                   (dx_exec_cmd("python3 missing.py", "c2", 5), dx_exec_output("c2", 6, "no such file", 2)),
                   (dx_exec_cmd("ls", "c3", 7, report_exit=False), dx_exec_output("c3", 8, "a b")))
    assert out["tools"]["errors"] == 1
    assert out["tools"]["largest_outputs"][0]["bytes"] > 0


def test_a_single_quoted_cmd_is_read_too(cli, tmp_path):
    js = "const r = await tools.exec_command({cmd:'echo \\'hi\\' && pytest -q'});\ntext(r.output);\n"
    out = run_cmds(cli, tmp_path, (dx_exec(js, "c1", 3), dx_exec_output("c1", 4)))
    assert out["tools"]["by_category"] == {"shell": 1} and out["verify"]["total"] == 1


# ---- tokens: token_usage_record, one per response -----------------------------------------------

from fixtures.builders import dx_usage  # noqa: E402


def token_session(cli, tmp_path, *usage_records, with_counts=()):
    return cli.json(session(cli, tmp_path, dx_task_started("t1", 1), dx_user("go", 2), dx_assistant("a", 3),
                            *usage_records, *with_counts), "--host", "codex")[1]["tokens"]


def test_token_usage_records_are_summed_with_cache_split_out(cli, tmp_path):
    tokens = token_session(cli, tmp_path, dx_usage("resp_a", 1000, 700, 50, 4), dx_usage("resp_b", 1500, 1200, 30, 5))
    assert (tokens["fresh_input"], tokens["cache_read"], tokens["output"], tokens["api_calls"]) == (600, 1900, 80, 2)
    assert tokens["calls_without_id"] == 0


def test_a_response_id_seen_twice_is_counted_once(cli, tmp_path):
    tokens = token_session(cli, tmp_path, dx_usage("resp_a", 1000, 700, 50, 4), dx_usage("resp_a", 1000, 700, 50, 5))
    assert (tokens["fresh_input"], tokens["cache_read"], tokens["output"], tokens["api_calls"]) == (300, 700, 50, 1)


def test_token_count_is_used_only_when_there_are_no_usage_records(cli, tmp_path):
    from fixtures.builders import cx_tokens
    counts = (cx_tokens(1000, 700, 50, 4, total={"input_tokens": 1000}),
              cx_tokens(1500, 1200, 30, 5, total={"input_tokens": 2500}))
    fallback = token_session(cli, tmp_path, with_counts=counts)
    assert (fallback["fresh_input"], fallback["cache_read"], fallback["output"]) == (600, 1900, 80)
    both = token_session(cli, tmp_path / "b", dx_usage("resp_a", 1000, 700, 50, 4), dx_usage("resp_b", 1500, 1200, 30, 5),
                         with_counts=counts)
    assert (both["fresh_input"], both["cache_read"], both["output"], both["api_calls"]) == (600, 1900, 80, 2)


def test_usage_records_add_up_to_the_final_token_count_total(cli, tmp_path):
    from fixtures.builders import cx_tokens
    final = {"input_tokens": 2500, "cached_input_tokens": 1900, "output_tokens": 80}
    tokens = token_session(cli, tmp_path, dx_usage("resp_a", 1000, 700, 50, 4), dx_usage("resp_b", 1500, 1200, 30, 5),
                           with_counts=(cx_tokens(1500, 1200, 30, 6, total=final),))
    assert tokens["fresh_input"] + tokens["cache_read"] == final["input_tokens"]
    assert tokens["cache_read"] == final["cached_input_tokens"] and tokens["output"] == final["output_tokens"]
