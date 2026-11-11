"""A mapping declares how its host stores records: [container] kind. jsonl is the default."""
import json

from fixtures.builders import cu_assistant, cu_user, write_jsonl

BODY = ('host = "x"\nschema_version = 1\ntype_path = "role"\n[capabilities]\nhas_tokens = false\n'
        '[[records]]\nmatch = { role = "user" }\nrole = "user"\ntext = ["message.content[type=text].text"]\n'
        '[[records]]\nmatch = { role = "assistant" }\nrole = "assistant"\n')


def run(cli, tmp_path, container=""):
    path = write_jsonl(tmp_path / "c.jsonl", [cu_user("hi"), cu_assistant("ok")])
    return cli.run(path, "--host", "cursor", "--mapping", "-", stdin=container + BODY)


def test_jsonl_is_the_default_container(cli, tmp_path):
    proc = run(cli, tmp_path)
    assert proc.returncode == 0 and json.loads(proc.stdout)["window"]["turns_total"] == 1


def test_an_explicit_jsonl_container_behaves_the_same(cli, tmp_path):
    proc = run(cli, tmp_path, '[container]\nkind = "jsonl"\n\n')
    assert proc.returncode == 0 and json.loads(proc.stdout)["window"]["turns_total"] == 1


def test_an_unknown_container_kind_is_refused_with_exit_2(cli, tmp_path):
    proc = run(cli, tmp_path, '[container]\nkind = "carrier-pigeon"\n\n')
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["error"] == "mapping container kind 'carrier-pigeon' is not supported (jsonl, sqlite)"


def test_json_document_is_reserved_but_not_built_yet(cli, tmp_path):
    proc = run(cli, tmp_path, '[container]\nkind = "json_document"\n\n')
    assert proc.returncode == 2 and "json_document" in json.loads(proc.stdout)["error"]
