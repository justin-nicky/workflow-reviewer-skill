"""Claude Code subagents attach to the Agent call that spawned them, not to whatever turn is running."""
import json

from fixtures.builders import cc_assistant, cc_tool_result, cc_user, tool_use, usage, write_jsonl

CALL = "toolu_spawn01"


def main_records(agent_id="abc", result_extra=True):
    result = cc_tool_result(CALL, "Async agent launched", 6, uuid="r-spawn")
    if result_extra:
        result["toolUseResult"] = {"isAsync": True, "status": "async_launched", "agentId": agent_id}
    return [
        cc_user("first task", 0, uuid="u1"),
        cc_assistant("m1", 5, [tool_use("Agent", {"prompt": "do the checks", "description": "d"}, CALL)],
                     usage(1, 10), uuid="a1"),
        result,
        cc_assistant("m2", 8, usage=usage(1, 1), uuid="a2"),
        cc_user("second task", 20, uuid="u2"),                      # the subagent finishes during this turn
        cc_assistant("m3", 21, usage=usage(1, 1), uuid="a3"),
    ]


def session(tmp_path, meta=None, agent_id="abc", result_extra=True):
    main = write_jsonl(tmp_path / "proj" / "s1.jsonl", main_records(agent_id, result_extra))
    sub_dir = tmp_path / "proj" / "s1" / "subagents"
    write_jsonl(sub_dir / f"agent-{agent_id}.jsonl", [
        cc_user("do the checks", 25, uuid="su"),
        cc_assistant("sm", 26, usage=usage(1, 500), model="claude-from-records", uuid="sa")])
    if meta is not None:
        (sub_dir / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
    return main


def run(cli, main):
    _, out = cli.json(main, "--host", "claude-code")
    sub = next(a for a in out["actors"] if a["actor"].startswith("subagent:"))
    turn_out = {t["turn"]: t["tokens"]["output"] for t in out["turns"]}
    return out, sub, turn_out


def test_meta_json_links_the_subagent_to_its_spawning_call_and_gives_its_model(cli, tmp_path):
    main = session(tmp_path, meta={"toolUseId": CALL, "pickedEffortModel": "claude-from-meta"},
                   result_extra=False)
    out, sub, turn_out = run(cli, main)
    assert sub["link"] == "tool_use_id"
    assert sub["models"] == ["claude-from-meta"]
    assert turn_out == {1: 10 + 1 + 500, 2: 1}        # its tokens belong to turn 1, where it was spawned


def test_the_result_agent_id_links_when_there_is_no_meta_file(cli, tmp_path):
    main = session(tmp_path, meta=None)
    out, sub, turn_out = run(cli, main)
    assert sub["link"] == "tool_use_id"
    assert sub["models"] == ["claude-from-records"]   # no meta.json: fall back to the records' own model
    assert turn_out[1] == 511


def test_without_any_exact_link_the_timestamp_decides_and_is_reported(cli, tmp_path):
    main = session(tmp_path, meta=None, result_extra=False)
    out, sub, turn_out = run(cli, main)
    assert sub["link"] == "timestamp"
    assert turn_out == {1: 11, 2: 501}                # it ran during turn 2


def test_a_meta_file_pointing_at_an_unknown_call_falls_back_to_the_timestamp(cli, tmp_path):
    main = session(tmp_path, meta={"toolUseId": "toolu_nope", "pickedEffortModel": "m"}, result_extra=False)
    out, sub, turn_out = run(cli, main)
    assert sub["link"] == "timestamp"
    assert sub["models"] == ["m"]                     # the model still comes from meta.json
