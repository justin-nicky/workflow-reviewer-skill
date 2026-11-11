"""coverage.variant is reported for every host; a mapping with one shape is the "default" variant."""
from fixtures.builders import cc_assistant, cc_user, cu_assistant, cu_user, usage, write_jsonl

FLAT = """
host = "x"
type_path = "kind"
[capabilities]
has_tokens = false
[[records]]
match = { kind = "u" }
role = "user"
text = ["t"]
"""


def test_cursor_reports_the_default_variant(cli, tmp_path):
    path = write_jsonl(tmp_path / "c.jsonl", [cu_user("go"), cu_assistant("ok")])
    _, out = cli.json(path, "--host", "cursor")
    assert out["coverage"]["variant"] == "default"


def test_claude_code_reports_the_default_variant(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [cc_user("go", 0), cc_assistant("m", 1, usage=usage(1, 1))])
    _, out = cli.json(path, "--host", "claude-code")
    assert out["coverage"]["variant"] == "default"


def test_an_override_mapping_without_variants_reports_default(cli, tmp_path):
    path = write_jsonl(tmp_path / "x.jsonl", [{"kind": "u", "t": "hi"}])
    _, out = cli.json(path, "--host", "cursor", "--mapping", "-", stdin=FLAT)
    assert out["coverage"]["variant"] == "default"
    assert out["coverage"]["mapping_source"] == "override"
