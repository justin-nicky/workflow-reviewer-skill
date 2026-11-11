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


def test_cursor_second_chat_uses_a_skill_automatically(cli):
    code, out = cli.json(one("cursor-chat2/*/"), "--host", "cursor")
    assert code == 0 and out["window"]["turns_total"] == 3
    assert [(s["name"], s["detected_by"]) for s in out["skills"]["loaded"]] == [("mango-greeting", "read SKILL.md")]
