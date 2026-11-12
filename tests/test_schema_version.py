"""Every mapping declares schema_version; a mapping newer than the script is refused (exit 2)."""
import tomllib
from pathlib import Path

import pytest
from fixtures.builders import cu_assistant, cu_user, write_jsonl

MAPPINGS = Path(__file__).resolve().parent.parent / "skills" / "workflow-reviewer" / "scripts" / "mappings"
BODY = ('host = "x"\ntype_path = "role"\n[[skill_folders]]\npath = ".x/skills"\n[capabilities]\nhas_tokens = false\n'
        '[[records]]\nmatch = { role = "user" }\nrole = "user"\ntext = ["message.content[type=text].text"]\n'
        '[[records]]\nmatch = { role = "assistant" }\nrole = "assistant"\n')


def run(cli, tmp_path, header=""):
    path = write_jsonl(tmp_path / "c.jsonl", [cu_user("hi"), cu_assistant("ok")])
    return cli.run(path, "--host", "cursor", "--mapping", "-", stdin=header + BODY)


@pytest.mark.parametrize("file", sorted(MAPPINGS.glob("*.toml")), ids=lambda p: p.stem)
def test_every_builtin_mapping_declares_schema_version_1(file):
    assert tomllib.loads(file.read_text(encoding="utf-8"))["schema_version"] == 1


def test_builtin_mappings_need_no_note(cli, tmp_path):
    path = write_jsonl(tmp_path / "c.jsonl", [cu_user("hi"), cu_assistant("ok")])
    _, out = cli.json(path, "--host", "cursor")
    assert "notes" not in out["coverage"]


def test_a_mapping_without_schema_version_is_treated_as_1_with_a_note(cli, tmp_path):
    import json
    proc = run(cli, tmp_path)
    out = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert out["coverage"]["notes"] == ["mapping has no schema_version; treated as 1"]


def test_schema_version_1_is_accepted_without_a_note(cli, tmp_path):
    import json
    proc = run(cli, tmp_path, "schema_version = 1\n")
    assert proc.returncode == 0 and "notes" not in json.loads(proc.stdout)["coverage"]


def test_a_newer_schema_is_refused_with_exit_2(cli, tmp_path):
    import json
    proc = run(cli, tmp_path, "schema_version = 99\n")
    assert proc.returncode == 2
    assert json.loads(proc.stdout) == {"error": "mapping schema 99 is newer than this script supports (max 1)"}


@pytest.mark.parametrize("bad", ["0", '"1"', "true", "1.5"])
def test_a_malformed_schema_version_is_refused(cli, tmp_path, bad):
    proc = run(cli, tmp_path, f"schema_version = {bad}\n")
    assert proc.returncode == 2 and "schema_version" in proc.stdout
