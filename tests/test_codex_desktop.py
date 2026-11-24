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


# ---- interruptions -----------------------------------------------------------------------------

from fixtures.builders import dx_aborted  # noqa: E402


def test_an_interrupted_turn_is_counted_with_its_turn_number(cli, tmp_path):
    path = session(cli, tmp_path, dx_task_started("t1", 1), dx_user("one", 2), dx_assistant("a", 3),
                   dx_task_started("t2", 4), dx_user("two", 5), dx_aborted(6, "interrupted"))
    _, out = cli.json(path, "--host", "codex")
    assert out["interruptions"] == {"count": 1, "turns": [2]}


def test_other_abort_reasons_are_not_interruptions_but_stay_visible(cli, tmp_path):
    path = session(cli, tmp_path, dx_task_started("t1", 1), dx_user("one", 2), dx_aborted(3, "replaced"))
    _, out = cli.json(path, "--host", "codex")
    assert out["interruptions"]["count"] == 0
    assert out["coverage"]["ignored_types"]["event_msg/turn_aborted"] == 1


# ---- subagents: linked by parent thread id, copied history skipped -------------------------------

from fixtures.builders import dx_spawn  # noqa: E402

SUB_STEM = "rollout-2026-11-11T11-31-48-01000000-0000-7000-8000-00000000000a"


def parent_file(cli, tmp_path):
    return session(cli, tmp_path, dx_task_started("t1", 1), dx_user("delegate", 2), dx_spawn("sp1", 3),
                   dx_usage("resp_p1", 1000, 800, 20, 4), dx_assistant("done", 9), dx_usage("resp_p2", 1200, 1000, 10, 10),
                   name="rollout-parent.jsonl")


def sub_file(cli, tmp_path, parent="p1", forked_from="p1", start=5, stem=SUB_STEM):
    """A subagent rollout: its own meta, copied parent history (ordinals below `start`), then its own records."""
    copied = [dx_task_started("t1", 1), dx_user("delegate", 2), dx_exec_cmd("git log", "pc1", 3),
              dx_usage("resp_old", 900, 0, 99, 4)]
    own = [dx_task_started("s-t1", 5), dx_user("subagent brief", 6), dx_exec_cmd("ls", "sc1", 7),
           dx_exec_output("sc1", 8, "a", 0), dx_usage("resp_s1", 500, 0, 40, 9), dx_assistant("listed", 9)]
    records = [dx_meta("s1", cli.repo, parent=parent, forked_from=forked_from, history_start=start), *copied, *own]
    return write_jsonl(tmp_path / f"{stem}.jsonl", desktop(records))


def run_pair(cli, tmp_path, **kw):
    return cli.json(parent_file(cli, tmp_path), sub_file(cli, tmp_path, **kw), "--host", "codex")[1]


def test_the_subagent_is_linked_to_its_parent_by_thread_id(cli, tmp_path):
    out = run_pair(cli, tmp_path)
    assert [a["actor"] for a in out["actors"]] == ["main", f"subagent:{SUB_STEM}"]
    assert out["actors"][1]["link"] == "parent_thread_id"


def test_copied_parent_history_is_skipped_and_the_skip_is_reported(cli, tmp_path):
    out = run_pair(cli, tmp_path)
    sub = out["actors"][1]
    assert sub["history_from_ordinal"] == 5 and sub["history_records_skipped"] == 5
    assert sub["tokens"]["output"] == 40 and sub["tokens"]["api_calls"] == 1      # only its own response
    assert out["actors"][0]["tokens"]["api_calls"] == 2                           # the parent's are not counted twice
    assert sub["tool_calls"] == 1


def test_subagent_records_never_become_main_turns(cli, tmp_path):
    out = run_pair(cli, tmp_path)
    assert out["window"]["turns_total"] == 1
    assert out["tokens"]["api_calls"] == 3 and out["tokens"]["output"] == 70   # not 4 / 169: no copied history


def test_a_subagent_of_another_parent_is_not_linked_by_id(cli, tmp_path):
    out = run_pair(cli, tmp_path, parent="somebody-else", forked_from="somebody-else")
    assert out["actors"][1]["link"] == "timestamp"


