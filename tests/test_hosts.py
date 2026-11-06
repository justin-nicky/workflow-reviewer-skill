import json
import re

from fixtures.builders import (at, cc_assistant, cc_user, cu_assistant, cu_turn_ended, cu_user, cx_call,
                               cx_developer, cx_event, cx_injected_user, cx_meta,
                               cx_output, cx_tokens, cx_user, usage, write_jsonl)


def codex_rollout(cli, thread_id, repo=None, records=None):
    codex_home = cli.home / ".codex"
    cli.env_extra["CODEX_HOME"] = str(codex_home)
    path = codex_home / "sessions" / "2026" / "11" / "01" / f"rollout-2026-11-01T10-00-00-{thread_id}.jsonl"
    return write_jsonl(path, [cx_meta(thread_id, repo or cli.repo), *(records or [])])


CODEX_TASK = [
    cx_user("add rate limiting", 1),
    cx_call("pytest -q", "k1", 5),
    cx_output("k1", "1 failed", 1, 9),
    cx_tokens(1000, 800, 50, 10),
    cx_call("pytest -q", "k2", 20),
    cx_output("k2", "ok", 0, 25),
    cx_tokens(1500, 1000, 70, 26),
]


def test_codex_thread_id_env_finds_the_rollout_and_maps_tokens(cli):
    codex_rollout(cli, "019abc", records=CODEX_TASK)
    codex_rollout(cli, "other", records=[cx_user("unrelated", 1)])
    cli.env_extra["CODEX_THREAD_ID"] = "019abc"
    code, out = cli.json()
    assert code == 0
    assert out["coverage"]["host"] == "codex"
    assert out["tokens"]["fresh_input"] == 200 + 500      # input minus cached
    assert out["tokens"]["cache_read"] == 1800
    assert out["tokens"]["output"] == 120
    assert out["verify"]["total"] == 2
    assert out["tools"]["errors"] == 1
    assert out["tools"]["by_category"] == {"shell": 2}


def test_codex_repo_fallback_matches_by_cwd(cli, tmp_path):
    codex_rollout(cli, "mine", records=CODEX_TASK)
    codex_rollout(cli, "elsewhere", repo=tmp_path / "other-repo", records=[cx_user("x", 1)])
    code, out = cli.json("--host", "codex")
    assert code == 0 and out["tokens"]["output"] == 120


def test_cursor_has_reduced_capabilities_and_says_so(cli, tmp_path):
    path = write_jsonl(tmp_path / "cursor.jsonl", [
        cu_user("fix the bug"),
        cu_assistant("looking", [("Read", {"path": "a.py"})]),
        cu_assistant(calls=[("Shell", {"command": "npm test"})]),
    ])
    code, out = cli.json(path, "--host", "cursor")
    assert code == 0
    assert out["tokens"]["not_measurable"]
    assert out["time"]["not_measurable"]
    assert out["tools"]["errors"] is None
    assert out["tools"]["by_category"] == {"read": 1, "shell": 1}
    assert out["verify"]["total"] == 1
    assert "has_tokens" in out["coverage"]["not_measurable"][0] or out["coverage"]["not_measurable"]


def test_host_is_autodetected_from_the_transcript(cli, tmp_path):
    cc = write_jsonl(tmp_path / "a.jsonl", [cc_user("go", 0), cc_assistant("m", 1, usage=usage(1, 1))])
    cx = write_jsonl(tmp_path / "b.jsonl", [cx_meta("i", cli.repo), *CODEX_TASK])
    cu = write_jsonl(tmp_path / "c.jsonl", [cu_user("go"), cu_assistant("hi")])
    assert cli.json(cc)[1]["coverage"]["host"] == "claude-code"
    assert cli.json(cx)[1]["coverage"]["host"] == "codex"
    assert cli.json(cu)[1]["coverage"]["host"] == "cursor"


# ---- drift ---------------------------------------------------------------------------

def drifted(tmp_path):
    records = [cc_user("go", 0)]
    for i in range(1, 8):
        rec = cc_assistant(f"m{i}", i * 5, usage=usage(1, 10))
        rec["type"] = "assistant_v2"          # a renamed record type
        records.append(rec)
    return write_jsonl(tmp_path / "drift.jsonl", records)


