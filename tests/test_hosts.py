import json
import re

from fixtures.builders import (cc_assistant, cc_user, cu_assistant, cu_user, cx_call, cx_meta,
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
    assert "2.1.x" in d["warning"]
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
