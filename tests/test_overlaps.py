"""skills.overlaps: description similarity, directive lines against always-on instructions, and hooks."""
import json

import pytest
from fixtures import devin as dv
from fixtures.builders import bash, cc_assistant, cc_tool_result, cc_user, tool_use, usage, write_jsonl
from test_skills_offered import listing

REVIEW_A = "Review pull requests for correctness bugs and report the findings with a severity ranking"
REVIEW_B = "Review pull requests for correctness bugs and report findings with a severity ranking"


def skill(root, name, description, body="Steps go here.", fm_name=None):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    fm_name = fm_name or name
    (folder / "SKILL.md").write_text(f"---\nname: {fm_name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")


def filler_skills(root, n=6):
    """Skills that make 'code', 'use' and 'help' common, so those words count for little."""
    topics = ["billing invoices", "database migrations", "image thumbnails", "calendar invites", "weather forecasts", "audio transcripts",
              "spreadsheet exports", "gardening schedules"]
    for i in range(n):
        skill(root, f"filler-{i}", f"Use this skill to help with code about {topics[i]} when you need it")


def run(cli, tmp_path, *args, names=("hello",), calls=(), session_extra=()):
    records = [listing(list(names), 0), cc_user("go", 1)]
    for k, (tool, tool_input) in enumerate(calls):
        records += [cc_assistant(f"m{k}", 10 + k, [tool_use(tool, tool_input, f"c{k}")], usage(1, 1)), cc_tool_result(f"c{k}", "ok", 11 + k)]
    path = write_jsonl(tmp_path / "s.jsonl", [*records, *session_extra, cc_assistant("mz", 90, usage=usage(1, 1))])
    code, out = cli.json(path, "--host", "claude-code", *args)
    assert code == 0
    return out["skills"]["overlaps"]


def pair(overlaps, a, b):
    return next((p for p in overlaps["skill_pairs"]["top"] if {p["a"], p["b"]} == {a, b}), None)


# ---- skill vs skill -----------------------------------------------------------------------------------------------------

def test_a_near_duplicate_description_pair_scores_at_least_70_percent(cli, tmp_path):
    root = cli.home / ".claude" / "skills"
    filler_skills(root)
    skill(root, "code-review", REVIEW_A)
    skill(root, "pr-review", REVIEW_B)
    o = run(cli, tmp_path, names=("code-review", "pr-review"))
    p = pair(o, "code-review", "pr-review")
    assert p and p["score_pct"] >= 70 and 1 <= len(p["shared_terms"]) <= 5
    assert {p["a_status"], p["b_status"]} == {"offered"}
    assert o["scanned"]["skills"] == 8


def test_two_skills_sharing_only_common_words_score_below_30_percent(cli, tmp_path):
    root = cli.home / ".claude" / "skills"
    filler_skills(root)
    skill(root, "alpha", "Use this skill to help with code about origami folding when you need it")
    skill(root, "beta", "Use this skill to help with code about volcano monitoring when you need it")
    o = run(cli, tmp_path, "--overlap-threshold", "0.0")
    p = pair(o, "alpha", "beta")
    assert p is None or p["score_pct"] < 30
    ranked = {(x["a"], x["b"]): x["score_pct"] for x in o["skill_pairs"]["top"]}
    assert ranked.get(("alpha", "beta"), 0) < 30


def test_an_unrelated_pair_is_below_the_threshold(cli, tmp_path):
    root = cli.home / ".claude" / "skills"
    filler_skills(root)
    skill(root, "alpha", "Translate release notes into formal Latin poetry")
    skill(root, "beta", "Calibrate a telescope mirror using interferometry")
    o = run(cli, tmp_path)
    assert pair(o, "alpha", "beta") is None


def test_status_is_loaded_offered_or_installed_only(cli, tmp_path):
    root = cli.home / ".claude" / "skills"
    filler_skills(root)
    skill(root, "one", REVIEW_A)
    skill(root, "two", REVIEW_B)
    skill(root, "three", REVIEW_B)
    records = [cc_assistant("s", 5, [tool_use("Skill", {"skill": "one"}, "cs")], usage(1, 1)), cc_tool_result("cs", "loaded", 6)]
    o = run(cli, tmp_path, names=("one", "two"), session_extra=records)
    status = {p["a"]: p["a_status"] for p in o["skill_pairs"]["top"]} | {p["b"]: p["b_status"] for p in o["skill_pairs"]["top"]}
    assert status["one"] == "loaded this session" and status["two"] == "offered" and status["three"] == "installed only"


