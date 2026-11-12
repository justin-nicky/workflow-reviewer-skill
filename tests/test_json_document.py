"""json_document storage: a session is a folder of JSON documents, merged and read through the normal mapping engine."""
import importlib.util
import json
import os
import time

import pytest

from conftest import SCRIPT

spec = importlib.util.spec_from_file_location("tm_jd", SCRIPT)
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)


def mapping(root, merge='by = "ts"', extra="", records="."):
    return f'''
host = "jd"
schema_version = 1
type_path = "role"
[container]
kind = "json_document"
roots = ["{root}"]
[container.session]
cwd_document = "meta.json"
cwd_path = "cwd"
[[container.documents]]
file = "api.json"
records = "{records}"
[[container.documents]]
file = "ui.json"
records = "."
[container.merge]
{merge}
{extra}
[capabilities]
has_tokens = false
has_timestamps = true
has_tool_results = false
has_ids = false
[fields]
timestamp = "ts"
[[records]]
match = {{ "$doc" = "ui" }}
role = "meta"
[[records]]
match = {{ role = "user" }}
role = "user"
text = ["text"]
[[records]]
match = {{ role = "assistant" }}
role = "assistant"
text = ["text"]
'''


def make_session(root, sid, cwd, api, ui, age=7200):
    folder = root / sid
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"cwd": str(cwd)}))
    (folder / "api.json").write_text(json.dumps(api))
    (folder / "ui.json").write_text(json.dumps(ui))
    stamp = time.time() - age
    for f in folder.iterdir():
        os.utime(f, (stamp, stamp))
    return folder


def run(cli, tmp_path, text, *args):
    (tmp_path / "m.toml").write_text(text)
    proc = cli.run(*args, "--mapping", tmp_path / "m.toml")
    return proc, json.loads(proc.stdout)


def basic(cli, tmp_path, sid="s1", age=7200, cwd=None):
    root = tmp_path / "sessions"
    root.mkdir(exist_ok=True)
    make_session(root, sid, cwd or cli.repo.resolve(),
                 api=[{"role": "user", "text": "one", "ts": 100}, {"role": "assistant", "text": "a", "ts": 104}, {"role": "user", "text": "two", "ts": 110}],
                 ui=[{"say": "x", "text": "ui one", "ts": 102}, {"say": "y", "text": "ui two", "ts": 108}], age=age)
    return root


def test_records_from_several_documents_are_merged_by_timestamp(cli, tmp_path):
    root = basic(cli, tmp_path)
    proc, out = run(cli, tmp_path, mapping(root), "--events", "0:9")
    assert proc.returncode == 0
    docs = [(e["role"], e["text"][:6]) for e in out["events"]]
    assert docs == [("user", "one"), ("meta", ""), ("assistant", "a"), ("meta", ""), ("user", "two")]


def test_order_merge_keeps_document_order(cli, tmp_path):
    root = basic(cli, tmp_path)
    text = mapping(root, merge='by = "order"').replace('[fields]\ntimestamp = "ts"\n', "").replace("has_timestamps = true", "has_timestamps = false")
    _, out = run(cli, tmp_path, text, "--events", "0:9")                       # no clock: events stay in merge order
    assert [e["role"] for e in out["events"]] == ["user", "assistant", "user", "meta", "meta"]


def test_a_record_knows_which_document_it_came_from(cli, tmp_path):
    root = basic(cli, tmp_path)
    proc, out = run(cli, tmp_path, mapping(root))
    assert proc.returncode == 0 and out["window"]["turns_total"] == 2
    assert out["coverage"]["container"] == "json_document" and out["coverage"]["storage"] == {"kind": "json_document", "documents": 2, "records": 5}