def test_renamed_record_type_triggers_drift_warning_and_exit_code(cli, tmp_path):
    code, out = cli.json(drifted(tmp_path), "--host", "claude-code")
    assert code == 2
    d = out["coverage"]["drift"]
    assert d["ok"] is False
    assert d["coverage_pct"] < 90
    assert d["unseen_types"] == {"assistant_v2": 7}
    assert "2.1.295 to 2.1.309" in d["warning"]
    assert out["tokens"]["api_calls"] == 0         # keeps going with what still parses


def test_clean_transcript_has_no_drift(cli, tmp_path):
    path = write_jsonl(tmp_path / "ok.jsonl", [cc_user("go", 0), cc_assistant("m", 1, usage=usage(1, 1))])
    code, out = cli.json(path, "--host", "claude-code")
    assert code == 0 and out["coverage"]["drift"]["ok"] is True


def test_missing_usage_is_a_required_field_failure(cli, tmp_path):
    path = write_jsonl(tmp_path / "nousage.jsonl", [cc_user("go", 0), cc_assistant("m", 1)])
    code, out = cli.json(path, "--host", "claude-code")
    assert code == 2
    assert "usage" in out["coverage"]["drift"]["missing_fields"]


def test_drift_threshold_is_configurable(cli, tmp_path):
    code, _ = cli.json(drifted(tmp_path), "--host", "claude-code", "--drift-threshold", "5")
    assert code == 0


CUSTOM_MAPPING = """
host = "claude-code"
tested_versions = "drafted"
type_path = "type"
[capabilities]
has_tokens = true
has_timestamps = true
has_tool_results = true
has_ids = true
[fields]
timestamp = "timestamp"
event_id = "uuid"
msg_id = "message.id"
[[records]]
match = { type = "user" }
role = "user"
text = ["message.content"]
[[records]]
match = { type = "assistant_v2" }
role = "assistant"
usage_input = "message.usage.input_tokens"
usage_output = "message.usage.output_tokens"
"""


def test_a_mapping_on_stdin_restores_coverage(cli, tmp_path):
    code, out = cli.json(drifted(tmp_path), "--host", "claude-code", "--mapping", "-", stdin=CUSTOM_MAPPING)
    assert code == 0
    assert out["coverage"]["drift"]["ok"] is True
    assert out["tokens"]["output"] == 70
    assert out["coverage"]["mapping_source"] == "override"


def test_mapping_values_are_inert(cli, tmp_path):
    marker = tmp_path / "pwned"
    evil = CUSTOM_MAPPING.replace('text = ["message.content"]',
                                  f'text = ["__import__(\'os\').system(\'touch {marker}\')"]')
    path = write_jsonl(tmp_path / "t.jsonl", [cc_user(f"$(touch {marker})", 0)])
    cli.json(path, "--host", "claude-code", "--mapping", "-", stdin=evil)
    assert not marker.exists()


def test_sample_gives_raw_lines_for_drafting_a_mapping(cli, tmp_path):
    code, out = cli.json(drifted(tmp_path), "--sample", "3")
    assert len(out["sample"]) == 3


def test_zst_rollouts_are_reported_unsupported(cli, tmp_path):
    path = tmp_path / "rollout-1.jsonl.zst"
    path.write_bytes(b"\x28\xb5\x2f\xfd")
    code, out = cli.json(path)
    assert code == 4
    assert out["unsupported"][0].endswith(".zst")


def test_unknown_format_is_flagged_as_drift(cli, tmp_path):
    path = write_jsonl(tmp_path / "weird.jsonl", [{"kind": "x", "n": i} for i in range(5)])
    code, out = cli.json(path)
    assert code == 2
    assert out["coverage"]["drift"]["unseen_types"]


