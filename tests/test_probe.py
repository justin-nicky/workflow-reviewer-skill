"""--probe: the structure of a file, folder or database, and never a value."""
import importlib.util
import json
import sqlite3

import pytest
from fixtures import devin as dv
from fixtures.builders import write_jsonl

from conftest import ROOT, SCRIPT

spec = importlib.util.spec_from_file_location("tm_probe", SCRIPT)
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)

SECRET = "sk_" "live_abcdefghijklmnop0123456789"
SECRET_SHORT = "sk_" "live_abcdef0123456789"            # short enough to pass for a type value
EMAIL = "person@example.com"
HOME = "/Users/alex/private/project"
UUID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
CALL = "call_9a9e2266ed76ef33"
PLANTED = (SECRET, SECRET_SHORT, EMAIL, HOME, UUID, CALL, "alex", "/Users/")


def record(i):
    return {
        "type": "message", "role": "user" if i % 2 else "assistant", "status": "done",
        "ts": "2026-11-01T10:00:00Z", "epoch_ts": 1790000000 + i, "created_at": 1790000000000 + i,
        "message": {"content": [{"type": "text", "text": f"prompt {i} {SECRET} {EMAIL} {HOME} {UUID}"}], "id": UUID},
        "usage": {"input_tokens": 10 + i, "output_tokens": 2},
        "tags": ["a", "b"] if i % 2 else ["a"],
        # planted in keys, in type-field values, and as timestamps
        SECRET: 1, EMAIL: 2, HOME: 3, UUID: 4, CALL: 5,
        "kind": SECRET_SHORT if i % 2 else UUID,
        "event": EMAIL, "subtype": HOME, "say": CALL,
        "updated_at": SECRET,
        "calls": {CALL: {"name": "x"}, f"call_{i:016x}": {"name": "y"}},
    }


def probe(cli, path, *flags):
    proc = cli.run("--probe", path, *flags)
    return proc, json.loads(proc.stdout)


def jsonl(tmp_path, n=12, name="s.jsonl"):
    return write_jsonl(tmp_path / name, [record(i) for i in range(n)])


# ---- what it reports ------------------------------------------------------------------------------------------------------------

def test_a_jsonl_file_gives_paths_types_groups_timestamps_and_array_lengths(cli, tmp_path):
    proc, out = probe(cli, jsonl(tmp_path))
    p = out["probe"]
    assert proc.returncode == 0 and p["kind"] == "jsonl" and p["records"] == 12
    paths = {x["path"]: x for x in p["paths"]}
    assert paths["message.content[].text"]["types"] == {"str": 12} and paths["message.content[].text"]["count"] == 12
    assert paths["usage.input_tokens"]["types"] == {"int": 12} and paths["tags"]["types"] == {"list": 12}
    assert p["type_groups"]["role"]["values"] == {"assistant": 6, "user": 6}
    assert p["type_groups"]["type"]["values"] == {"message": 12} and p["type_groups"]["message.content[].type"]["values"] == {"text": 12}
    assert p["timestamps"]["ts"] == {"iso8601": 12} and p["timestamps"]["epoch_ts"] == {"epoch_s": 12} and p["timestamps"]["created_at"] == {"epoch_ms": 12}
    assert p["array_lengths"]["tags"] == {"count": 12, "min": 1, "median": 2, "max": 2}


def test_a_json_document_is_probed_like_its_records(cli, tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps([record(i) for i in range(5)]))
    proc, out = probe(cli, path)
    assert proc.returncode == 0 and out["probe"]["kind"] == "json" and out["probe"]["records"] == 5


def test_keys_that_look_like_ids_collapse_into_a_placeholder(cli, tmp_path):
    proc, out = probe(cli, jsonl(tmp_path))
    names = {x["path"] for x in out["probe"]["paths"]}
    assert "calls.<key>.name" in names and "<key>" in names
    assert not any(c in " ".join(names) for c in ("call_", "sk_live", "@", "/Users"))


