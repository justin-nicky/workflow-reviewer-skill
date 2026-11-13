"""Cursor builds without a link in the parent's text: spawns are CallDynamicTool calls, and the subagent
files are matched to them by how much the brief overlaps the spawn's arguments."""
from fixtures.builders import cu_assistant, cu_spawn, cu_turn_ended, cu_user, write_jsonl

SID = "00000000-0000-4000-8000-0000000000b2"
FIRST, SECOND = "00000000-0000-4000-8000-0000000000a1", "00000000-0000-4000-8000-0000000000a2"
TEMPLATE = " ".join(f"template{n}" for n in range(60))          # the shared prefix of every brief


def brief(tail):
    return f"{TEMPLATE} {tail}"


def session(tmp_path, main, subs):
    folder = tmp_path / SID
    write_jsonl(folder / f"{SID}.jsonl", main)
    for sub_id, records in subs.items():
        write_jsonl(folder / "subagents" / f"{sub_id}.jsonl", records)
    return folder


def sub(text, calls=1, tag="x"):
    """A subagent file; each file reads its own paths (identical records in two files would count once)."""
    return [cu_user(text), *[cu_assistant("w", [("Read", {"path": f"/r/{tag}-{n}.md"})]) for n in range(calls)], cu_turn_ended()]


def two_spawns():
    """Spawn alpha in turn 1, spawn beta in turn 2. The file that sorts first holds beta's brief."""
    main = [cu_user("one"), cu_assistant("go", [cu_spawn(brief("alpha alpha alpha plan the parser"))]), cu_turn_ended(),
            cu_user("two"), cu_assistant("go", [cu_spawn(brief("beta beta beta review the schema"))]), cu_turn_ended()]
    subs = {FIRST: sub(brief("beta beta beta review the schema"), calls=3, tag="a1"),
            SECOND: sub(brief("alpha alpha alpha plan the parser"), calls=5, tag="a2")}
    return main, subs


def rows(out):
    return {a["actor"]: a for a in out["actors"]}


def test_a_dynamic_task_call_is_a_spawn(cli, tmp_path):
    main, subs = two_spawns()
    _, out = cli.json(session(tmp_path, main, subs), "--host", "cursor")
    assert out["tools"]["by_category"]["agent"] == 2


def test_each_subagent_goes_to_the_spawn_its_brief_overlaps_most(cli, tmp_path):
    main, subs = two_spawns()
    folder = session(tmp_path, main, subs)
    _, turn1 = cli.json(folder, "--host", "cursor", "--from-turn", 1, "--to-turn", 1)
    _, turn2 = cli.json(folder, "--host", "cursor", "--from-turn", 2, "--to-turn", 2)
    assert rows(turn1)[f"subagent:{SECOND}"]["tool_calls"] == 5 and f"subagent:{FIRST}" not in rows(turn1)
    assert rows(turn2)[f"subagent:{FIRST}"]["tool_calls"] == 3 and f"subagent:{SECOND}" not in rows(turn2)


def test_the_match_names_its_score_and_the_runner_up(cli, tmp_path):
    main, subs = two_spawns()
    _, out = cli.json(session(tmp_path, main, subs), "--host", "cursor")
    row = rows(out)[f"subagent:{FIRST}"]
    assert row["link"] == "brief"
    assert row["match_score"] > row["runner_up_score"] and 0 < row["runner_up_score"] < 1


def test_identical_briefs_go_to_the_spawns_in_order(cli, tmp_path):
    same = brief("the same words")
    main = [cu_user("one"), cu_assistant("go", [cu_spawn(same)]), cu_turn_ended(),
            cu_user("two"), cu_assistant("go", [cu_spawn(same)]), cu_turn_ended()]
    folder = session(tmp_path, main, {FIRST: sub(same, calls=3, tag="a1"), SECOND: sub(same, calls=5, tag="a2")})
    _, turn1 = cli.json(folder, "--host", "cursor", "--from-turn", 1, "--to-turn", 1)
    assert rows(turn1)[f"subagent:{FIRST}"]["tool_calls"] == 3 and f"subagent:{SECOND}" not in rows(turn1)


def test_a_spawn_is_used_once(cli, tmp_path):
    main = [cu_user("one"), cu_assistant("go", [cu_spawn(brief("only one spawn"))]), cu_turn_ended()]
    folder = session(tmp_path, main, {FIRST: sub(brief("only one spawn"), tag="a1"), SECOND: sub(brief("only one spawn"), tag="a2")})
    _, out = cli.json(folder, "--host", "cursor")
    assert sorted(a["link"] for a in rows(out).values() if a["actor"] != "main") == ["brief", "none"]


