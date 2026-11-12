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


def test_plugin_skill_folders_are_part_of_the_corpus(cli, tmp_path):
    skill(cli.home / ".claude" / "plugins" / "cache" / "mkt" / "plug" / "1.0" / "skills", "from-plugin", "Alpha description about origami")
    assert run(cli, tmp_path)["scanned"]["skills"] == 1


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
    o = run(cli, tmp_path)["skill_pairs"]
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