def test_the_same_name_in_two_folders_is_its_own_finding(cli, tmp_path):
    skill(cli.home / ".claude" / "skills", "dup", "Alpha description about origami folding")
    skill(cli.repo / ".claude" / "skills", "dup", "Entirely different words about volcano monitoring")
    o = run(cli, tmp_path)
    dup = o["duplicate_names"]
    assert dup["total"] == 1 and dup["top"][0]["name"] == "dup" and len(dup["top"][0]["paths"]) == 2
    assert all("SKILL.md" in p for p in dup["top"][0]["paths"]) and dup["more"] == 0


def test_frontmatter_name_is_the_skills_name(cli, tmp_path):
    skill(cli.home / ".claude" / "skills", "folder-a", "Alpha description about origami", fm_name="shared-name")
    skill(cli.repo / ".claude" / "skills", "folder-b", "Other description about volcano", fm_name="shared-name")
    assert run(cli, tmp_path)["duplicate_names"]["top"][0]["name"] == "shared-name"


def test_multiline_frontmatter_descriptions_are_read(cli, tmp_path):
    root = cli.home / ".claude" / "skills"
    filler_skills(root)
    for name in ("folded", "plain"):
        (root / name).mkdir()
    (root / "folded" / "SKILL.md").write_text("---\nname: folded\ndescription: >\n  Review pull requests for correctness bugs and\n  report findings with a severity ranking\n---\nbody\n")
    skill(root, "plain", REVIEW_B)
    assert pair(run(cli, tmp_path), "folded", "plain")["score_pct"] >= 70


def test_an_offered_skill_with_no_file_is_not_scanned_not_guessed(cli, tmp_path):
    skill(cli.home / ".claude" / "skills", "present", "Alpha description about origami")
    o = run(cli, tmp_path, names=("present", "ghost", "plugin:also-ghost"))
    assert o["scanned"]["skills"] == 1 and o["scanned"]["not_scanned"] == 2


APP = ("Library", "Application Support", "Claude", "local-agent-mode-sessions")


def plugin_layout(cli):
    root = cli.home.joinpath(*APP)
    skill(root / "skills-plugin" / "acc" / "org" / "skills", "from-skills-plugin", "Alpha description about origami")
    skill(root / "acc" / "org" / "rpm" / "plugin_x" / "skills", "from-rpm", "Beta description about volcano monitoring")
    skill(root / "acc" / "org" / "rpm" / "plugin_x" / "skills" / "engineering", "from-category", "Gamma description about telescopes")
    skill(root / "acc" / "org" / "rpm" / "plugin_x" / "skills" / "in-progress", "draft", "Delta description about pottery")
    skill(cli.home / ".claude" / "plugins" / "cache" / "mkt" / "plug" / "1.0" / "skills", "guessed-location", "Never read")


def test_plugin_skill_folders_are_the_desktop_apps_plugin_folders(cli, tmp_path):
    plugin_layout(cli)
    names = ("from-skills-plugin", "from-rpm", "plugin:from-category")
    o = run(cli, tmp_path, names=names)
    assert o["scanned"]["skills"] == 3 and o["scanned"]["from_files"] == 3 and o["scanned"]["not_scanned"] == 0


def test_plugin_skills_nobody_offered_are_left_out(cli, tmp_path):
    plugin_layout(cli)                                     # 'draft' and the rest are on disk but were never offered
    assert run(cli, tmp_path, names=("from-rpm",))["scanned"]["skills"] == 1
    assert run(cli, tmp_path, names=("from-rpm",))["scanned"]["from_files"] == 1


def test_without_an_offered_list_plugin_skills_are_not_assumed_installed(cli, tmp_path):
    plugin_layout(cli)
    skill(cli.home / ".claude" / "skills", "mine", "Epsilon description about gardening")
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 1), cc_assistant("mz", 90, usage=usage(1, 1))])
    o = cli.json(path, "--host", "claude-code")[1]["skills"]["overlaps"]
    assert o["scanned"]["skills"] == 1