def test_without_forked_from_nothing_is_skipped(cli, tmp_path):
    out = cli.json(parent_file(cli, tmp_path), sub_file(cli, tmp_path, forked_from=None, start=None), "--host", "codex")[1]
    sub = out["actors"][1]
    assert "history_from_ordinal" not in sub and "history_records_skipped" not in sub


def test_the_spawn_call_is_an_agent_call(cli, tmp_path):
    _, out = cli.json(parent_file(cli, tmp_path), "--host", "codex")
    assert out["tools"]["by_category"]["agent"] == 1


def test_a_uuid_shaped_subagent_label_is_readable(cli, tmp_path):
    out = run_pair(cli, tmp_path)
    assert "REDACTED" not in out["actors"][1]["actor"]


def test_same_position_records_of_different_files_are_not_mistaken_for_copies(cli, tmp_path):
    parent = session(cli, tmp_path, dx_task_started("t1", 1), dx_user("go", 2), dx_usage("resp_p", 100, 0, 11, 3),
                     name="rollout-parent.jsonl")
    sub = write_jsonl(tmp_path / f"{SUB_STEM}.jsonl", desktop([
        dx_meta("s1", cli.repo, parent="p1"), dx_task_started("s-t1", 4), dx_user("brief", 5),
        dx_usage("resp_s", 200, 0, 22, 6)]))
    _, out = cli.json(parent, sub, "--host", "codex")
    assert out["actors"][1]["tokens"]["output"] == 22 and out["tokens"]["output"] == 33


# ---- skills: used (link + block) and offered (developer message) -----------------------------

from fixtures.builders import cx_developer  # noqa: E402

LISTING = ("<skills_instructions>\n## Skills\nA skill is a set of local instructions.\n### Skill roots\n"
           "- `r0` = `/x/.codex/skills`\n### Available skills\n"
           "- imagegen: Generate raster images. (file: r0/imagegen/SKILL.md)\n"
           "- hello: Say hello. (file: r5/hello/SKILL.md)\n"
           "- data:analyze: Answer data questions. (file: r3/analyze/SKILL.md)\n"
           "### How to use skills\n- Discovery: read the list.\n</skills_instructions>")


def skills_session(cli, tmp_path, *extra, listing=True):
    block = lambda name: f"<skill><name>{name}</name><path>/x/{name}/SKILL.md</path>{'steps ' * 40}</skill>"
    records = ([cx_developer(LISTING, 0)] if listing else [])
    records += [dx_task_started("t1", 1), dx_user("[$hello](/x/hello/SKILL.md) hi", 2), dx_user(block("hello"), 3),
                dx_assistant("hi", 4), dx_task_started("t2", 5), dx_user("[$explicit-only](/x/explicit-only/SKILL.md) go", 6),
                dx_user(block("explicit-only"), 7), dx_assistant("ok", 8), *extra]
    return cli.json(session(cli, tmp_path, *records), "--host", "codex")[1]["skills"]


def test_used_skills_are_still_found_by_link_and_block(cli, tmp_path):
    skills = skills_session(cli, tmp_path)
    assert skills["used"] == ["hello", "explicit-only"]
    assert {s["name"]: s["detected_by"] for s in skills["loaded"]} == {"hello": "$skill link", "explicit-only": "$skill link"}


def test_offered_skills_come_from_the_developer_message_skill_list(cli, tmp_path):
    skills = skills_session(cli, tmp_path)
    assert skills["offered"]["names"] == ["imagegen", "hello", "data:analyze"] and skills["offered"]["total"] == 3
    assert skills["used_not_offered"] == ["explicit-only"]          # normal for an explicit-only skill


def test_without_a_skill_list_offered_is_not_measurable(cli, tmp_path):
    skills = skills_session(cli, tmp_path, listing=False)
    assert "not_measurable" in skills["offered"] and "used_not_offered" not in skills