def test_cursor_subagent_that_also_appears_as_its_own_session_is_counted_once(cli, tmp_path):
    parent = write_jsonl(tmp_path / "p" / "p.jsonl", [cu_user("fix the bug"), cu_assistant("on it")])
    sub = [cu_user("run the checks"), cu_assistant(calls=[("Shell", {"command": "npm test"})])]
    write_jsonl(tmp_path / "p" / "p" / "subagents" / "sub1.jsonl", sub)       # listed under the parent
    twin = write_jsonl(tmp_path / "sub1-session.jsonl", sub)                  # same run as its own session
    for order in ([parent, twin], [twin, parent]):
        _, out = cli.json(*order, "--host", "cursor")
        assert out["verify"]["total"] == 1
        assert out["tools"]["by_category"] == {"shell": 1}


# ---- phase 0: structures observed in real transcripts (reproduced synthetically) ----------

def test_claude_code_housekeeping_record_types_do_not_count_as_drift(cli, tmp_path):
    extras = [{"type": t, "sessionId": "s1", "timestamp": at(1)}
              for t in ("api-request", "api-request-blob", "api-request-shape", "mode", "cost-state",
                        "atis-latch", "bridge-session", "file-history-delta")]
    path = write_jsonl(tmp_path / "s.jsonl", [
        cc_user("go", 0), *extras, *extras, cc_assistant("m", 5, usage=usage(1, 1))])
    code, out = cli.json(path, "--host", "claude-code")
    assert code == 0, out["coverage"]["drift"]
    assert out["coverage"]["drift"]["unseen_types"] == {}


def test_codex_counts_real_user_turns_not_injected_context(cli, tmp_path):
    path = write_jsonl(tmp_path / "rollout-x.jsonl", [
        cx_meta("i", cli.repo),
        cx_developer("system rules", 1),
        cx_injected_user("<environment_context>cwd</environment_context>", 1),
        cx_user("first task", 2), cx_tokens(10, 0, 5, 3),
        cx_user("second task", 50), cx_tokens(10, 0, 5, 53),
    ])
    code, out = cli.json(path, "--host", "codex")
    assert code == 0, out["coverage"]["drift"]
    assert [t["preview"] for t in out["turns"]] == ["first task", "second task"]


def test_codex_exit_code_is_read_from_the_output_text(cli, tmp_path):
    path = write_jsonl(tmp_path / "rollout-x.jsonl", [
        cx_meta("i", cli.repo), cx_user("go", 1),
        cx_call("pytest -x", "k1", 2), cx_output("k1", "boom", 1, 3),
        cx_call("pytest -x", "k2", 4), cx_output("k2", "boom", 1, 5),
        cx_call("ls", "k3", 6), cx_output("k3", "fine", 0, 7),
    ])
    _, out = cli.json(path, "--host", "codex")
    assert out["tools"]["errors"] == 2
    assert out["tools"]["retried_failures"] == [{"command": "pytest -x", "times": 2}]


def test_codex_repeated_token_count_events_are_counted_once(cli, tmp_path):
    total1 = {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}
    total2 = {"input_tokens": 300, "cached_input_tokens": 0, "output_tokens": 30}
    path = write_jsonl(tmp_path / "rollout-x.jsonl", [
        cx_meta("i", cli.repo), cx_user("go", 1),
        cx_tokens(100, 0, 10, 2, total=total1),
        cx_tokens(100, 0, 10, 3, total=total1),          # same cumulative total re-emitted
        cx_tokens(200, 0, 20, 4, total=total2),
    ])
    _, out = cli.json(path, "--host", "codex")
    assert out["tokens"]["fresh_input"] == 300
    assert out["tokens"]["output"] == 30
    assert out["tokens"]["api_calls"] == 2


def test_cursor_turn_ended_rows_and_strreplace_edits(cli, tmp_path):
    path = write_jsonl(tmp_path / "c.jsonl", [
        cu_user("edit it"),
        cu_assistant(calls=[("StrReplace", {"path": "a.py"}), ("Write", {"path": "b.py"})]),
        cu_turn_ended(),
    ])
    code, out = cli.json(path, "--host", "cursor")
    assert code == 0, out["coverage"]["drift"]
    assert out["tools"]["by_category"] == {"edit": 2}
    assert out["tools"]["edits_per_file"] == {"a.py": 1, "b.py": 1}


