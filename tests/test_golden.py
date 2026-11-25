"""Golden replicas: synthetic copies of the real conformance sessions (see fixtures/golden/README.md)."""
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden"
COARSE = "coarse: prompt timestamps only, minute precision; last turn's duration unknown"


def one(pattern):
    return next(iter(sorted(GOLDEN.glob(pattern))))


def test_claude_code_session_with_a_subagent(cli):
    session = one("claude-code/*.jsonl").with_suffix("")
    code, out = cli.json(session, "--host", "claude-code")
    cov, skills = out["coverage"], out["skills"]
    assert code == 0 and cov["variant"] == "default" and cov["version_note"] is None
    assert cov["drift"]["coverage_pct"] == 100.0 and cov["drift"]["unseen_types"] == {}
    assert cov["ignored_types"]["relocated"] == 5 and cov["ignored_types"]["tools-used"] == 4
    assert (out["window"]["turns_total"], out["verify"]["total"], out["tools"]["errors"],
            out["interruptions"]["count"]) == (7, 1, 1, 1)
    sub = out["actors"][1]
    assert sub["actor"].startswith("subagent:") and sub["link"] == "tool_use_id" and sub["models"] == ["claude-sonnet-5-5"]
    assert skills["offered"]["total"] == 56 and "hello" in skills["offered"]["names"]
    assert "explicit-only" not in skills["offered"]["names"]
    assert skills["used"] == ["hello", "explicit-only"] and skills["used_not_offered"] == ["explicit-only"]


def test_codex_desktop_parent_and_subagent(cli):
    files = sorted(GOLDEN.glob("codex-desktop/rollout-*.jsonl"))
    assert len(files) == 2
    code, out = cli.json(*files, "--host", "codex")
    cov = out["coverage"]
    assert code == 0 and cov["variant"] == "desktop" and cov["drift"]["ok"] and cov["drift"]["coverage_pct"] >= 90
    assert (out["window"]["turns_total"], out["interruptions"]["count"]) == (6, 1)
    assert out["tools"]["by_category"] == {"shell": 5, "other": 2, "agent": 1}
    assert out["verify"]["total"] == 1 and out["tools"]["errors"] == 1
    assert out["skills"]["used"] == ["hello", "explicit-only"]
    main, sub = out["actors"]
    assert main["tokens"] == {"fresh_input": 29522, "output": 906, "cache_read": 193920, "cache_write": 0, "api_calls": 12}
    assert main["tool_calls"] == 7 and main["verify_runs"] == 1 and main["models"] and sub["models"]
    assert sub["link"] == "parent_thread_id" and sub["brief_bytes"] > 0
    assert (sub["history_from_ordinal"], sub["history_records_skipped"]) == (28, 28)
    assert sub["history_note"] == "copied history skipped: 27 records (ordinals 1–27); session_meta read for linking only"
    assert out["tokens"]["method"] == "token_usage_record, deduplicated by response_id"
    assert "warnings" not in cov


def test_cursor_first_chat_with_a_subagent(cli):
    code, out = cli.json(one("cursor-chat1/*/"), "--host", "cursor")
    assert code == 0 and out["coverage"]["drift"]["coverage_pct"] == 100.0
    assert out["window"]["turns_total"] == 7 and out["interruptions"] == {"count": 1, "turns": [7]}
    assert out["turn_errors"] == {"count": 0, "turns": []}
    assert out["skills"]["used"] == ["hello", "explicit-only", "explicit-agents"]
    assert [a["actor"].split(":")[0] for a in out["actors"]] == ["main", "subagent"]
    assert out["actors"][1]["link"] == "markdown_link"
    assert out["verify"]["total"] == 1 and out["tools"]["errors"] is None
    assert out["time"]["wall_s"] == 180 and out["time"]["precision"] == COARSE
    assert out["time"]["wall_range_s"] == [120, 240]


def test_cursor_second_chat_uses_a_skill_automatically(cli):
    code, out = cli.json(one("cursor-chat2/*/"), "--host", "cursor")
    assert code == 0 and out["window"]["turns_total"] == 3
    assert out["time"]["wall_range_s"] == [0, 60]
    md = cli.run(one("cursor-chat2/*/"), "--host", "cursor", fmt="md").stdout
    assert "under 1 min" in md
    assert [(s["name"], s["detected_by"]) for s in out["skills"]["loaded"]] == [("mango-greeting", "read SKILL.md")]