def test_a_markdown_link_still_comes_first(cli, tmp_path):
    main = [cu_user("one"), cu_assistant(f"done: [explore]({FIRST})", [cu_spawn(brief("alpha"))]), cu_turn_ended()]
    _, out = cli.json(session(tmp_path, main, {FIRST: sub(brief("alpha"))}), "--host", "cursor")
    assert rows(out)[f"subagent:{FIRST}"]["link"] == "markdown_link"


def test_a_linked_subagent_stays_in_the_default_window_before_the_invocation(cli, tmp_path):
    main = [cu_user("one"), cu_assistant(f"done: [explore]({FIRST})"), cu_turn_ended(),
            cu_user("/workflow-reviewer"), cu_assistant("ok"), cu_turn_ended()]
    _, out = cli.json(session(tmp_path, main, {FIRST: sub("anything", calls=4)}), "--host", "cursor")
    assert out["window"]["reviewer_invocation_found"] is True
    assert rows(out)[f"subagent:{FIRST}"]["tool_calls"] == 4


# ---- subagents that no spawn explains --------------------------------------------------------------------

def unlinked_session(tmp_path):
    main = [cu_user("one"), cu_assistant("go", [cu_spawn(brief("alpha"))]), cu_turn_ended(),
            cu_user("two"), cu_assistant("fine"), cu_turn_ended()]
    return session(tmp_path, main, {FIRST: sub(brief("alpha"), calls=2, tag="a1"), SECOND: sub("something else entirely", calls=7, tag="a2")})


def test_an_unlinked_subagent_is_counted_in_coverage_and_in_no_window(cli, tmp_path):
    _, out = cli.json(unlinked_session(tmp_path), "--host", "cursor", "--from-turn", 1, "--to-turn", 2)
    assert out["coverage"]["unlinked_subagents"] == 1
    assert "unlinked subagents: 1 (not placed in any window)" in out["coverage"]["warnings"]
    assert f"subagent:{SECOND}" not in rows(out)


def test_a_whole_session_run_lists_the_unlinked_subagent_as_an_actor(cli, tmp_path):
    _, out = cli.json(unlinked_session(tmp_path), "--host", "cursor")
    assert rows(out)[f"subagent:{SECOND}"]["link"] == "none"
    assert out["coverage"]["unlinked_subagents"] == 1


def test_no_unlinked_subagents_means_no_such_warning(cli, tmp_path):
    main, subs = two_spawns()
    _, out = cli.json(session(tmp_path, main, subs), "--host", "cursor")
    assert "unlinked_subagents" not in out["coverage"]
    assert not any("unlinked" in w for w in out["coverage"].get("warnings", []))


# ---- the invocation, in each host's own form ---------------------------------------------------------------

ATTACHED = ("<manually_attached_skills>\nSkill Name: workflow-reviewer\nPath: /x/.cursor/skills/workflow-reviewer/SKILL.md\n"
            "steps\n</manually_attached_skills>\n<user_query>\nreview it\n</user_query>")


def test_a_cursor_prompt_attaching_the_skill_is_the_invocation(cli, tmp_path):
    records = [cu_user("one"), cu_assistant("a"), cu_turn_ended(), cu_user("two"), cu_assistant("b"), cu_turn_ended(),
               cu_user(ATTACHED), cu_assistant("reviewing"), cu_turn_ended()]
    _, out = cli.json(write_jsonl(tmp_path / "c.jsonl", records), "--host", "cursor")
    assert out["window"]["reviewer_invocation_found"] is True and out["window"]["turns_total"] == 2


def test_attaching_another_skill_is_not_the_invocation(cli, tmp_path):
    other = ATTACHED.replace("workflow-reviewer", "hello")
    _, out = cli.json(write_jsonl(tmp_path / "c.jsonl", [cu_user(other), cu_assistant("a")]), "--host", "cursor")
    assert out["window"]["reviewer_invocation_found"] is False


def test_a_cline_prompt_asking_for_the_skill_is_the_invocation(cli, tmp_path):
    from fixtures.cline import assistant, user_input, write_session
    messages = [user_input("run the tests", 1, 0), assistant(1, 2), user_input("use the workflow-reviewer skill on this task", 3, 10),
                assistant(4, 12)]
    _, out = cli.json(write_session(tmp_path / "sessions", "s1", cli.repo, messages), "--host", "cline")
    assert out["window"]["reviewer_invocation_found"] is True and out["window"]["turns_total"] == 1