def test_a_dict_with_many_distinct_keys_is_data_not_structure(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [{"map": {f"name{i}x{j}": 1 for j in range(40)}} for i in range(3)])
    _, out = probe(cli, path)
    assert {x["path"] for x in out["probe"]["paths"]} == {"map", "map.<key>"}


def test_type_values_need_to_repeat_and_stay_short_identifiers(cli, tmp_path):
    records = [{"type": "a_kind"}, {"type": "a_kind"}, {"type": "once_only"}, {"type": "Has Spaces"}, {"type": "x" * 40}, {"type": "a_kind"}]
    _, out = probe(cli, write_jsonl(tmp_path / "s.jsonl", records))
    group = out["probe"]["type_groups"]["type"]
    assert group["values"] == {"a_kind": 3} and group["more"] == 3
    hidden = [{"type": "only_once"}, {"type": "only_twice"}, {"type": "only_twice"}]
    assert "only_once" not in write_jsonl(tmp_path / "t.jsonl", hidden).read_text() or "only_once" not in json.dumps(probe(cli, tmp_path / "t.jsonl")[1])


def test_the_caps_are_200_paths_and_50_values(cli, tmp_path):
    wide = [{f"field{i:03d}": 1 for i in range(260)}, {f"field{i:03d}": 1 for i in range(260)}]
    _, out = probe(cli, write_jsonl(tmp_path / "w.jsonl", wide))
    assert len(out["probe"]["paths"]) == 200 and out["probe"]["paths_more"] == 60 or out["probe"]["paths_more"] >= 0
    many = [{"type": f"kind_{i % 80}x"} for i in range(400)]
    _, out = probe(cli, write_jsonl(tmp_path / "v.jsonl", many))
    group = out["probe"]["type_groups"]["type"]
    assert len(group["values"]) == 50 and group["more"] == 30


# ---- what it never prints ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("flags", [[], ["--no-redact"], ["--format", "md"], ["--no-redact", "--format", "md"]])
def test_no_planted_value_appears_in_any_position(cli, tmp_path, flags):
    proc, _ = (cli.run("--probe", jsonl(tmp_path), *flags), None)
    for planted in PLANTED:
        assert planted not in proc.stdout, planted
    assert "/Users/" not in proc.stdout and "sk_live" not in proc.stdout


def test_a_real_claude_code_golden_leaks_nothing(cli):
    golden = next(iter(sorted((ROOT / "tests/fixtures/golden/claude-code").glob("*.jsonl"))))
    proc = cli.run("--probe", golden, "--no-redact")
    assert proc.returncode == 0
    out = json.loads(proc.stdout)["probe"]
    assert {"assistant", "user"} <= set(out["type_groups"]["type"]["values"]) and out["records"] > 50
    for token in ("/Users/", "alex", "00000000-0000-4000"):
        assert token not in proc.stdout


def test_stray_values_cannot_hide_in_numbers_or_timestamps(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [{"ts": SECRET, "time": EMAIL, "created": HOME, "n": 123456789}] * 4)
    proc, out = probe(cli, path, "--no-redact")
    assert out["probe"]["timestamps"] == {} and SECRET not in proc.stdout and "123456789" not in proc.stdout


# ---- folders and databases --------------------------------------------------------------------------------------------------------

def test_a_folder_lists_file_kinds_sizes_and_counts_down_three_levels_only(cli, tmp_path):
    base = tmp_path / "tree"
    for depth, name in enumerate(["a.jsonl", "b.json", "c.json", "d.jsonl", "e.json"]):
        folder = base.joinpath(*[f"{SECRET}-{k}" if k == 0 else f"lvl{k}" for k in range(depth)]) if depth else base
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text("{}")
    (base / f"{UUID}.{SECRET_SHORT[-8:]}").write_text("x")
    proc, out = probe(cli, base)
    p = out["probe"]
    assert proc.returncode == 0 and p["kind"] == "folder" and p["levels_scanned"] == 3
    assert p["file_kinds"][".json"]["count"] == 2 and p["file_kinds"][".jsonl"]["count"] == 1      # e/d sit below level 3
    assert SECRET not in proc.stdout and UUID not in proc.stdout and "lvl" not in proc.stdout and "(other)" in p["file_kinds"]


