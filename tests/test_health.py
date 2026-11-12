"""coverage.health: a status per metric from structure checks and second counts; only a broken core metric pauses (exit 2)."""
import json

import pytest
from fixtures.builders import write_jsonl

MAPPING = '''
host = "hx"
schema_version = 1
type_path = "t"
tested_versions = "1.0.0 to 2.0.0"
[fields]
version = "v"
[capabilities]
has_tokens = true
has_timestamps = false
has_tool_results = true
has_ids = true
[[records]]
match = { t = "u" }
role = "user"
text = ["text"]
[[records]]
match = { t = "a" }
role = "assistant"
text = ["text"]
msg_id = "mid"
calls = "calls[]"
call_name = "name"
call_input = "in"
call_id = "id"
usage_input = "usage.i"
usage_output = "usage.o"
[[records]]
match = { t = "r" }
role = "meta"
results = "."
result_id = "rid"
[[records]]
match = { t = "m" }
role = "ignore"
'''
TURN_CHECK = '''
[[checks]]
name = "turns vs markers"
metric = "turns"
first = "turns"
second = { match = { t = "m" }, count = true }
'''
TOKEN_CHECK = '''
[[checks]]
name = "output vs reported"
metric = "tokens"
first = "tokens_output"
second = { match = { t = "m" }, sum = "reported" }
tolerance_pct = 2
'''


def user(text="go", v="1.5.0"):
    return {"t": "u", "text": text, "v": v}


def assistant(mid, o=10, calls=(), text="ok"):
    return {"t": "a", "mid": mid, "text": text, "usage": {"i": 5, "o": o},
            "calls": [{"name": "Bash", "in": {"command": "ls"}, "id": c} for c in calls]}


def result(rid):
    return {"t": "r", "rid": rid}


def marker(reported=None):
    return {"t": "m", **({"reported": reported} if reported is not None else {})}


def run(cli, tmp_path, records, extra_mapping="", *args):
    path = write_jsonl(tmp_path / "s.jsonl", records)
    mapping = tmp_path / "m.toml"
    mapping.write_text(MAPPING.replace("[fields]", f"{extra_mapping}\n[fields]", 1) if False else MAPPING + extra_mapping)
    proc = cli.run(path, "--mapping", mapping, *args)
    return proc.returncode, json.loads(proc.stdout)


def rows(out):
    return {r["metric"]: r for r in out["coverage"]["health"]["rows"]}


N = 12                    # turns in a normal synthetic session; enough records that one odd record stays above 90% coverage


def session(v="1.5.0"):
    records = []
    for i in range(N):
        records += [user(f"prompt {i}", v), assistant(f"m{i}", 10, [f"c{i}"]), result(f"c{i}")]
    return records


# ---- statuses -------------------------------------------------------------------------------------------------------------

def test_a_metric_with_no_second_count_is_one_way_and_that_is_not_a_failure(cli, tmp_path):
    code, out = run(cli, tmp_path, session(), "", "--health-all")
    r = rows(out)
    assert code == 0 and out["coverage"]["drift"]["ok"]
    assert (r["turns"]["status"], r["tool_calls"]["status"], r["tokens"]["status"]) == ("one_way",) * 3
    assert r["time"]["status"] == "not_measurable" and r["interruptions"]["status"] == "not_measurable"


def test_a_passing_second_count_makes_the_row_ok_and_shows_both_values(cli, tmp_path):
    code, out = run(cli, tmp_path, session() + [marker()] * N, TURN_CHECK, "--health-all")
    check = next(c for c in rows(out)["turns"]["checks"] if c["layer"] == "second count")
    assert code == 0 and rows(out)["turns"]["status"] == "ok"
    assert (check["first"], check["second"], check["relation"], check["passed"]) == (N, N, "equal", True)


def test_counts_must_match_exactly(cli, tmp_path):
    code, out = run(cli, tmp_path, session() + [marker()] * N, TURN_CHECK, "--health-all")
    assert rows(out)["turns"]["status"] == "ok" and code == 0
    code, out = run(cli, tmp_path, session() + [marker()] * (N + 1), TURN_CHECK, "--health-all")
    assert rows(out)["turns"]["status"] == "broken" and code == 2


