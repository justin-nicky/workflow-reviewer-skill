"""Skills the user starts by name (slash command, $skill, attached skill) are detected per host."""
from fixtures.builders import (cc_assistant, cc_user, cu_assistant, cu_turn_ended, cu_user,
                               cx_injected_user, cx_meta, cx_user, usage, write_jsonl)


def loaded(out):
    return {s["name"]: s for s in out["skills"]["loaded"]}


def test_claude_code_slash_command_skill_is_detected(cli, tmp_path):
    body = "Base directory for this skill: /x/.claude/skills/hello\n\n" + "steps " * 50
    path = write_jsonl(tmp_path / "s.jsonl", [
        cc_user("<command-message>hello</command-message>\n<command-name>/hello</command-name>", 0),
        cc_user(body, 1, isMeta=True),
        cc_assistant("m", 2, usage=usage(1, 1)),
        cc_user("<command-name>/clear</command-name>", 10),               # a command, not a skill
        cc_user("<local-command-stdout></local-command-stdout>", 11, isMeta=True),
    ])
    _, out = cli.json(path, "--host", "claude-code")
    assert list(loaded(out)) == ["hello"]
    assert loaded(out)["hello"]["detected_by"] == "slash command"
    assert loaded(out)["hello"]["bytes"] == len(body)


def test_claude_code_prompt_that_only_mentions_a_command_name_is_not_a_skill(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        cc_user("what does <command-name>/hello</command-name> do?", 0),
        cc_assistant("m", 2, usage=usage(1, 1)),
    ])
    _, out = cli.json(path, "--host", "claude-code")
    assert loaded(out) == {}


def test_codex_dollar_skill_link_with_injected_block_is_detected(cli, tmp_path):
    block = "<skill><name>hello</name><path>/x/.agents/skills/hello/SKILL.md</path>" + "steps " * 50 + "</skill>"
    path = write_jsonl(tmp_path / "rollout-x.jsonl", [
        cx_meta("i", cli.repo),
        cx_user("[$hello](/x/.agents/skills/hello/SKILL.md) say hi", 1),
        cx_injected_user("[$hello](/x/.agents/skills/hello/SKILL.md) say hi", 1),
        cx_injected_user(block, 2),
        cx_injected_user("<environment_context>cwd</environment_context>", 3),
    ])
    _, out = cli.json(path, "--host", "codex")
    assert list(loaded(out)) == ["hello"]
    assert loaded(out)["hello"]["detected_by"] == "$skill link"
    assert loaded(out)["hello"]["bytes"] == len(block)


def test_codex_link_without_an_injected_block_is_not_a_loaded_skill(cli, tmp_path):
    path = write_jsonl(tmp_path / "rollout-x.jsonl", [
        cx_meta("i", cli.repo),
        cx_user("see [$hello](/x/.agents/skills/hello/SKILL.md)", 1),
    ])
    _, out = cli.json(path, "--host", "codex")
    assert loaded(out) == {}


def test_cursor_manually_attached_skill_is_detected(cli, tmp_path):
    text = ("<manually_attached_skills>\nSkill Name: hello\nPath: /x/.cursor/skills/hello/SKILL.md\n"
            + "steps " * 50 + "\n</manually_attached_skills>")
    path = write_jsonl(tmp_path / "c.jsonl", [
        cu_user(text), cu_assistant("MANGO"), cu_turn_ended(),
        cu_user("a plain prompt mentioning Skill Name: nope"), cu_turn_ended(),
    ])
    _, out = cli.json(path, "--host", "cursor")
    assert list(loaded(out)) == ["hello"]
    assert loaded(out)["hello"]["detected_by"] == "attached skill"


def test_skill_md_read_and_skill_tool_detection_still_work(cli, tmp_path):
    from fixtures.builders import cc_tool_result, tool_use
    path = write_jsonl(tmp_path / "s.jsonl", [
        cc_user("go", 0),
        cc_assistant("m0", 2, [tool_use("Skill", {"skill": "tdd"}, "cs")], usage(1, 1)),
        cc_tool_result("cs", "loaded", 3),
    ])
    _, out = cli.json(path, "--host", "claude-code")
    assert loaded(out)["tdd"]["detected_by"] == "skill-tool"
