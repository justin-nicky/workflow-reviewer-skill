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