def test_tokens_may_differ_by_two_percent(cli, tmp_path):
    base = [user(), assistant("m1", 100)]
    ok_code, ok = run(cli, tmp_path, base + [marker(102)], TOKEN_CHECK, "--health-all")
    assert rows(ok)["tokens"]["status"] == "ok" and ok_code == 0
    bad_code, bad = run(cli, tmp_path, base + [marker(110)], TOKEN_CHECK, "--health-all")
    assert rows(bad)["tokens"]["status"] == "broken" and bad_code == 2


def test_a_live_run_may_have_one_turn_still_open(cli, tmp_path):
    check = TURN_CHECK + "slack_first = 1\n"
    assert run(cli, tmp_path, session() + [marker()] * (N - 1), check)[0] == 0
    assert run(cli, tmp_path, session() + [marker()] * (N - 2), check)[0] == 2        # two open turns is not "live"


def test_a_check_is_skipped_when_the_second_record_does_not_exist_if_the_mapping_says_so(cli, tmp_path):
    check = TURN_CHECK + "skip_if_second_zero = true\n"
    code, out = run(cli, tmp_path, session(), check, "--health-all")
    assert code == 0 and rows(out)["turns"]["status"] == "one_way"


def test_relations_at_most_and_at_least(cli, tmp_path):
    for relation, first_ok in (("at_most", 3), ("at_least", 1)):
        check = TURN_CHECK.replace("[[checks]]", "[[checks]]\nrelation = \"%s\"" % relation)
        assert run(cli, tmp_path, session() + [marker()] * (N + first_ok - 2 if relation == "at_most" else N - 1), check)[0] == 0


def test_a_non_core_metric_that_fails_warns_and_the_run_continues(cli, tmp_path):
    check = '''
[[checks]]
name = "results vs markers"
metric = "errors"
first = "results"
second = { match = { t = "m" }, count = true }
'''
    code, out = run(cli, tmp_path, session() + [marker()] * 2, check, "--health-all")
    assert code == 0 and out["coverage"]["drift"]["ok"] and rows(out)["errors"]["status"] == "warn"


def test_a_check_can_be_declared_warn_only(cli, tmp_path):
    code, out = run(cli, tmp_path, session() + [marker()] * 5, TURN_CHECK + 'severity = "warn"\n', "--health-all")
    assert code == 0 and rows(out)["turns"]["status"] == "warn"


# ---- structure checks point at the metrics they affect -------------------------------------------------------------------------

def noisy(n_unknown):
    return session() + [{"t": "zz"}] * n_unknown


def test_low_coverage_with_no_second_count_for_a_core_metric_still_pauses(cli, tmp_path):
    code, out = run(cli, tmp_path, noisy(40), "", "--health-all")
    assert code == 2 and rows(out)["turns"]["status"] == "broken" and not out["coverage"]["drift"]["ok"]
    assert out["coverage"]["drift"]["warning"]


def test_low_coverage_alone_is_only_a_warn_when_the_core_second_counts_still_pass(cli, tmp_path):
    checks = TURN_CHECK + TOKEN_CHECK + '''
[[checks]]
name = "calls vs results"
metric = "tool_calls"
first = "tool_calls"
second = "results"
'''
    records = noisy(40) + [marker(N * 10)] + [marker()] * (N - 1)
    code, out = run(cli, tmp_path, records, checks, "--health-all")
    r = rows(out)
    assert code == 0 and out["coverage"]["drift"]["ok"]
    assert (r["turns"]["status"], r["tool_calls"]["status"], r["tokens"]["status"]) == ("warn", "warn", "warn")
    assert any(c["name"] == "coverage" and not c["passed"] for c in r["turns"]["checks"])