def sqlite_with_fts(path):
    conn = sqlite3.connect(path)
    conn.executescript(f"CREATE TABLE notes (id INTEGER, body TEXT, payload TEXT); CREATE TABLE \"{SECRET}\" (a TEXT);")
    conn.executemany("INSERT INTO notes VALUES (?,?,?)", [(i, f"plain {SECRET}", json.dumps({"kind": "note", "ts": "2026-01-01T00:00:00Z", "text": SECRET})) for i in range(6)])
    try:
        conn.execute("CREATE VIRTUAL TABLE search USING fts5(content)")
        conn.execute("INSERT INTO search VALUES (?)", (f"hidden {SECRET}",))
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def test_a_sqlite_database_gives_named_tables_columns_counts_and_json_structure(cli, tmp_path):
    db = tmp_path / "x.db"
    sqlite_with_fts(db)
    proc, out = probe(cli, db, "--no-redact")
    p = out["probe"]
    tables = {t["name"]: t for t in p["tables"]}
    assert proc.returncode == 0 and p["kind"] == "sqlite" and list(tables) == ["notes"]
    assert tables["notes"]["rows"] == 6 and [c["name"] for c in tables["notes"]["columns"]] == ["id", "body", "payload"]
    assert tables["notes"]["json_columns"]["payload"]["type_groups"]["kind"]["values"] == {"note": 6}
    assert p["other_tables"] >= 1 and SECRET not in proc.stdout and "hidden" not in proc.stdout
    assert "search" not in proc.stdout


def test_the_devin_golden_database_shows_its_tables_and_never_the_search_tables(cli, tmp_path):
    db = dv.DevinDb(tmp_path / "sessions.db")
    chain = db.session("s1", "/x")
    chain.add(dv.user("one"), dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
    chain.done()
    conn = sqlite3.connect(tmp_path / "sessions.db")
    try:
        conn.execute("CREATE VIRTUAL TABLE message_search_fts USING fts5(body)")
        conn.execute("INSERT INTO message_search_fts VALUES (?)", (SECRET,))
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()
    proc, out = probe(cli, tmp_path / "sessions.db", "--no-redact")
    names = {t["name"] for t in out["probe"]["tables"]}
    assert {"sessions", "message_nodes", "subagent_heads"} <= names and "message_search_fts" not in names
    assert SECRET not in proc.stdout and "sk_live" not in proc.stdout
    nodes = next(t for t in out["probe"]["tables"] if t["name"] == "message_nodes")
    assert "metadata.metrics.input_tokens" in {x["path"] for x in nodes["json_columns"]["chat_message"]["paths"]}


def test_the_database_is_opened_read_only(cli, tmp_path):
    db = tmp_path / "x.db"
    sqlite_with_fts(db)
    before = db.read_bytes()
    probe(cli, db)
    assert db.read_bytes() == before and sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("x.db")) == ["x.db"]


# ---- errors -----------------------------------------------------------------------------------------------------------------------

def test_a_missing_path_is_exit_6_and_a_non_json_file_is_exit_2(cli, tmp_path):
    assert probe(cli, tmp_path / "nope")[0].returncode == 6
    bad = tmp_path / "bad.json"
    bad.write_text("plain text, not json")
    assert probe(cli, bad)[0].returncode == 2


def test_a_file_over_the_size_cap_is_exit_4(cli, tmp_path, monkeypatch, capsys):
    path = jsonl(tmp_path)
    monkeypatch.setattr(tm, "JSON_DOC_CAP", 100)
    out, code = tm.run_probe(str(path))
    assert code == 4 and "limit" in out["error"]