def test_the_skill_list_is_labelled_in_events_without_its_names(cli, tmp_path):
    path = session(cli, tmp_path, cx_developer(LISTING, 0), dx_task_started("t1", 1))
    _, out = cli.json(path, "--host", "codex", "--events", "0:3")
    labels = [e["text"] for e in out["events"] if e["text"].startswith("skill listing")]
    assert labels == ["skill listing (3 names)"] and "imagegen" not in str(out["events"])



def test_several_cmds_in_one_exec_are_separate_commands_with_their_own_verify_check(cli, tmp_path):
    js = ('const a = await tools.exec_command({cmd:"pytest -q"});\ntext(a.output);\n'
          'const b = await tools.exec_command({cmd:"ls"});\ntext(b.output);\n')
    out = run_cmds(cli, tmp_path, (dx_exec(js, "c1", 3), dx_exec_output("c1", 4)))
    assert out["tools"]["by_category"] == {"shell": 2}
    assert out["verify"]["total"] == 1 and out["verify"]["by_kind"] == {"test": 1}
    _, ev = cli.json(session(cli, tmp_path / "e", dx_task_started("t1", 1), dx_exec(js, "c1", 3)),
                     "--host", "codex", "--events", "0:9")
    assert [c["command"] for e in ev["events"] for c in e["calls"]] == ["pytest -q", "ls"]


def listing_session(cli, tmp_path, text):
    path = session(cli, tmp_path, cx_developer(text, 0), dx_task_started("t1", 1), dx_user("go", 2), dx_assistant("a", 3))
    return cli.json(path, "--host", "codex")[1]

def test_a_skill_list_that_is_found_but_not_parsed_is_warned_about(cli, tmp_path):
    changed = "<skills_instructions>\n## Skills\n* hello: Say hello.\n* imagegen: Make images.\n</skills_instructions>"
    out = listing_session(cli, tmp_path, changed)
    assert out["coverage"]["warnings"] == ["skill list found but not parsed; offered list may be incomplete"]
    empty_items = "<skills_instructions>\n### Available skills\n* hello - Say hello.\n</skills_instructions>"
    out = listing_session(cli, tmp_path / "b", empty_items)
    assert out["coverage"]["warnings"] == ["skill list found but not parsed; offered list may be incomplete"]

def test_a_parsed_skill_list_or_no_skill_list_gives_no_warning(cli, tmp_path):
    assert "warnings" not in listing_session(cli, tmp_path, LISTING)["coverage"]
    assert "warnings" not in listing_session(cli, tmp_path / "b", "<collaboration_mode>plain</collaboration_mode>")["coverage"]


def test_the_desktop_version_range_is_open_ended(cli, tmp_path):
    for version in ("0.162.0", "0.300.0", "12.0.0"):
        path = write_jsonl(tmp_path / f"rollout-{version}.jsonl", desktop([dx_meta("p1", cli.repo, version=version)]))
        _, out = cli.json(path, "--host", "codex")
        assert out["coverage"]["variant"] == "desktop", version
    old = write_jsonl(tmp_path / "rollout-old.jsonl", desktop([dx_meta("p1", cli.repo, version="0.150.0")]))
    _, out = cli.json(old, "--host", "codex")
    assert out["coverage"]["variant"] == "classic"          # a tie below the range goes to the first listed variant


def test_the_skipped_records_line_says_what_was_copied_and_what_was_read_for_linking(cli, tmp_path):
    sub = run_pair(cli, tmp_path)["actors"][1]
    assert sub["history_note"] == ("copied history skipped: 4 records (ordinals 1–4); "
                                   "session_meta read for linking only")
    assert sub["history_records_skipped"] == 5


# ---- 1d: models, subagent brief bytes, token method labels ---------------------------------------------

def test_models_come_from_turn_context_for_each_actor(cli, tmp_path):
    parent = session(cli, tmp_path, dx_task_started("t1", 1), dx_turn_context(2, "gpt-main"), dx_user("go", 3),
                     dx_spawn("sp1", 4), dx_usage("resp_p", 100, 0, 5, 5), name="rollout-parent.jsonl")
    sub = write_jsonl(tmp_path / f"{SUB_STEM}.jsonl", desktop([
        dx_meta("s1", cli.repo, parent="p1"), dx_task_started("s-t1", 6), dx_turn_context(7, "gpt-sub"),
        dx_user("brief", 8), dx_usage("resp_s", 100, 0, 5, 9)]))
    _, out = cli.json(parent, sub, "--host", "codex")
    assert [a["models"] for a in out["actors"]] == [["gpt-main"], ["gpt-sub"]]