def test_a_missing_usage_field_breaks_tokens_and_a_missing_tool_name_breaks_tool_calls(cli, tmp_path):
    no_usage = [user(), {"t": "a", "mid": "m", "text": "x", "calls": []}]
    code, out = run(cli, tmp_path, no_usage, "", "--health-all")
    assert code == 2 and rows(out)["tokens"]["status"] == "broken" and rows(out)["turns"]["status"] != "broken"
    no_name = [user(), {"t": "a", "mid": "m", "usage": {"i": 1, "o": 1}, "calls": [{"in": {}, "id": "c1"}]}]
    code, out = run(cli, tmp_path, no_name, "", "--health-all")
    assert code == 2 and rows(out)["tool_calls"]["status"] == "broken" and rows(out)["tokens"]["status"] != "broken"


def test_an_unseen_type_points_at_the_metric_its_record_would_feed(cli, tmp_path):
    for kind, metric in (("tool_result_v2", "tool_calls"), ("usage_report", "tokens"), ("skill_event", "skills")):
        code, out = run(cli, tmp_path, session() + [{"t": kind}], "", "--health-all")
        r = rows(out)
        assert r[metric]["status"] == "warn" and code == 0
        assert [m for m, row in r.items() if row["status"] == "warn"] == [metric]


def test_an_unseen_type_that_matches_no_metric_is_listed_not_attributed(cli, tmp_path):
    code, out = run(cli, tmp_path, session() + [{"t": "zzz_novel"}], "", "--health-all")
    health = out["coverage"]["health"]
    assert code == 0 and health["unattributed_types"] == ["zzz_novel"] and not any(r["status"] in ("warn", "broken") for r in health["rows"])


def test_a_mapping_can_override_the_hints(cli, tmp_path):
    code, out = run(cli, tmp_path, session() + [{"t": "zzz_novel"}], '\n[health_hints]\nsubagents = ["novel"]\n', "--health-all")
    assert rows(out)["subagents"]["status"] == "warn"


# ---- output -------------------------------------------------------------------------------------------------------------------

def test_only_rows_needing_attention_are_shown_unless_health_all(cli, tmp_path):
    records = session() + [{"t": "tool_result_v2"}]
    _, short = run(cli, tmp_path, records)
    health = short["coverage"]["health"]
    assert [r["metric"] for r in health["rows"]] == ["tool_calls"] and health["summary"]["warn"] == 1 and health["summary"]["one_way"] == 6
    assert all(not c["passed"] for r in health["rows"] for c in r["checks"])
    _, full = run(cli, tmp_path, records, "", "--health-all")
    assert len(full["coverage"]["health"]["rows"]) == 9


def test_a_clean_session_shows_an_empty_table_and_the_version_context(cli, tmp_path):
    _, out = run(cli, tmp_path, session())
    health = out["coverage"]["health"]
    assert health["rows"] == [] and health["version"] == {"transcript": "1.5.0", "tested": "1.0.0 to 2.0.0", "position": "within"}


@pytest.mark.parametrize("seen, position", [("0.9.0", "older"), ("2.5.0", "newer"), ("1.5.0", "within"), (None, "unknown")])
def test_version_position(cli, tmp_path, seen, position):
    _, out = run(cli, tmp_path, session(seen))
    assert out["coverage"]["health"]["version"]["position"] == position