def test_cursor_subagents_beside_the_transcript_are_found_and_attached_by_brief(cli, tmp_path):
    sid = tmp_path / "abc123"
    main = write_jsonl(sid / "abc123.jsonl", [
        cu_user("first task"), cu_assistant("ok"),
        cu_user("second task"),
        cu_assistant(calls=[("Task", {"prompt": "run the checks please", "description": "checks"})]),
    ])
    write_jsonl(sid / "subagents" / "sub-1.jsonl", [
        cu_user("run the checks please"), cu_assistant(calls=[("Shell", {"command": "npm test"})])])
    _, out = cli.json(main, "--host", "cursor")
    assert [a["actor"] for a in out["actors"]] == ["main", "subagent:sub-1"]
    by_turn = {t["turn"]: t for t in out["turns"]}
    assert by_turn[1]["tool_calls"] == 0
    assert by_turn[2]["tool_calls"] == 2            # the Task call plus the subagent's shell call
    assert by_turn[2]["verify_runs"] == 1


def test_unicode_line_separators_inside_a_record_do_not_split_it(cli, tmp_path):
    rec = cc_user("first second\x0cthird\x1dfourth", 0)
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps(rec, ensure_ascii=False) + "\n"
                    + json.dumps(cc_assistant("m", 5, usage=usage(1, 1)), ensure_ascii=False) + "\n",
                    encoding="utf-8")
    code, out = cli.json(path, "--host", "claude-code")
    assert code == 0
    assert out["coverage"]["drift"]["coverage_pct"] == 100.0
    assert out["tokens"]["api_calls"] == 1


# ---- ignored types stay visible; versions come from real transcripts ---------------------

def test_ignored_record_types_are_reported_with_counts(cli, tmp_path):
    housekeeping = [{"type": "mode", "sessionId": "s", "timestamp": at(1)}] * 3 + \
                   [{"type": "api-request", "sessionId": "s", "timestamp": at(1)}] * 2
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), *housekeeping,
                                              cc_assistant("m", 5, usage=usage(1, 1))])
    code, out = cli.json(path, "--host", "claude-code")
    assert code == 0
    assert out["coverage"]["ignored_types"] == {"mode": 3, "api-request": 2}


def test_codex_and_cursor_ignored_types_name_their_record_kind(cli, tmp_path):
    cx = write_jsonl(tmp_path / "rollout-x.jsonl", [
        cx_meta("i", cli.repo), cx_event("task_started", 1), cx_event("task_started", 2),
        cx_user("go", 3), cx_tokens(1, 0, 1, 4)])
    _, out = cli.json(cx, "--host", "codex")
    assert out["coverage"]["ignored_types"] == {"session_meta": 1, "event_msg/task_started": 2}
    cu = write_jsonl(tmp_path / "c.jsonl", [cu_user("go"), cu_turn_ended(), cu_turn_ended()])
    _, out = cli.json(cu, "--host", "cursor")
    assert out["coverage"]["ignored_types"] == {"turn_ended": 2}


def test_tested_versions_and_observed_version_per_host(cli, tmp_path):
    cc_rec = cc_assistant("m", 5, usage=usage(1, 1))
    cc_rec["version"] = "2.1.309"
    cc = write_jsonl(tmp_path / "a.jsonl", [cc_user("go", 0), cc_rec])
    _, out = cli.json(cc, "--host", "claude-code")
    assert out["coverage"]["tested_versions"] == "2.1.295 to 2.1.309"
    assert out["coverage"]["transcript_version"] == "2.1.309"
    cx = write_jsonl(tmp_path / "rollout-x.jsonl", [cx_meta("i", cli.repo, cli_version="0.142.2"), cx_user("go", 1)])
    _, out = cli.json(cx, "--host", "codex")
    assert out["coverage"]["tested_versions"] == "0.137.0-alpha.4 to 0.142.2 (cli_version)"
    assert out["coverage"]["transcript_version"] == "0.142.2"
    cu = write_jsonl(tmp_path / "c.jsonl", [cu_user("go")])
    _, out = cli.json(cu, "--host", "cursor")
    assert out["coverage"]["tested_versions"] == "unknown (no version marker in transcripts)"
    assert out["coverage"]["transcript_version"] is None