def test_classic_turn_context_model_is_mapped_too(cli, tmp_path):
    from fixtures.builders import cx_line
    path = write_jsonl(tmp_path / "rollout-c.jsonl", [cx_meta("i", cli.repo), cx_line("turn_context", {"model": "gpt-old"}, 1),
                                                      cx_user("go", 2), cx_tokens(1, 0, 1, 3)])
    _, out = cli.json(path, "--host", "codex")
    assert out["actors"][0]["models"] == ["gpt-old"]


def test_subagent_brief_bytes_come_from_the_spawn_message(cli, tmp_path):
    message = "x" * 321 + "é"                         # 322 characters, 323 bytes
    parent = session(cli, tmp_path, dx_task_started("t1", 1), dx_user("go", 2),
                     dx_spawn("sp1", 3, "/root/a", "other"), dx_spawn("sp2", 4, "/root/b", message),
                     dx_usage("resp_p", 100, 0, 5, 5), name="rollout-parent.jsonl")
    sub = write_jsonl(tmp_path / f"{SUB_STEM}.jsonl", desktop([
        dx_meta("s1", cli.repo, parent="p1", agent_path="/root/b"), dx_task_started("s-t1", 6),
        dx_user("short", 7), dx_usage("resp_s", 100, 0, 5, 8)]))
    _, out = cli.json(parent, sub, "--host", "codex")
    assert out["actors"][1]["brief_bytes"] == 323
    assert "x" * 20 not in str(out)                         # bytes only, never the text


def test_without_a_matching_spawn_the_brief_falls_back_to_the_first_message(cli, tmp_path):
    parent = session(cli, tmp_path, dx_task_started("t1", 1), dx_user("go", 2), name="rollout-parent.jsonl")
    sub = write_jsonl(tmp_path / f"{SUB_STEM}.jsonl", desktop([
        dx_meta("s1", cli.repo, parent="p1", agent_path="/root/zzz"), dx_task_started("s-t1", 6), dx_user("short", 7)]))
    _, out = cli.json(parent, sub, "--host", "codex")
    assert out["actors"][1]["brief_bytes"] == 5


def test_token_method_names_what_was_done_for_each_host_and_source(cli, tmp_path):
    from fixtures.builders import cc_assistant, cc_user, usage
    records = token_session(cli, tmp_path, dx_usage("resp_a", 100, 0, 5, 4))
    assert records["method"] == "token_usage_record, deduplicated by response_id"
    from fixtures.builders import cx_tokens
    fallback = token_session(cli, tmp_path / "f", with_counts=(cx_tokens(100, 0, 5, 4),))
    assert fallback["method"] == "token_count totals (no token_usage_record rows)"
    classic = write_jsonl(tmp_path / "rollout-c.jsonl", [cx_meta("i", cli.repo), cx_user("go", 1), cx_tokens(1, 0, 1, 2)])
    assert cli.json(classic, "--host", "codex")[1]["tokens"]["method"] == "token_count totals, a repeated total counted once"
    cc = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), cc_assistant("m", 1, usage=usage(1, 1))])
    assert cli.json(cc, "--host", "claude-code")[1]["tokens"]["method"].startswith("deduplicated by message id")


def test_a_subagent_record_equal_in_content_to_a_parent_record_is_still_counted(cli, tmp_path):
    """Both rollouts have turn_context records with the same model; only the timestamps tell them apart."""
    parent = session(cli, tmp_path, dx_task_started("t1", 1), dx_turn_context(2, "gpt-x"), dx_user("go", 3),
                     dx_usage("resp_p", 100, 0, 5, 4), name="rollout-parent.jsonl")
    sub = write_jsonl(tmp_path / f"{SUB_STEM}.jsonl", desktop([
        dx_meta("s1", cli.repo, parent="p1"), dx_task_started("s-t1", 6), dx_turn_context(7, "gpt-x"),
        dx_user("brief", 8), dx_usage("resp_s", 100, 0, 5, 9)]))
    _, out = cli.json(parent, sub, "--host", "codex")
    assert [a["models"] for a in out["actors"]] == [["gpt-x"], ["gpt-x"]]