def test_the_threshold_flag_moves_the_cut(cli, tmp_path):
    root = cli.home / ".claude" / "skills"
    filler_skills(root)
    skill(root, "a", REVIEW_A)
    skill(root, "b", "Review pull requests and summarise findings in a table")
    low = run(cli, tmp_path, "--overlap-threshold", "0.2")["skill_pairs"]
    high = run(cli, tmp_path, "--overlap-threshold", "0.95")["skill_pairs"]
    assert low["threshold"] == 0.2 and high["threshold"] == 0.95 and low["total"] > high["total"]


def test_lists_are_capped_with_a_more_count(cli, tmp_path):
    root = cli.home / ".claude" / "skills"
    filler_skills(root)
    for i in range(8):
        skill(root, f"twin-{i}", REVIEW_A)
    o = run(cli, tmp_path, "--overlap-threshold", "0.5")["skill_pairs"]
    assert o["total"] == 28 and len(o["top"]) == 10 and o["more"] == 18
    assert [p["score_pct"] for p in o["top"]] == sorted((p["score_pct"] for p in o["top"]), reverse=True)


def test_output_is_deterministic(cli, tmp_path):
    root = cli.home / ".claude" / "skills"
    filler_skills(root)
    for i in range(5):
        skill(root, f"twin-{i}", REVIEW_A)
    skill(cli.repo / ".claude" / "skills", "twin-0", REVIEW_B)
    assert json.dumps(run(cli, tmp_path)) == json.dumps(run(cli, tmp_path))


# ---- skill bodies against always-on instructions ----------------------------------------------------------------------

DIRECTIVE = "Always run the full test suite before every commit"


def rule_file(cli, text):
    path = cli.repo / "CLAUDE.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_directive_pair_from_files_not_known_to_be_loaded_is_static(cli, tmp_path):
    skill(cli.home / ".claude" / "skills", "tdd", "Test driven development", body=f"Intro line.\n{DIRECTIVE}.\nOther text.")
    rule_file(cli, f"# Rules\nSome text.\n{DIRECTIVE}, then push.\n")
    o = run(cli, tmp_path, names=("tdd",), session_extra=[cc_assistant("s", 5, [tool_use("Skill", {"skill": "tdd"}, "cs")], usage(1, 1)),
                                                          cc_tool_result("cs", "loaded", 6)])["directive_pairs"]
    top = o["top"][0]
    assert o["total"] == 1 and o["static"] == 1 and o["eligible"] == 0
    assert (top["skill"], top["skill_line"], top["instruction"], top["instruction_line"]) == ("tdd", 6, "CLAUDE.md", 3)
    assert top["judge"] == "static" and top["score_pct"] >= 45 and top["both_loaded"] is False
    assert "conflict" not in json.dumps(o) and "duplicate\"" not in json.dumps(o)


