"""The audit runs inside the session it audits, so the script's output must stay small."""
from fixtures.builders import (bash, cc_assistant, cc_tool_result, cc_user, cu_assistant, cu_user,
                               tool_use, usage, write_jsonl)

MD_LIMIT = 8000
JSON_LIMIT = 12000


def big_session(tmp_path, turns=361, files=60, subagents=0):
    records, t = [], 0
    for i in range(turns):
        records.append(cc_user(f"task step {i}: " + "please look into the thing " * 6, t, uuid=f"u{i}"))
        calls = [tool_use("Read", {"file_path": f"src/module_{i % files}.py"}, f"r{i}"),
                 tool_use("Edit", {"file_path": f"src/module_{(i * 7) % files}.py"}, f"e{i}"),
                 bash("pytest -x tests/test_everything.py", f"b{i}")]
        records.append(cc_assistant(f"m{i}", t + 5, calls, usage(10 + i, 100 + i * 3, 5000 + i, 20), uuid=f"a{i}"))
        for c in ("r", "e", "b"):
            records.append(cc_tool_result(f"{c}{i}", "x" * (200 + i), t + 6, is_error=(c == "b" and i % 2 == 0),
                                          uuid=f"res-{c}{i}"))
        t += 60
    path = write_jsonl(tmp_path / "big.jsonl", records)
    for n in range(subagents):
        write_jsonl(tmp_path / "big" / "subagents" / f"agent-{n}.jsonl", [
            cc_user(f"sub brief {n}", 10 + n, uuid=f"su{n}"),
            cc_assistant(f"sm{n}", 20 + n, usage=usage(1, 50 * (n + 1)), uuid=f"sa{n}")])
    return path


def test_summaries_of_a_361_turn_session_stay_under_the_size_budget(cli, tmp_path):
    path = big_session(tmp_path, subagents=9)
    for i in range(40):                                   # a large skill inventory
        d = cli.home / ".claude" / "skills" / f"skill-{i}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: skill-{i}\ndescription: {'d' * (10 + i)}\n---\nbody\n")
    md = cli.run(path, "--host", "claude-code", fmt="md").stdout
    js = cli.run(path, "--host", "claude-code", fmt="json").stdout
    assert len(md) < MD_LIMIT, len(md)
    assert len(js) < JSON_LIMIT, len(js)


def test_per_turn_rows_are_top_five_plus_one_aggregate(cli, tmp_path):
    _, out = cli.json(big_session(tmp_path), "--host", "claude-code")
    rows = out["turns"]
    assert len(rows) == 6
    assert rows[-1]["turn"] == "other" and rows[-1]["turns"] == 356
    assert [r["turn"] for r in rows[:5]] == sorted(r["turn"] for r in rows[:5])
    assert min(r["tokens"]["output"] for r in rows[:5]) >= 100 + 356 * 3          # the five most expensive
    assert sum(r["tokens"]["output"] for r in rows) == out["tokens"]["output"]
    assert out["window"]["turns_total"] == 361


def test_few_turns_are_all_listed_without_an_aggregate(cli, tmp_path):
    _, out = cli.json(big_session(tmp_path, turns=4), "--host", "claude-code")
    assert [r["turn"] for r in out["turns"]] == [1, 2, 3, 4]


def test_per_actor_rows_are_capped_too(cli, tmp_path):
    _, out = cli.json(big_session(tmp_path, turns=3, subagents=9), "--host", "claude-code")
    actors = out["actors"]
    assert len(actors) == 6
    assert actors[-1]["actor"] == "other" and actors[-1]["actors"] == 5
    assert "main" in [a["actor"] for a in actors]


def test_hosts_without_tokens_rank_turns_by_tool_calls(cli, tmp_path):
    records = []
    for i in range(12):
        records += [cu_user(f"step {i}"), cu_assistant(calls=[("Read", {"path": f"f{i}.py"})] * (i + 1))]
    _, out = cli.json(write_jsonl(tmp_path / "c.jsonl", records), "--host", "cursor")
    shown = [r["turn"] for r in out["turns"][:5]]
    assert shown == [8, 9, 10, 11, 12]
    assert out["turns"][-1]["turn"] == "other" and out["turns"][-1]["turns"] == 7


def test_list_turns_shows_the_last_30_with_a_header_and_all_on_request(cli, tmp_path):
    path = big_session(tmp_path)
    _, out = cli.json(path, "--host", "claude-code", "--list-turns")
    assert out["header"] == "showing 30 of 361 turns (use --all for every turn)"
    assert [t["index"] for t in out["turns"]] == list(range(332, 362))
    _, full = cli.json(path, "--host", "claude-code", "--list-turns", "--all")
    assert len(full["turns"]) == 361 and full["header"] == "showing all 361 turns"
    md = cli.run(path, "--host", "claude-code", "--list-turns", fmt="md").stdout
    assert "showing 30 of 361 turns" in md
    assert len(md) < 8000


def test_other_lists_are_capped_with_a_remainder(cli, tmp_path):
    _, out = cli.json(big_session(tmp_path, turns=120, files=100), "--host", "claude-code")
    tools = out["tools"]
    assert len(tools["repeated_reads"]) <= 10 and len(tools["edits_per_file"]) <= 10
    assert tools["edits_per_file_more"]["files"] == 100 - len(tools["edits_per_file"])
    assert len(tools["retried_failures"]) <= 5
    assert len(out["skills"]["inventory"]) <= 10
    assert len(out["skills"]["loaded"]) <= 15


def test_capped_inventory_reports_the_total(cli, tmp_path):
    for i in range(25):
        d = cli.home / ".claude" / "skills" / f"s{i}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: s{i}\ndescription: {'x' * i}\n---\n")
    _, out = cli.json(big_session(tmp_path, turns=2), "--host", "claude-code")
    assert len(out["skills"]["inventory"]) == 10
    assert out["skills"]["inventory_total"] == 25
    assert out["skills"]["inventory"][0]["description_chars"] == 24      # largest descriptions first