# ---- Devin: SQLite databases, one session each -------------------------------------------------------------------

def devin(cli, name):
    return cli.json(GOLDEN / f"devin-{name}" / "sessions.db", "--host", "devin", "--session-id", f"golden-devin-{name}")


def test_devin_session_with_a_subagent(cli):
    code, out = devin(cli, "subagent")
    cov = out["coverage"]
    assert code == 0 and cov["drift"]["ok"] and cov["drift"]["coverage_pct"] == 100.0 and "warnings" not in cov
    assert cov["storage"] == {"kind": "sqlite", "off_chain_nodes": 36, "subagent_chains": 1, "nodes_shared_with_main": 0,
                              "schema_version": 20, "schema_version_source": "refinery_schema_history"}
    assert (out["window"]["turns_total"], out["verify"]["total"], out["tools"]["errors"], out["interruptions"]["count"]) == (4, 1, 0, 0)
    main, sub = out["actors"]
    assert main["tool_calls"] == 8 and out["tools"]["by_category"]["search"] == 4
    assert {k: main["tokens"][k] for k in ("fresh_input", "cache_read", "output", "api_calls")} == \
        {"fresh_input": 16015, "cache_read": 121600, "output": 1294, "api_calls": 10}
    assert main["tokens"]["context_check"] == {"calls": 10, "context_input_total": 137615, "mismatches": 0}
    assert sub["link"] == "subagent_heads" and sub["tool_calls"] == 4 and sub["models"] == ["swe-2-flash-medium"]
    assert sub["tokens"]["api_calls"] == 2 and sub["tokens"]["output"] == 296
    skills = out["skills"]
    assert skills["offered"]["total"] == 5 and "hello" in skills["offered"]["names"] and skills["used"] == ["hello"]
    assert skills["loaded"][0]["detections"] == [{"by": "slash command", "turn": 2}, {"by": "skill tool call", "turn": 2}]


def test_devin_failed_command_and_interruption(cli):
    code, out = devin(cli, "error-interrupted")
    assert code == 0 and out["window"]["turns_total"] == 2 and out["tools"]["by_category"] == {"shell": 2, "read": 1}
    assert out["tools"]["errors"] == 1 and out["interruptions"] == {"count": 1, "turns": [2]}
    tokens = out["actors"][0]["tokens"]
    assert (tokens["fresh_input"], tokens["cache_read"], tokens["output"], tokens["api_calls"]) == (4735, 47872, 168, 5)
    assert out["coverage"]["storage"]["off_chain_nodes"] == 21


def test_devin_explicit_only_skill(cli):
    code, out = devin(cli, "explicit-only")
    assert code == 0 and out["window"]["turns_total"] == 1
    assert out["skills"]["used"] == ["explicit-only", "hello"] and out["skills"]["used_not_offered"] == ["explicit-only"]
    tokens = out["actors"][0]["tokens"]
    assert (tokens["fresh_input"], tokens["cache_read"], tokens["output"], tokens["api_calls"]) == (1134, 25088, 316, 2)
    assert out["coverage"]["storage"]["off_chain_nodes"] == 18


# ---- labels: Devin and Cline are experimental; Claude Code, Codex and Cursor are verified ---------------------------------------

def test_devin_is_labelled_experimental(cli):
    _, out = devin(cli, "subagent")
    assert out["coverage"]["label"] == "experimental"


def test_the_main_hosts_stay_verified(cli):
    _, out = cli.json(one("claude-code/*.jsonl").with_suffix(""), "--host", "claude-code")
    assert out["coverage"]["label"] == "verified"


def test_an_experimental_adapter_is_named_in_the_report(cli, tmp_path):
    import json
    from test_report import FINDINGS
    cli.env_extra["TMPDIR"] = str(tmp_path)
    proc = cli.run(GOLDEN / "devin-subagent" / "sessions.db", "--host", "devin", "--session-id", "golden-devin-subagent",
                   "--report", "-", "--no-open", "--report-format", "md", stdin=json.dumps(FINDINGS))
    assert "(experimental adapter)" in proc.stdout