def devin_db(cli, tmp_path, rules_text, skill_body, load_skill=True):
    rules = tmp_path / "rules.md"
    rules.write_text(rules_text, encoding="utf-8")
    skill(cli.repo / ".cognition" / "skills", "hello", "Say hello nicely", body=skill_body)
    skill(cli.repo / ".cognition" / "skills", "other", "Another skill entirely", body=skill_body)
    db = dv.DevinDb(tmp_path / "sessions.db")
    chain = db.session("s1", str(cli.repo.resolve()))
    chain.add(dv.system(), dv.rules_loaded([str(rules)], 100), dv.user("one", slash="/hello" if load_skill else None),
              dv.skills_loaded(["hello", "other"]), dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
    chain.done()
    return db.path


def test_a_directive_pair_where_both_sides_were_loaded_is_eligible(cli, tmp_path):
    path = devin_db(cli, tmp_path, f"Rules\n{DIRECTIVE}.\n", f"Intro\n{DIRECTIVE} please.\n")
    _, out = cli.json(path, "--host", "devin")
    pairs = out["skills"]["overlaps"]["directive_pairs"]
    by_skill = {p["skill"]: p for p in pairs["top"]}
    assert by_skill["hello"]["judge"] == "eligible" and by_skill["hello"]["both_loaded"] is True
    assert by_skill["other"]["judge"] == "static" and by_skill["other"]["both_loaded"] is False     # offered, not loaded
    assert pairs["eligible"] == 1 and pairs["static"] == 1


def test_lines_inside_code_blocks_are_skipped(cli, tmp_path):
    body = f"Intro\n```\n{DIRECTIVE}\n```\nPlain text.\n"
    skill(cli.home / ".claude" / "skills", "tdd", "Test driven development", body=body)
    rule_file(cli, f"{DIRECTIVE}.\n")
    o = run(cli, tmp_path)
    assert o["directive_pairs"]["total"] == 0 and o["scanned"]["directive_lines"]["skills"] == 0


def test_only_directive_lines_are_paired(cli, tmp_path):
    skill(cli.home / ".claude" / "skills", "tdd", "Test driven development", body="The suite is large and slow to finish\nAlways run the full test suite before every commit.")
    rule_file(cli, "The suite is large and slow to finish\n")
    o = run(cli, tmp_path)
    assert o["scanned"]["directive_lines"]["skills"] == 1 and o["directive_pairs"]["total"] == 0


def test_a_planted_secret_in_a_skill_body_never_appears(cli, tmp_path):
    secret = "API_KEY=sk_" "live_abcdefghijklmnop0123456789"
    skill(cli.home / ".claude" / "skills", "leaky", "Handles credentials", body=f"Never print {secret} or the suite output.")
    rule_file(cli, f"Never print {secret} or the suite output.\n")
    path = write_jsonl(tmp_path / "s.jsonl", [listing(["leaky"], 0), cc_user("go", 1), cc_assistant("m", 9, usage=usage(1, 1))])
    for flag in ([], ["--no-redact"]):
        proc = cli.run(path, "--host", "claude-code", *flag)
        assert "sk_live" not in proc.stdout and "abcdefghijklmnop0123456789" not in proc.stdout and proc.returncode == 0


def test_directive_pair_lists_are_capped(cli, tmp_path):
    lines = "\n".join(f"Always run check number {i} before every commit" for i in range(12))
    skill(cli.home / ".claude" / "skills", "tdd", "Test driven development", body=lines)
    rule_file(cli, lines + "\n")
    pairs = run(cli, tmp_path)["directive_pairs"]
    assert len(pairs["top"]) == 10 and pairs["more"] == pairs["total"] - 10 and pairs["total"] >= 12


def test_repo_agents_md_and_always_on_cursor_rules_are_scanned(cli, tmp_path):
    skill(cli.home / ".claude" / "skills", "tdd", "Test driven development", body=f"{DIRECTIVE}.")
    (cli.repo / "AGENTS.md").write_text(f"{DIRECTIVE}.\n")
    rules = cli.repo / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "on.mdc").write_text(f"---\nalwaysApply: true\n---\n{DIRECTIVE}.\n")
    (rules / "off.mdc").write_text(f"---\nalwaysApply: false\n---\n{DIRECTIVE}.\n")
    o = run(cli, tmp_path)
    assert o["scanned"]["instruction_files"] == 2
    assert {p["instruction"] for p in o["directive_pairs"]["top"]} == {"AGENTS.md", ".cursor/rules/on.mdc"}


# ---- hooks ---------------------------------------------------------------------------------------------------------------

def settings(path, hooks):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}))


HOOK = [{"matcher": "Bash|Grep", "hooks": [{"type": "command", "command": "graphify hook-guard search --mandatory-flag"}]}]


def test_a_matcher_counts_the_sessions_matching_calls(cli, tmp_path):
    settings(cli.repo / ".claude" / "settings.json", {"PreToolUse": HOOK})
    calls = [("Bash", {"command": f"ls {i}"}) for i in range(4)] + [("Read", {"file_path": "/x/a.py"})]
    hooks = run(cli, tmp_path, calls=calls)["hooks"]
    assert hooks["files"] == 1 and hooks["total"] == 1
    h = hooks["top"][0]
    assert (h["event"], h["matcher"], h["command"]) == ("PreToolUse", "Bash|Grep", "graphify")
    assert (h["matched"], h["of_all_calls"], h["share_pct"]) == (4, 5, 80) and h["summary"] == "matched 4 of 4 shell calls"
    assert h["every_call_of"] == ["Bash"]


def test_the_full_hook_command_never_appears(cli, tmp_path):
    settings(cli.repo / ".claude" / "settings.json", {"PreToolUse": HOOK})
    path = write_jsonl(tmp_path / "s.jsonl", [listing(["x"], 0), cc_user("go", 1), cc_assistant("m", 9, usage=usage(1, 1))])
    proc = cli.run(path, "--host", "claude-code", "--no-redact")
    assert "graphify" in proc.stdout and "hook-guard" not in proc.stdout and "mandatory-flag" not in proc.stdout