def test_the_record_list_can_sit_below_the_document_root(cli, tmp_path):
    root = tmp_path / "sessions"
    folder = root / "s1"
    folder.mkdir(parents=True)
    (folder / "api.json").write_text(json.dumps({"data": {"messages": [{"role": "user", "text": "hi", "ts": 1}]}}))
    (folder / "ui.json").write_text("[]")
    (folder / "meta.json").write_text(json.dumps({"cwd": str(cli.repo.resolve())}))
    proc, out = run(cli, tmp_path, mapping(root, records="data.messages"))
    assert proc.returncode == 0 and out["window"]["turns_total"] == 1


def test_a_folder_argument_reads_that_session(cli, tmp_path):
    root = basic(cli, tmp_path, "s1")
    proc, out = run(cli, tmp_path, mapping(root), root / "s1")
    assert proc.returncode == 0 and out["window"]["turns_total"] == 2


def test_session_id_is_the_folder_name(cli, tmp_path):
    root = basic(cli, tmp_path, "s1")
    make_session(root, "s2", cli.repo.resolve(), api=[{"role": "user", "text": "x", "ts": 1}], ui=[], age=3600)
    _, out = run(cli, tmp_path, mapping(root), "--session-id", "s2")
    assert out["window"]["turns_total"] == 1
    proc, out = run(cli, tmp_path, mapping(root), "--session-id", "nope")
    assert proc.returncode == 6


def test_the_most_recent_session_in_this_working_directory_is_chosen(cli, tmp_path):
    root = basic(cli, tmp_path, "old", age=9000)
    make_session(root, "new", cli.repo.resolve(), api=[{"role": "user", "text": "x", "ts": 1}], ui=[], age=3000)
    make_session(root, "elsewhere", tmp_path / "other", api=[{"role": "user", "text": "x", "ts": 1}] * 4, ui=[], age=10)
    _, out = run(cli, tmp_path, mapping(root))
    assert out["window"]["turns_total"] == 1


def test_two_recent_sessions_are_ambiguous(cli, tmp_path):
    root = basic(cli, tmp_path, "a", age=20)
    make_session(root, "b", cli.repo.resolve(), api=[{"role": "user", "text": "x", "ts": 1}], ui=[], age=30)
    proc, out = run(cli, tmp_path, mapping(root))
    assert proc.returncode == 3 and {a["session"] for a in out["ambiguous"]} == {"a", "b"}
    assert all(set(a) == {"session", "working_directory", "created", "last_activity"} for a in out["ambiguous"])


def test_no_session_here_is_the_degraded_exit_6(cli, tmp_path):
    root = basic(cli, tmp_path, "s1", cwd=tmp_path / "elsewhere")
    proc, out = run(cli, tmp_path, mapping(root))
    assert proc.returncode == 6 and out["mode"] == "degraded"


# ---- safety ---------------------------------------------------------------------------------------------------------------------

def test_only_the_chosen_sessions_documents_are_read(cli, tmp_path, monkeypatch, capsys):
    root = basic(cli, tmp_path, "mine", age=3000)
    make_session(root, "other", cli.repo.resolve(), api=[{"role": "user", "text": "PLANTED-OTHER", "ts": 1}], ui=[], age=9000)
    reads = []
    real = tm.read_document
    monkeypatch.setattr(tm, "read_document", lambda p: (reads.append(p), real(p))[1])
    (tmp_path / "m.toml").write_text(mapping(root).replace('[container.session]\ncwd_document = "meta.json"\ncwd_path = "cwd"\n', ""))
    monkeypatch.chdir(cli.repo)
    assert tm.main(["--mapping", str(tmp_path / "m.toml"), "--format", "json"]) == 0
    assert "PLANTED-OTHER" not in capsys.readouterr().out
    assert {p.parent.name for p in reads} == {"mine"}                    # the other session's documents are never opened


def test_a_planted_secret_in_the_chosen_session_never_reaches_the_output(cli, tmp_path):
    root = tmp_path / "sessions"
    secret = "API_KEY=sk_" "live_abcdefghijklmnop0123456789"
    make_session(root, "s1", cli.repo.resolve(), api=[{"role": "user", "text": f"use {secret}", "ts": 1}], ui=[{"say": "x", "text": secret, "ts": 2}])
    text = mapping(root)
    for flags in ([], ["--sample", "5"], ["--events", "0:3"], ["--list-turns"]):
        proc, _ = run(cli, tmp_path, text, *flags)
        assert "sk_live" not in proc.stdout and "abcdefghijklmnop0123456789" not in proc.stdout, flags


