"""Claude Code 2.1.331 writes data-flow-records: lists of entries that point at tool calls and responses already in the
transcript. They carry no metric, so they are ignored, and two warn-level checks make sure what they point at exists."""
import json

from test_golden import GOLDEN

G2331 = GOLDEN / "claude-code-2.1.331"
TOOL_CHECK = "tool calls: data-flow tool_use_ids vs tool calls"
RESPONSE_CHECK = "tokens: data-flow response_ids vs assistant messages"     # an info note now, never a check
SIDE_CALLS = ("{n} model responses are referenced but not in the transcript (likely the host's side calls); "
              "token totals cover the transcript's calls only.")
NOTE = "4 data-flow entries name a tool with no matching call (3 from plugins)"


def session(folder=G2331):
    return next(folder.glob("*.jsonl")).with_suffix("")


def checks(out):
    return {c["name"]: c for r in out["coverage"]["health"]["rows"] for c in r["checks"]}


def rewrite(folder, change):
    main = next(folder.glob("*.jsonl"))
    rs = [json.loads(line) for line in main.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in rs:
        if r.get("type") == "data-flow-records":
            change(r)
    with open(main, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rs))


def copy(tmp_path):
    import shutil
    shutil.copytree(G2331, tmp_path / "g")
    return tmp_path / "g"


def test_the_2331_golden_is_fully_recognised_with_no_warning(cli):
    code, out = cli.json(session(), "--host", "claude-code", "--health-all")
    cov = out["coverage"]
    assert code == 0 and cov["transcript_version"] == "2.1.331"
    assert cov["drift"]["coverage_pct"] == 100.0 and cov["drift"]["unseen_types"] == {}
    assert cov["ignored_types"]["data-flow-records"] == 2
    assert not [r["metric"] for r in cov["health"]["rows"] if r["status"] in ("warn", "broken")]
    c = checks(out)
    assert c[TOOL_CHECK]["passed"] and (c[TOOL_CHECK]["first"], c[TOOL_CHECK]["second"]) == (2, 2)
    assert RESPONSE_CHECK not in c
    assert not any("model responses" in n for n in cov.get("notes", []))       # every response it names is in the transcript


def test_the_numbers_match_the_2321_golden(cli):
    _, old = cli.json(session(GOLDEN / "claude-code"), "--host", "claude-code")
    _, new = cli.json(session(), "--host", "claude-code")
    for key in ("tokens", "tools", "verify", "interruptions", "time"):
        assert new[key] == old[key], key
    assert new["window"]["turns_total"] == old["window"]["turns_total"]


def test_tool_names_with_no_matching_call_are_an_info_note(cli):
    code, out = cli.json(session(), "--host", "claude-code")
    assert code == 0 and NOTE in out["coverage"]["notes"]
    assert "DemoTool" not in json.dumps(out["coverage"]) and "plugin-demo" not in json.dumps(out["coverage"])
    md = cli.run(session(), "--host", "claude-code", fmt="md").stdout
    assert NOTE in md


def test_a_data_flow_tool_use_id_with_no_call_is_a_warning(cli, tmp_path):
    folder = copy(tmp_path)

    changed = []

    def change(r):                        # one of the two ids points nowhere
        for e in r["records"]:
            if "tool_use_id" in e and not changed:
                e["tool_use_id"] = "toolu_nowhere"
                changed.append(1)
    rewrite(folder, change)
    code, out = cli.json(session(folder), "--host", "claude-code", "--health-all")
    c = checks(out)[TOOL_CHECK]
    assert code == 0 and not c["passed"] and (c["first"], c["second"]) == (1, 2)
    assert next(r for r in out["coverage"]["health"]["rows"] if r["metric"] == "tool_calls")["status"] == "warn"


def test_a_response_id_with_no_message_is_an_info_note_not_a_warning(cli, tmp_path):
    folder = copy(tmp_path)

    def change(r):
        for e in r["records"]:
            if "response_id" in e:
                e["response_id"] = "msg_side_call"
    rewrite(folder, change)
    code, out = cli.json(session(folder), "--host", "claude-code", "--health-all")
    _, clean = cli.json(session(), "--host", "claude-code", "--health-all")
    assert code == 0 and SIDE_CALLS.format(n=1) in out["coverage"]["notes"]
    assert RESPONSE_CHECK not in checks(out)
    assert out["coverage"]["health"] == clean["coverage"]["health"]           # no warning, nothing else moves
    assert out["tokens"] == clean["tokens"] and "lower_bound" not in out["tokens"]


def test_a_session_without_data_flow_records_is_unchanged(cli):
    _, out = cli.json(session(GOLDEN / "claude-code"), "--host", "claude-code", "--health-all")
    c = checks(out)
    assert TOOL_CHECK not in c and RESPONSE_CHECK not in c       # skipped: nothing to compare
    assert "notes" not in out["coverage"] or not any("data-flow" in n for n in out["coverage"]["notes"])
