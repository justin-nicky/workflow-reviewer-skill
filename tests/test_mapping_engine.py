"""Mapping-engine behaviour driven through the CLI with a mapping on stdin."""
from fixtures.builders import at, cc_assistant, cc_user, usage, write_jsonl

MOVED = """
host = "claude-code"
tested_versions = "drafted"
type_path = "type"
[capabilities]
has_tokens = true
has_timestamps = true
has_tool_results = false
has_ids = true
[fields]
timestamp = ["timestamp", "meta.at"]
msg_id = ["message.id", "id"]
[[records]]
match = { type = "user" }
role = "user"
text = ["message.content"]
[[records]]
match = { type = "assistant" }
role = "assistant"
usage_input = ["message.usage.input_tokens", "usage.in"]
usage_output = ["message.usage.output_tokens", "usage.out"]
calls = ["message.content[type=tool_use]", "tool_calls[]"]
call_name = ["name", "fn"]
call_input = ["input", "args"]
call_id = ["id", "call_id"]
"""


def test_a_field_that_moved_between_two_shapes_is_read_from_either(cli, tmp_path):
    old_shape = cc_assistant("m1", 10, usage=usage(1, 10))
    new_shape = {"type": "assistant", "id": "m2", "meta": {"at": at(30)}, "usage": {"in": 2, "out": 20},
                 "tool_calls": [{"fn": "Read", "args": {"file_path": "a.py"}, "call_id": "c1"}]}
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), old_shape, new_shape])
    code, out = cli.json(path, "--host", "claude-code", "--mapping", "-", stdin=MOVED)
    assert code == 0
    assert out["tokens"]["output"] == 30 and out["tokens"]["fresh_input"] == 3
    assert out["tokens"]["api_calls"] == 2                      # the id moved too
    assert out["time"]["wall_s"] == 30                          # the timestamp moved too
    assert out["tools"]["by_category"] == {"read": 1}           # and the tool call list


def test_the_first_alternative_that_is_present_wins(cli, tmp_path):
    both = cc_assistant("m1", 10, usage=usage(1, 10))
    both["usage"] = {"in": 99, "out": 99}
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), both])
    _, out = cli.json(path, "--host", "claude-code", "--mapping", "-", stdin=MOVED)
    assert out["tokens"]["output"] == 10
