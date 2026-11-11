"""A mapping file may hold several variants; the transcript's shape picks one."""
import json

from fixtures.builders import write_jsonl

HEAD = """
host = "claude-code"
tested_versions = "drafted"
type_path = "type"
[capabilities]
has_tokens = true
has_timestamps = false
has_tool_results = false
has_ids = false
[fields]
version = "version"
"""

USER = """
[[records]]
match = { type = "user" }
role = "user"
text = ["message.content"]
"""


def variant(name, assistant_type="assistant", usage_path="message.usage.output_tokens", extra="", head=""):
    return f"""
[[variants]]
name = "{name}"
{head}
[[variants.records]]
match = {{ type = "user" }}
role = "user"
text = ["message.content"]
[[variants.records]]
match = {{ type = "{assistant_type}" }}
role = "assistant"
usage_output = "{usage_path}"
{extra}
"""


def two_shapes():
    return HEAD + variant("old") + variant("new", "assistant_v2", "usage.out")


def user(i=0, version=None):
    rec = {"type": "user", "message": {"content": f"go {i}"}}
    if version:
        rec["version"] = version
    return rec


def old_assistant(out):
    return {"type": "assistant", "message": {"usage": {"output_tokens": out}}}


def new_assistant(out):
    return {"type": "assistant_v2", "usage": {"out": out}}


def run(cli, tmp_path, mapping, records):
    path = write_jsonl(tmp_path / "s.jsonl", records)
    return cli.json(path, "--host", "claude-code", "--mapping", "-", stdin=mapping)


def test_the_variant_whose_shape_fits_best_is_chosen_and_reported(cli, tmp_path):
    code, out = run(cli, tmp_path, two_shapes(), [user(), new_assistant(7), new_assistant(8)])
    assert out["coverage"]["variant"] == "new"
    assert out["tokens"]["output"] == 15
    assert out["coverage"]["variant_choice"]["reason"] == "best classified ratio"
    assert out["coverage"]["variant_choice"]["scores"] == {"old": 33.3, "new": 100.0}
    code, out = run(cli, tmp_path, two_shapes(), [user(), old_assistant(5)])
    assert out["coverage"]["variant"] == "old" and out["tokens"]["output"] == 5
    assert code == 0


def test_required_markers_decide_between_equal_shapes(cli, tmp_path):
    ignore = lambda t: f'[[variants.records]]\nmatch = {{ type = "{t}" }}\nrole = "ignore"\n'
    mapping = (HEAD
               + variant("a", extra=ignore("marker_a") + ignore("marker_b"),
                         head='requires = { types = ["marker_a"] }')
               + variant("b", extra=ignore("marker_a") + ignore("marker_b"),
                         head='requires = { types = ["marker_b"], fields = ["extra.flag"] }'))
    for records, want in (
        ([user(), {"type": "marker_a"}, old_assistant(1)], "a"),
        ([user(), {"type": "marker_b", "extra": {"flag": 1}}, old_assistant(1)], "b"),
        ([user(), {"type": "marker_b"}, old_assistant(1)], "a"),       # b misses its field; a misses its type: all fail
    ):
        _, out = run(cli, tmp_path, mapping, records)
        assert out["coverage"]["variant"] == want, records


def test_version_range_only_breaks_ties(cli, tmp_path):
    mapping = (HEAD + variant("early", head='versions = "1.0.0 to 1.9.9"')
               + variant("late", head='versions = "2.0.0 to 2.9.9"'))
    for version, want in (("2.3.1", "late"), ("1.5.0", "early"), ("9.9.9", "early"), (None, "early")):
        _, out = run(cli, tmp_path, mapping, [user(version=version), old_assistant(1)])
        assert out["coverage"]["variant"] == want, version
    _, out = run(cli, tmp_path, mapping, [user(version="2.3.1"), old_assistant(1)])
    assert out["coverage"]["variant_choice"]["reason"] == "version range tiebreak"
    _, out = run(cli, tmp_path, mapping, [user(), old_assistant(1)])
    assert out["coverage"]["variant_choice"]["reason"] == "listed first"


def test_a_better_shape_beats_a_matching_version(cli, tmp_path):
    mapping = (HEAD + variant("old", head='versions = "1.0.0 to 1.9.9"')
               + variant("new", "assistant_v2", "usage.out", head='versions = "2.0.0 to 2.9.9"'))
    _, out = run(cli, tmp_path, mapping, [user(version="1.5.0"), new_assistant(1), new_assistant(2)])
    assert out["coverage"]["variant"] == "new"


def test_single_variant_mappings_keep_working_unchanged(cli, tmp_path):
    single = HEAD + USER + """
[[records]]
match = { type = "assistant" }
role = "assistant"
usage_output = "message.usage.output_tokens"
"""
    code, out = run(cli, tmp_path, single, [user(), old_assistant(4)])
    assert code == 0 and out["tokens"]["output"] == 4
    assert out["coverage"]["variant"] is None
    assert "variant_choice" not in out["coverage"]


def test_builtin_mappings_still_load_and_name_their_variant(cli, tmp_path):
    from fixtures.builders import cc_assistant, cc_user, usage
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), cc_assistant("m", 1, usage=usage(1, 1))])
    code, out = cli.json(path, "--host", "claude-code")
    assert code == 0 and out["coverage"]["variant"] == "default"
