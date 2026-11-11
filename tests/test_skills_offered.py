"""Skills offered to the model are reported separately from skills used."""
from fixtures.builders import (at, cc_assistant, cc_user, cu_assistant, cu_user, usage, write_jsonl)

NAMES = ["learning-notes", "hello", "data:analyze", "mattpocock-skills:tdd"]


def listing(names, t, session="s1"):
    return {"type": "attachment", "uuid": f"l-{t}", "sessionId": session, "timestamp": at(t),
            "attachment": {"type": "skill_listing", "names": names, "skillCount": len(names), "isInitial": True}}


def slash(name, t):
    return [cc_user(f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>", t),
            cc_user(f"Base directory for this skill: /x/skills/{name}\n\nsteps", t + 1, isMeta=True)]


def session(tmp_path, *records):
    return write_jsonl(tmp_path / "s.jsonl", [listing(NAMES, 0), *records, cc_assistant("m", 90, usage=usage(1, 1))])


def test_offered_and_used_are_reported_separately_and_explicit_only_is_normal(cli, tmp_path):
    path = session(tmp_path, *slash("hello", 10), *slash("explicit-only", 20))
    code, out = cli.json(path, "--host", "claude-code")
    skills = out["skills"]
    assert code == 0                                          # not an error, not drift
    assert "hello" in skills["offered"]["names"] and "explicit-only" not in skills["offered"]["names"]
    assert skills["offered"]["total"] == len(NAMES)
    assert skills["used"] == ["hello", "explicit-only"]
    assert skills["used_not_offered"] == ["explicit-only"]


def test_a_plugin_skill_used_by_its_short_name_counts_as_offered(cli, tmp_path):
    from fixtures.builders import cc_tool_result, tool_use
    path = session(tmp_path, cc_user("go", 5),
                   cc_assistant("m0", 6, [tool_use("Skill", {"skill": "tdd"}, "cs")], usage(1, 1)),
                   cc_tool_result("cs", "loaded", 7))
    _, out = cli.json(path, "--host", "claude-code")
    assert out["skills"]["used"] == ["tdd"] and out["skills"]["used_not_offered"] == []


def test_the_listing_is_session_level_so_a_later_window_still_sees_it(cli, tmp_path):
    path = session(tmp_path, cc_user("one", 5), cc_user("two", 30), *slash("hello", 40))
    _, out = cli.json(path, "--host", "claude-code", "--from-turn", "2")
    assert out["skills"]["offered"]["total"] == len(NAMES)


def test_later_listings_add_names_and_subagent_listings_are_ignored(cli, tmp_path):
    path = write_jsonl(tmp_path / "proj" / "s1.jsonl", [
        listing(["a", "b"], 0), cc_user("go", 1), listing(["b", "c"], 2), cc_assistant("m", 3, usage=usage(1, 1))])
    write_jsonl(tmp_path / "proj" / "s1" / "subagents" / "agent-x.jsonl", [
        listing(["only-sub"], 4), cc_user("brief", 5, uuid="su")])
    _, out = cli.json(path, "--host", "claude-code")
    assert out["skills"]["offered"]["names"] == ["a", "b", "c"]


def test_no_listing_means_not_measurable_not_empty(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), cc_assistant("m", 1, usage=usage(1, 1))])
    _, out = cli.json(path, "--host", "claude-code")
    assert "not_measurable" in out["skills"]["offered"]
    assert "used_not_offered" not in out["skills"]
    cu = write_jsonl(tmp_path / "c.jsonl", [cu_user("go"), cu_assistant("hi")])
    _, out = cli.json(cu, "--host", "cursor")
    assert "not_measurable" in out["skills"]["offered"]