def test_a_malformed_document_is_exit_2_and_names_the_file(cli, tmp_path):
    root = basic(cli, tmp_path)
    (root / "s1" / "ui.json").write_text("{not json")
    proc, out = run(cli, tmp_path, mapping(root), root / "s1")
    assert proc.returncode == 2 and "ui.json" in out["error"] and "not valid JSON" in out["error"]
    assert str(root) not in proc.stdout


def test_a_document_over_the_size_cap_is_exit_4(cli, tmp_path, monkeypatch, capsys):
    root = basic(cli, tmp_path)
    (tmp_path / "m.toml").write_text(mapping(root))
    monkeypatch.setattr(tm, "JSON_DOC_CAP", 50)
    monkeypatch.chdir(cli.repo)
    assert tm.main(["--mapping", str(tmp_path / "m.toml"), str(root / "s1"), "--format", "json"]) == 4
    assert "limit" in json.loads(capsys.readouterr().out)["error"]


def test_a_document_without_its_record_list_is_exit_2(cli, tmp_path):
    root = basic(cli, tmp_path)
    (root / "s1" / "api.json").write_text(json.dumps({"other": []}))
    proc, out = run(cli, tmp_path, mapping(root, records="data.messages"), root / "s1")
    assert proc.returncode == 2 and "api.json" in out["error"] and "record list" in out["error"]


@pytest.mark.parametrize("bad", ["../escape.json", "/etc/passwd", "a/b.json", ".."])
def test_a_document_name_must_be_a_plain_file_name(cli, tmp_path, bad):
    root = basic(cli, tmp_path)
    proc, out = run(cli, tmp_path, mapping(root).replace('file = "ui.json"', f'file = "{bad}"'), root / "s1")
    assert proc.returncode == 2 and "plain file name" in out["error"]


def test_the_documents_are_never_written(cli, tmp_path):
    root = basic(cli, tmp_path)
    before = {f: f.read_bytes() for f in (root / "s1").iterdir()}
    run(cli, tmp_path, mapping(root))
    assert {f: f.read_bytes() for f in (root / "s1").iterdir()} == before and sorted(p.name for p in (root / "s1").iterdir()) == sorted(f.name for f in before)


def test_tool_names_alone_can_set_the_category_without_a_kind_field(cli, tmp_path):
    root = tmp_path / "sessions"
    make_session(root, "s1", cli.repo.resolve(), ui=[], api=[
        {"role": "user", "text": "go", "ts": 1},
        {"role": "assistant", "text": "a", "ts": 2, "calls": [{"name": "read_file", "input": {"path": "a.py"}, "id": "c1"},
                                                              {"name": "execute_command", "input": {"command": "python3 -m pytest -q"}, "id": "c2"},
                                                              {"name": "write_to_file", "input": {"path": "a.py"}, "id": "c3"}]}])
    text = mapping(root) + '''
[call_name_categories]
read_file = "read"
write_to_file = "edit"
execute_command = "shell"
''' 
    text = text.replace('[[records]]\nmatch = { role = "assistant" }\nrole = "assistant"\ntext = ["text"]',
                        '[[records]]\nmatch = { role = "assistant" }\nrole = "assistant"\ntext = ["text"]\ncalls = "calls[]"\ncall_name = "name"\ncall_input = "input"\ncall_id = "id"')
    # the call_name_categories table must sit before the first [[records]]; TOML tables can come in any order
    proc, out = run(cli, tmp_path, text, root / "s1")
    assert proc.returncode == 0 and out["tools"]["by_category"] == {"read": 1, "shell": 1, "edit": 1} and out["verify"]["total"] == 1