def test_a_hook_matching_every_call_of_a_common_tool_is_listed_first(cli, tmp_path):
    settings(cli.repo / ".claude" / "settings.json", {"PostToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "a"}]}],
                                                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/bin/b --x"}]}]})
    calls = [("Bash", {"command": "ls"}), ("Read", {"file_path": "/x"}), ("Read", {"file_path": "/y"}), ("Read", {"file_path": "/z"})]
    hooks = run(cli, tmp_path, calls=calls)["hooks"]["top"]
    assert [h["command"] for h in hooks] == ["a", "b"] and hooks[0]["every_call_of"] == ["Read"]


def test_hooks_from_settings_local_and_the_user_file_are_read(cli, tmp_path):
    settings(cli.repo / ".claude" / "settings.local.json", {"Stop": [{"hooks": [{"type": "command", "command": "notify done"}]}]})
    settings(cli.home / ".claude" / "settings.json", {"PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "x"}]}]})
    hooks = run(cli, tmp_path, calls=[("Bash", {"command": "ls"})])["hooks"]
    assert hooks["files"] == 2 and hooks["total"] == 2
    events = {h["event"]: h for h in hooks["top"]}
    assert events["Stop"]["matched"] is None and events["PreToolUse"]["matched"] == 1


def test_no_hooks_found_is_reported_as_zero_not_unknown(cli, tmp_path):
    hooks = run(cli, tmp_path)["hooks"]
    assert hooks["files"] == 0 and hooks["total"] == 0


def test_hook_lists_are_capped(cli, tmp_path):
    groups = [{"matcher": f"Tool{i}", "hooks": [{"type": "command", "command": f"cmd{i}"}]} for i in range(13)]
    settings(cli.repo / ".claude" / "settings.json", {"PreToolUse": groups})
    hooks = run(cli, tmp_path)["hooks"]
    assert hooks["total"] == 13 and len(hooks["top"]) == 10 and hooks["more"] == 3


def test_a_host_that_declares_no_hook_files_says_unknown(cli, tmp_path):
    path = write_jsonl(tmp_path / "rollout-x.jsonl", [{"type": "session_meta", "payload": {"id": "i", "cwd": str(cli.repo), "cli_version": "0.1.0"}}])
    code, out = cli.json(path, "--host", "codex")
    assert out["skills"]["overlaps"]["hooks"] == {"unknown": "hooks unknown for this host"}


# ---- descriptions from the transcript's own skill list -------------------------------------------------------------------

def listing_with(descriptions, t=0):
    """A Claude Code skill_listing attachment: names[] plus one '- name: description' line per skill (and two other lines)."""
    content = "TRIGGER notes for the model.\n" + "\n".join(f"- {n}: {d}" for n, d in descriptions.items()) + "\nSKIP notes."
    rec = listing(list(descriptions), t)
    rec["attachment"]["content"] = content
    return rec


def run_listing(cli, tmp_path, descriptions, *args, session_extra=()):
    path = write_jsonl(tmp_path / "s.jsonl", [listing_with(descriptions), cc_user("go", 1), *session_extra, cc_assistant("mz", 90, usage=usage(1, 1))])
    code, out = cli.json(path, "--host", "claude-code", *args)
    assert code == 0
    return out["skills"]["overlaps"]


def twins(**extra):
    descriptions = {"plug:review-a": REVIEW_A, "plug:review-b": REVIEW_B, "other": "Calibrate telescope mirrors with interferometry"}
    descriptions.update(extra)
    return descriptions


def test_a_missing_file_uses_the_listing_description(cli, tmp_path):
    o = run_listing(cli, tmp_path, twins())
    assert o["scanned"] == {"skills": 3, "from_files": 0, "from_listing": 3, "unreadable": 0, "not_scanned": 0,
                            "instruction_files": o["scanned"]["instruction_files"], "directive_lines": o["scanned"]["directive_lines"]}
    p = pair(o, "plug:review-a", "plug:review-b")
    assert p and p["score_pct"] >= 70 and p["a_status"] == "offered"


def test_a_file_wins_over_the_listing_and_is_counted_once(cli, tmp_path):
    skill(cli.home / ".claude" / "skills", "hello", REVIEW_A)
    o = run_listing(cli, tmp_path, {"hello": "Calibrate telescope mirrors", "twin": REVIEW_B, "z": "Origami folding guide for paper cranes"})
    assert o["scanned"]["from_files"] == 1 and o["scanned"]["from_listing"] == 2 and o["scanned"]["skills"] == 3
    assert pair(o, "hello", "twin")["score_pct"] >= 70                      # the file's description, not the listing's


def test_a_listing_name_with_a_plugin_prefix_matches_its_file(cli, tmp_path):
    skill(cli.home / ".claude" / "skills", "tdd", "Test driven development workflow")
    o = run_listing(cli, tmp_path, {"mattpocock-skills:tdd": "Something else entirely about pottery"})
    assert o["scanned"]["from_files"] == 1 and o["scanned"]["from_listing"] == 0


def test_an_offered_name_with_no_file_and_no_listing_line_is_not_scanned(cli, tmp_path):
    rec = listing_with({"a": "Alpha text about origami"})
    rec["attachment"]["names"] = ["a", "ghost"]
    path = write_jsonl(tmp_path / "s.jsonl", [rec, cc_user("go", 1), cc_assistant("mz", 90, usage=usage(1, 1))])
    o = cli.json(path, "--host", "claude-code")[1]["skills"]["overlaps"]
    assert o["scanned"]["skills"] == 1 and o["scanned"]["not_scanned"] == 1


def test_a_subagent_skill_listing_adds_no_descriptions(cli, tmp_path):
    main = write_jsonl(tmp_path / "proj" / "s1.jsonl", [listing_with({"a": "Alpha text about origami"}), cc_user("go", 1),
                                                         cc_assistant("m", 3, usage=usage(1, 1))])
    write_jsonl(tmp_path / "proj" / "s1" / "subagents" / "agent-x.jsonl", [listing_with({"only-sub": "Sub text about volcano"}, 4), cc_user("brief", 5, uuid="su")])
    o = cli.json(main, "--host", "claude-code")[1]["skills"]["overlaps"]
    assert o["scanned"]["skills"] == 1


CODEX_LISTING = ("<skills_instructions>\n## Skills\n### Skill roots\n- `r0` = `/x/.codex/skills`\n### Available skills\n"
                 f"- review-a: {REVIEW_A}. (file: r0/review-a/SKILL.md)\n- review-b: {REVIEW_B}. (file: r0/review-b/SKILL.md)\n"
                 "- data:analyze: Answer data questions about pottery. (file: r0/analyze/SKILL.md)\n"
                 "### How to use skills\n- Discovery: read the list.\n</skills_instructions>")


def test_codex_descriptions_come_from_the_developer_message_without_the_file_suffix(cli, tmp_path):
    from fixtures.builders import cx_developer, cx_meta, cx_user
    path = write_jsonl(tmp_path / "rollout-x.jsonl", [cx_meta("i", cli.repo), cx_developer(CODEX_LISTING, 0), cx_user("go", 1)])
    code, out = cli.json(path, "--host", "codex")
    o = out["skills"]["overlaps"]
    assert code == 0 and o["scanned"]["from_listing"] == 3
    p = pair(o, "review-a", "review-b")
    assert p and p["score_pct"] >= 70 and "file" not in p["shared_terms"]


def test_devin_descriptions_come_from_skills_loaded(cli, tmp_path):
    db = dv.DevinDb(tmp_path / "sessions.db")
    chain = db.session("s1", str(cli.repo.resolve()))
    chain.add(dv.system(), dv.rules_loaded(["~/a.md"]), dv.user("one"),
              dv.skills_loaded(["review-a", "review-b"], hidden=["explicit"], descriptions={"review-a": REVIEW_A, "review-b": REVIEW_B, "explicit": "Pottery glazing recipes"}),
              dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
    chain.done()
    _, out = cli.json(db.path, "--host", "devin")
    o = out["skills"]["overlaps"]
    assert o["scanned"]["from_listing"] == 3 and o["scanned"]["not_scanned"] == 0
    assert pair(o, "review-a", "review-b")["score_pct"] >= 70


# ---- instructions Claude Code recorded as loaded ---------------------------------------------------------------------------

def instructions(path, content):
    return {"type": "attachment", "uuid": "i1", "sessionId": "s1", "timestamp": "2026-11-01T10:00:00.000Z",
            "attachment": {"type": "instructions", "files": [{"path": str(path), "type": "User", "content": content}]}}


def instr_run(cli, tmp_path, rule_text, loaded=True, disk=None):
    rules = cli.home / ".claude" / "CLAUDE.md"
    if disk is not None:
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text(disk, encoding="utf-8")
    skill(cli.home / ".claude" / "skills", "tdd", "Test driven development", body=f"Intro line.\n{DIRECTIVE}.\n")
    extra = [cc_assistant("s", 5, [tool_use("Skill", {"skill": "tdd"}, "cs")], usage(1, 1)), cc_tool_result("cs", "loaded", 6)] if loaded else []
    path = write_jsonl(tmp_path / "s.jsonl", [instructions(rules, rule_text), listing(["tdd"], 0), cc_user("go", 1), *extra, cc_assistant("mz", 90, usage=usage(1, 1))])
    code, out = cli.json(path, "--host", "claude-code")
    assert code == 0
    return out, cli.run(path, "--host", "claude-code", "--no-redact").stdout


def test_an_instruction_file_recorded_as_loaded_makes_its_pair_eligible(cli, tmp_path):
    out, text = instr_run(cli, tmp_path, f"# Rules\n{DIRECTIVE}, then push.\n")           # the file is not on disk: the content is used
    o = out["skills"]["overlaps"]
    assert o["scanned"]["instruction_files"] == 1
    top = o["directive_pairs"]["top"][0]
    assert (top["skill"], top["instruction"], top["instruction_line"], top["judge"], top["both_loaded"]) == ("tdd", "~/.claude/CLAUDE.md", 2, "eligible", True)
    assert o["directive_pairs"]["eligible"] == 1 and DIRECTIVE not in text and "then push" not in text
    assert o["instruction_files"] == {"total": 1, "loaded": 1, "more": 0, "top": [{"path": "~/.claude/CLAUDE.md", "loaded": True, "directive_lines": 1}]}
    assert o["directive_pairs"]["best_pct"] >= 45


def test_a_skill_that_was_not_loaded_stays_static_even_with_a_loaded_instruction(cli, tmp_path):
    out, _ = instr_run(cli, tmp_path, f"{DIRECTIVE}.\n", loaded=False)
    top = out["skills"]["overlaps"]["directive_pairs"]["top"][0]
    assert top["judge"] == "static" and top["both_loaded"] is False


def test_the_recorded_content_beats_the_file_on_disk(cli, tmp_path):
    out, _ = instr_run(cli, tmp_path, f"{DIRECTIVE}.\n", disk="Nothing relevant here at all\n")
    assert out["skills"]["overlaps"]["directive_pairs"]["total"] == 1


def test_a_repo_file_with_no_loaded_record_is_static(cli, tmp_path):
    rules = cli.repo / "CLAUDE.md"
    rules.write_text(f"{DIRECTIVE}.\n")
    skill(cli.home / ".claude" / "skills", "tdd", "Test driven development", body=f"{DIRECTIVE}.\n")
    o = run(cli, tmp_path, names=("tdd",), session_extra=[cc_assistant("s", 5, [tool_use("Skill", {"skill": "tdd"}, "cs")], usage(1, 1)), cc_tool_result("cs", "loaded", 6)])
    assert o["directive_pairs"]["top"][0]["judge"] == "static"


def test_instruction_content_is_never_printed(cli, tmp_path):
    secret = "API_KEY=sk_" "live_abcdefghijklmnop0123456789"
    out, text = instr_run(cli, tmp_path, f"Never print {secret}. {DIRECTIVE}.\n")
    assert "sk_live" not in text and "abcdefghijklmnop0123456789" not in text


# ---- realistic, long descriptions ---------------------------------------------------------------------------------------------

LONG = {
    "charts-a": "Create publication-quality chart visualizations with Python. Use when turning query results or a DataFrame into a chart or plot, selecting the right chart type for a trend or comparison, generating a plot for a report or presentation, or needing an interactive chart with hover and zoom.",
    "charts-b": "Create effective data visualizations and chart plots with Python (matplotlib, seaborn, plotly). Use when building charts, choosing the right chart type for a dataset, creating publication-quality plots for a report, or applying design principles like accessibility and color theory.",
    "charts-c": "Use this skill whenever you are about to create ANY chart, plot, dashboard, or data visualization, in ANY output medium: an HTML artifact, inline SVG, Python plotting code in any library, or an image of a chart for a report. Read it BEFORE writing the first line of chart code, choosing chart colors, picking a chart type, or laying out a dashboard. Produces accessible visualizations that read as one system, consistent in light and dark. Triggers on: chart, plot, visualization, dashboard, palette, legend, axis, tooltip, heatmap.",
    "sql-a": "Write correct, performant SQL across all major data warehouse dialects. Use when writing queries, optimizing slow SQL, translating between dialects, or building complex analytical queries with CTEs, window functions, or aggregations.",
    "sql-b": "Write optimized SQL for your dialect with best practices. Use when translating a natural-language data need into SQL, building a multi-CTE query with joins and aggregations, optimizing a query against a large partitioned table, or getting dialect-specific syntax.",
    "browser-a": "Read this skill before the first step that uses the built-in browser, the browser pane inside the desktop app, covering tabs, reading pages as text, site approvals, and what the pane cannot open. It is not for the external browser extension, which has its own skill.",
    "browser-b": "Read this skill before the first step that uses the browser extension, the external browser with the person's own sign-ins. It covers loading the tools, checking open tabs, site permissions, recordings, console logs, and when to stop and ask. It is not for the built-in browser pane, which has its own skill.",
    "docs-a": "Editable documents people share and comment on: the default for any document, report, proposal, resume, letter, contract, template or guide that people keep, share, or collaborate on, with tracked changes and comments.",
    "docs-b": "Use this skill whenever the user wants to create, read, edit, or manipulate Word documents: reports, letters and templates, with tracked changes, comments, find-and-replace, and inserting or replacing images in the document file.",
    "pdf": "Use this skill whenever the user wants to do anything with PDF files: reading or extracting text and tables, merging, splitting, rotating pages, adding watermarks, filling forms, encrypting, extracting images, and OCR on scanned pages.",
    "slides": "Build slide decks and presentations from outlines, with speaker notes, layouts, themes and exportable files for meetings and talks.",
    "calendar": "Schedule meetings, propose times, resolve conflicts between invitations, and summarise the week ahead from the person's calendar.",
    "email": "Draft, summarise and triage email threads, extract action items, and prepare replies in the person's own tone.",
    "pottery": "Plan kiln firing schedules, glaze combinations and clay body recipes for ceramics studios.",
}


def test_long_realistic_descriptions_find_the_real_overlaps_and_little_else(cli, tmp_path):
    o = run_listing(cli, tmp_path, LONG)
    assert o["scanned"]["from_listing"] == len(LONG)
    found = {frozenset((p["a"], p["b"])) for p in o["skill_pairs"]["top"]}
    for a, b in (("charts-a", "charts-b"), ("charts-b", "charts-c"), ("charts-a", "charts-c"), ("sql-a", "sql-b"), ("browser-a", "browser-b"), ("docs-a", "docs-b")):
        assert frozenset((a, b)) in found, (a, b)
    assert o["skill_pairs"]["threshold"] == 0.15 and o["skill_pairs"]["total"] <= 12


def test_the_best_directive_score_is_reported_even_when_nothing_crosses_the_threshold(cli, tmp_path):
    skill(cli.home / ".claude" / "skills", "tdd", "Test driven development", body="Always run the full test suite before every commit.")
    rule_file(cli, "Never delete the production database without a backup copy.\n")
    pairs = run(cli, tmp_path)["directive_pairs"]
    assert pairs["total"] == 0 and 0 <= pairs["best_pct"] < 45


def test_instruction_files_are_listed_with_loaded_status_and_capped(cli, tmp_path):
    (cli.repo / "AGENTS.md").write_text("Always check the docs before every commit.\n")
    rule_file(cli, "Never skip the tests.\n")
    listed = run(cli, tmp_path)["instruction_files"]
    assert listed["total"] == 2 and listed["loaded"] == 0 and {f["path"] for f in listed["top"]} == {"AGENTS.md", "CLAUDE.md"}
    assert all(f["loaded"] is False for f in listed["top"])