def test_a_cline_prompt_that_only_mentions_the_skill_is_not(cli, tmp_path):
    from fixtures.cline import assistant, user_input, write_session
    messages = [user_input("rename workflow-reviewer in the docs", 1, 0), assistant(1, 2)]
    _, out = cli.json(write_session(tmp_path / "sessions", "s1", cli.repo, messages), "--host", "cline")
    assert out["window"]["reviewer_invocation_found"] is False


def test_a_devin_slash_command_is_the_invocation(cli, tmp_path):
    from fixtures import devin as dv
    db = dv.DevinDb(tmp_path / "sessions.db")
    chain = db.session("sess-one", str(cli.repo.resolve()))
    chain.add(dv.system(), dv.user("real task"), dv.assistant("ok", tokens=dv.metrics(1, 1, 1)),
              dv.user("expanded skill text", slash="/workflow-reviewer"), dv.assistant("ok", tokens=dv.metrics(1, 1, 999)))
    chain.done()
    _, out = cli.json(db.path, "--host", "devin")
    assert out["window"]["reviewer_invocation_found"] is True and out["window"]["turns_total"] == 1


def test_a_codex_dollar_command_is_the_invocation(cli, tmp_path):
    from fixtures.builders import cx_tokens, cx_user
    records = [cx_user("real task", 0), cx_tokens(1, 0, 5, 1), cx_user("$workflow-reviewer", 50), cx_tokens(1, 0, 999, 51)]
    _, out = cli.json(write_jsonl(tmp_path / "r.jsonl", records), "--host", "codex")
    assert out["window"]["reviewer_invocation_found"] is True


# ---- the end of a coarse window is the next prompt ---------------------------------------------------------------

def stamped(text, clock, day="Wednesday, Nov 11, 2026"):
    return cu_user(f"<timestamp>{day}, {clock} (UTC+4)</timestamp>\n<user_query>\n{text}\n</user_query>")


def test_the_next_prompt_ends_the_window_and_the_label_says_so(cli, tmp_path):
    records = [stamped("a", "11:00 AM"), cu_assistant("x"), cu_turn_ended(),
               stamped("b", "11:10 AM"), cu_assistant("y"), cu_turn_ended(),
               stamped(ATTACHED, "12:10 PM"), cu_assistant("z")]
    _, out = cli.json(write_jsonl(tmp_path / "c.jsonl", records), "--host", "cursor")
    assert out["window"]["turns_total"] == 2 and out["window"]["reviewer_invocation_found"] is True
    assert out["time"]["wall_s"] == 4200 and out["time"]["wall_range_s"] == [4140, 4260]
    assert out["time"]["precision"] == "coarse: prompt timestamps only, minute precision; until the next prompt"


def test_with_no_later_prompt_the_last_turn_is_unknown(cli, tmp_path):
    records = [stamped("a", "11:00 AM"), cu_assistant("x"), stamped("b", "11:10 AM"), cu_assistant("y")]
    _, out = cli.json(write_jsonl(tmp_path / "c.jsonl", records), "--host", "cursor")
    assert out["time"]["wall_s"] == 600
    assert out["time"]["precision"] == "coarse: prompt timestamps only, minute precision; last turn's duration unknown"


def test_a_window_ended_by_to_turn_also_uses_the_next_prompt(cli, tmp_path):
    records = [stamped("a", "11:00 AM"), cu_assistant("x"), stamped("b", "11:10 AM"), cu_assistant("y"),
               stamped("c", "11:30 AM"), cu_assistant("z")]
    _, out = cli.json(write_jsonl(tmp_path / "c.jsonl", records), "--host", "cursor", "--from-turn", 1, "--to-turn", 2)
    assert out["time"]["wall_s"] == 1800
    assert out["time"]["precision"].endswith("until the next prompt")


def test_a_single_turn_before_the_next_prompt_has_a_wall_time(cli, tmp_path):
    records = [stamped("a", "11:00 AM"), cu_assistant("x"), stamped("b", "11:05 AM"), cu_assistant("y")]
    _, out = cli.json(write_jsonl(tmp_path / "c.jsonl", records), "--host", "cursor", "--from-turn", 1, "--to-turn", 1)
    assert out["time"]["wall_s"] == 300
