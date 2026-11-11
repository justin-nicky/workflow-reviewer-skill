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


# ---- subagent link: a markdown link to the subagent id -----------------------------------------------

SUB_ID = "00000000-0000-4000-8000-0000000000a1"


def folder_session(tmp_path, main_records, sub_records, sub_id=SUB_ID):
    sid = tmp_path / "00000000-0000-4000-8000-0000000000b2"
    write_jsonl(sid / f"{sid.name}.jsonl", main_records)
    write_jsonl(sid / "subagents" / f"{sub_id}.jsonl", sub_records)
    return sid


def test_the_subagent_is_linked_by_the_markdown_link_in_the_parent_text(cli, tmp_path):
    sid = folder_session(tmp_path,
                         [cu_user("one"), cu_assistant("first"), cu_turn_ended(),
                          cu_user("two"), cu_assistant(f"The explorer finished: [Explore the files]({SUB_ID})"),
                          cu_turn_ended()],
                         [cu_user("a brief nobody quotes"), cu_assistant("found")])
    _, out = cli.json(sid, "--host", "cursor")
    sub = out["actors"][1]
    assert sub["actor"] == f"subagent:{SUB_ID}" and sub["link"] == "markdown_link"


def test_a_link_to_another_id_does_not_link(cli, tmp_path):
    sid = folder_session(tmp_path, [cu_user("one"), cu_assistant("see [x](11111111-2222-3333-4444-555555555555)")],
                         [cu_user("brief"), cu_assistant("found")])
    _, out = cli.json(sid, "--host", "cursor")
    assert out["actors"][1]["link"] == "none"


def test_without_a_link_the_brief_fallback_is_named(cli, tmp_path):
    sid = folder_session(tmp_path,
                         [cu_user("one"), cu_assistant("go", [("Task", {"prompt": "explore the repo layout"})])],
                         [cu_user("explore the repo layout"), cu_assistant("done")])
    _, out = cli.json(sid, "--host", "cursor")
    assert out["actors"][1]["link"] == "brief"


# ---- automatic skill use: a read of <name>/SKILL.md ------------------------------------------------------

def loaded(out):
    return {s["name"]: s["detected_by"] for s in out["skills"]["loaded"]}


def test_a_read_of_skill_md_without_an_attached_block_is_automatic_use(cli, tmp_path):
    out = run(cli, tmp_path, cu_user("greet me"),
              cu_assistant("ok", [("Read", {"path": "/x/.cursor/skills/mango-greeting/SKILL.md"})]), cu_turn_ended())
    assert loaded(out) == {"mango-greeting": "read SKILL.md"}
    assert out["skills"]["used"] == ["mango-greeting"]


def test_an_attached_skill_that_is_also_read_stays_attached(cli, tmp_path):
    attached = ("<manually_attached_skills>\nSkill Name: hello\nPath: /x/.cursor/skills/hello/SKILL.md\nsteps\n"
                "</manually_attached_skills>")
    out = run(cli, tmp_path, cu_user(attached),
              cu_assistant("ok", [("Read", {"path": "/x/.cursor/skills/hello/SKILL.md"})]), cu_turn_ended())
    assert loaded(out) == {"hello": "attached skill"}


def test_reading_a_file_that_merely_sits_under_a_skills_folder_is_not_a_use(cli, tmp_path):
    out = run(cli, tmp_path, cu_user("x"), cu_assistant("ok", [("Read", {"path": "/x/.cursor/skills/hello/notes.md"})]))
    assert loaded(out) == {}