def test_markdown_shows_the_table_compactly(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", session() + [{"t": "tool_result_v2"}])
    (tmp_path / "m.toml").write_text(MAPPING)
    proc = cli.run(path, "--mapping", tmp_path / "m.toml", fmt="md")
    assert "tool_calls" in proc.stdout and "warn" in proc.stdout and proc.returncode == 0


def test_the_report_coverage_row_names_the_affected_metrics(cli, tmp_path):
    import copy
    from test_report import FINDINGS
    path = write_jsonl(tmp_path / "s.jsonl", session() + [{"t": "tool_result_v2"}])
    (tmp_path / "m.toml").write_text(MAPPING)
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir()
    proc = cli.run(path, "--mapping", tmp_path / "m.toml", "--report", "-", "--no-open", "--report-format", "md",
                   stdin=json.dumps(copy.deepcopy(FINDINGS)).replace("claude-code", "all hosts") if False else json.dumps(_findings()))
    assert "Coverage: tool_calls: warn" in proc.stdout


def _findings():
    from test_report import FINDINGS
    data = json.loads(json.dumps(FINDINGS))
    for a in data["actions"]:
        a.pop("applies_to", None)
    return data


# ---- assistant activity with no turn: the mapping decides how serious it is ---------------------------------------------------

def test_assistant_activity_with_no_turn_warns_unless_the_mapping_says_it_is_broken(cli, tmp_path):
    records = [assistant("m1", 3, ["c1"]), result("c1")]
    code, out = run(cli, tmp_path, records, "", "--health-all")
    assert code == 0 and rows(out)["turns"]["status"] == "warn"
    path = write_jsonl(tmp_path / "s2.jsonl", records)
    (tmp_path / "m2.toml").write_text(MAPPING.replace('type_path = "t"', 'type_path = "t"\nturnless_activity = "broken"'))
    proc = cli.run(path, "--mapping", tmp_path / "m2.toml", "--health-all")
    out = json.loads(proc.stdout)
    assert proc.returncode == 2 and rows(out)["turns"]["status"] == "broken" and not out["coverage"]["drift"]["ok"]


def test_a_subagent_rollout_passed_alone_only_warns_even_where_the_mapping_breaks_on_it(cli):
    from pathlib import Path
    golden = Path(__file__).resolve().parent / "fixtures" / "golden" / "codex-desktop"
    sub = next(f for f in sorted(golden.glob("rollout-*.jsonl")) if '"subagent"' in f.read_text(encoding="utf-8").split("\n")[0])
    code, out = cli.json(sub, "--host", "codex", "--health-all")
    turns = rows(out)["turns"]
    assert code == 0 and out["coverage"]["drift"]["ok"]
    assert turns["status"] == "warn" and "subagent" in turns["checks"][0]["name"]


def test_claude_code_and_codex_break_on_turnless_main_activity():
    import tomllib
    from conftest import ROOT
    for name in ("claude_code", "codex"):
        raw = tomllib.loads((ROOT / "skills/workflow-reviewer/scripts/mappings" / f"{name}.toml").read_text())
        assert raw["turnless_activity"] == "broken", name


# ---- a duplicated call is counted once ------------------------------------------------------------------------------------------

DUP_CHECK = """
[[checks]]
name = "no call id counted twice"
metric = "tool_calls"
first = "duplicate_calls"
second = { const = 0 }
severity = "warn"
"""


def test_a_call_id_repeated_in_another_record_is_counted_once_and_warned_about(cli, tmp_path):
    records = session() + [assistant("dup", 10, ["c0"])]                        # c0 is already used by the first assistant record
    code, out = run(cli, tmp_path, records, DUP_CHECK, "--health-all")
    assert code == 0 and out["tools"]["total"] == N
    check = next(c for c in rows(out)["tool_calls"]["checks"] if c["name"] == "no call id counted twice")
    assert rows(out)["tool_calls"]["status"] == "warn" and (check["first"], check["second"]) == (1, 0)


def test_no_duplicate_means_no_warning(cli, tmp_path):
    code, out = run(cli, tmp_path, session(), DUP_CHECK, "--health-all")
    assert code == 0 and rows(out)["tool_calls"]["status"] == "ok"


def test_several_commands_of_one_call_are_not_duplicates():
    import importlib.util
    from conftest import SCRIPT
    spec = importlib.util.spec_from_file_location("tm_dedupe", SCRIPT)
    tm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tm)
    call = lambda cid, cmd: {"id": cid, "name": "exec", "input": {"cmd": cmd}}
    events = [{"calls": [call("k1", "ls"), call("k1", "pwd"), call("k2", "ls")]}, {"calls": [call("k1", "ls"), call(None, "ls"), call(None, "ls")]}]
    assert tm.dedupe_calls(events) == 1                                   # only the repeat of k1/ls
    assert [len(e["calls"]) for e in events] == [3, 2]