def test_the_spawn_task_name_may_be_the_last_segment_of_the_agent_path(cli, tmp_path):
    """Real spawn calls say task_name "list_files"; the subagent's own path is "/root/list_files"."""
    parent = session(cli, tmp_path, dx_task_started("t1", 1), dx_user("go", 2),
                     dx_spawn("sp1", 3, "worker", "x" * 77), dx_usage("resp_p", 100, 0, 5, 4), name="rollout-parent.jsonl")
    sub = write_jsonl(tmp_path / f"{SUB_STEM}.jsonl", desktop([
        dx_meta("s1", cli.repo, parent="p1", agent_path="/root/worker"), dx_task_started("s-t1", 6), dx_user("short", 7)]))
    _, out = cli.json(parent, sub, "--host", "codex")
    assert out["actors"][1]["brief_bytes"] == 77


# ---- Codex 0.171 ---------------------------------------------------------------------------------------------------------------------

from fixtures.builders import dx_additional_tools  # noqa: E402

QUOTING = ('A skill is a set of local instructions. The list is under "## Skills" and its "### Available skills" heading, '
           "which names each skill and where its file is.")


def test_a_tool_listing_is_ignored_and_is_not_drift(cli, tmp_path):
    path = session(cli, tmp_path, dx_task_started("t1", 1), dx_additional_tools(2), dx_user("go", 3), dx_exec_cmd("ls", "c1", 4),
                   dx_exec_output("c1", 5, "a", 0), dx_usage("r1", 5, 0, 7, 6), dx_assistant("ok", 7))
    code, out = cli.json(path, "--host", "codex")
    assert code == 0 and out["coverage"]["drift"]["unseen_types"] == {} and out["tools"]["total"] == 1
    assert out["coverage"]["ignored_types"] and any("additional_tools" in k for k in out["coverage"]["ignored_types"])


def test_the_tool_listing_is_ignored_in_the_classic_layout_too(cli, tmp_path):
    from fixtures.builders import cx_call, cx_meta, cx_output, cx_tokens, cx_user
    path = write_jsonl(tmp_path / "rollout-c.jsonl", [cx_meta("i", cli.repo, cli_version="0.171.0-alpha.13"), cx_user("go", 1),
                                                    dx_additional_tools(2), cx_call("ls", "k1", 3), cx_output("k1", "a", 0, 4),
                                                    cx_tokens(1, 0, 1, 5)])
    code, out = cli.json(path, "--host", "codex")
    assert code == 0 and out["coverage"]["drift"]["unseen_types"] == {}


def test_the_newest_tested_codex_version_is_listed(cli, tmp_path):
    path = write_jsonl(tmp_path / "rollout-v.jsonl", desktop([dx_meta("p1", cli.repo, version="0.171.0-alpha.13")]))
    _, out = cli.json(path, "--host", "codex")
    assert "0.171.0-alpha.13" in out["coverage"]["tested_versions"] and "version_note" in out["coverage"]


def test_a_message_that_only_quotes_the_skills_heading_is_not_an_unparsed_list(cli, tmp_path):
    """0.171 sends instructions that name the heading in prose, then the real listing in the next message."""
    path = session(cli, tmp_path, cx_developer(QUOTING, 0), cx_developer(LISTING, 1), dx_task_started("t1", 2),
                   dx_user("go", 3), dx_assistant("a", 4))
    _, out = cli.json(path, "--host", "codex")
    assert "warnings" not in out["coverage"] and out["skills"]["offered"]["total"] == 3


def test_the_skills_heading_must_start_a_line_to_count_as_a_listing(cli, tmp_path):
    out = listing_session(cli, tmp_path, QUOTING)
    assert "warnings" not in out["coverage"]
