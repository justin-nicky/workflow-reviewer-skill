"""Sessions stored in a SQLite database, read through a declarative mapping (test-only host)."""
import json
import sqlite3
import time

import pytest

MAPPING = '''
host = "dbhost"
schema_version = 1
type_path = "type"

[container]
kind = "sqlite"
default_path = "~/dbhost/sessions.db"

[container.sessions]
table = "sessions"
id = "id"
cwd = "cwd"
created = "created_at"
last_activity = "updated_at"
head = "head_node"

[container.nodes]
table = "nodes"
session = "session_id"
id = "node_id"
parent = "parent_id"
payload = "payload"
created = "created_at"

[container.subagent_heads]
table = "subagent_heads"
session = "session_id"
agent = "agent_id"
head = "head_node"

[capabilities]
has_tokens = false
has_timestamps = true
has_tool_results = false
has_ids = false

[fields]
timestamp = ["ts", "$row.created_at"]

[[records]]
match = { type = "user" }
role = "user"
text = ["text"]

[[records]]
match = { type = "assistant" }
role = "assistant"
text = ["text"]
calls = "calls[]"
call_name = "name"
call_input = "input"
call_id = "id"
'''


def build(path, sessions, nodes, heads=(), wal=False):
    """sessions: (id, cwd, created, updated, head); nodes: (session, id, parent, payload dict, created)."""
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        "CREATE TABLE sessions (id TEXT, cwd TEXT, created_at REAL, updated_at REAL, head_node TEXT);"
        "CREATE TABLE nodes (session_id TEXT, node_id TEXT, parent_id TEXT, payload TEXT, created_at REAL);"
        "CREATE TABLE subagent_heads (session_id TEXT, agent_id TEXT, head_node TEXT);")
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", sessions)
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?)", [(s, i, p, json.dumps(d), t) for s, i, p, d, t in nodes])
    conn.executemany("INSERT INTO subagent_heads VALUES (?,?,?)", heads)
    conn.commit()
    return conn


def user(text):
    return {"type": "user", "text": text}


def assistant(text, calls=()):
    return {"type": "assistant", "text": text, "calls": [{"name": n, "input": i, "id": f"c{k}"} for k, (n, i) in enumerate(calls)]}


@pytest.fixture
def db(cli, tmp_path):
    class Db:
        path = tmp_path / "sessions.db"
        mapping = MAPPING

        def run(self, *args, mapping=None):
            return cli.run(self.path, *args, "--mapping", "-", stdin=mapping or self.mapping)

        def json(self, *args, mapping=None):
            proc = self.run(*args, mapping=mapping)
            try:
                return proc.returncode, json.loads(proc.stdout)
            except ValueError:
                raise AssertionError(f"not JSON (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return Db()


def now(offset=0):
    return time.time() - 3600 - offset


def simple(cli, db, **kw):
    cwd = str(cli.repo.resolve())
    t = now()
    return build(db.path, [("s1", cwd, t, t + 10, "n3")],
                 [("s1", "n1", None, user("one"), t), ("s1", "n2", "n1", assistant("a", [("Bash", {"command": "pytest -q"})]), t + 1),
                  ("s1", "n3", "n2", user("two"), t + 2)], **kw)


def test_the_chain_is_walked_from_the_head_and_off_chain_drafts_are_counted(cli, db):
    cwd, t = str(cli.repo.resolve()), now()
    build(db.path, [("s1", cwd, t, t + 10, "n3")],
          [("s1", "n1", None, user("one"), t), ("s1", "n2", "n1", assistant("a"), t + 1),
           ("s1", "n3", "n2", user("two"), t + 2), ("s1", "n9", "n2", user("a draft that was never sent"), t + 3)])
    code, out = db.json()
    assert code == 0 and out["window"]["turns_total"] == 2
    assert out["coverage"]["notes"] == ["off-chain nodes: 1"] and out["coverage"]["variant"] == "default"
    assert out["coverage"]["container"] == "sqlite"


def test_two_subagent_heads_are_two_actors_linked_by_their_heads(cli, db):
    cwd, t = str(cli.repo.resolve()), now()
    build(db.path, [("s1", cwd, t, t + 10, "n2")],
          [("s1", "n1", None, user("go"), t), ("s1", "n2", "n1", assistant("done"), t + 1),
           ("s1", "a1", None, user("brief one"), t + 2), ("s1", "a2", "a1", assistant("found", [("Bash", {"command": "ls"})]), t + 3),
           ("s1", "b1", None, user("brief two"), t + 4)],
          heads=[("s1", "agent-one", "a2"), ("s1", "agent-two", "b1"), ("other", "agent-x", "zz")])
    code, out = db.json()
    assert code == 0
    assert [a["actor"] for a in out["actors"]] == ["main", "subagent:agent-one", "subagent:agent-two"]
    assert [a.get("link") for a in out["actors"]] == [None, "subagent_heads", "subagent_heads"]
    assert out["window"]["turns_total"] == 1 and out["actors"][1]["tool_calls"] == 1
    assert "notes" not in out["coverage"]


def test_row_columns_are_reachable_from_the_mapping(cli, db):
    simple(cli, db)
    _, out = db.json("--events", "0:5")
    stamps = [e["ts"] for e in out["events"]]
    assert all(stamps) and stamps == sorted(stamps)           # the payload has no ts; the row's created_at supplies it


def test_sample_events_and_list_turns_work_like_jsonl(cli, db):
    simple(cli, db)
    _, sample = db.json("--sample", "5")
    assert len(sample["sample"]) == 3 and all(json.loads(s)["$row"]["node_id"] for s in sample["sample"])
    _, events = db.json("--events", "0:1")
    assert [e["role"] for e in events["events"]] == ["user", "assistant"]
    _, turns = db.json("--list-turns")
    assert [t["preview"] for t in turns["turns"]] == ["one", "two"]
    _, full = db.json()
    assert full["verify"]["total"] == 1 and full["tools"]["by_category"] == {"shell": 1}


# ---- session selection: the same rules as jsonl ------------------------------------------------------------------

def two_sessions(cli, db, age_a=7200, age_b=3600, cwd_b=None):
    cwd = str(cli.repo.resolve())
    t = time.time()
    nodes = []
    for sid, text in (("sa", "session a prompt"), ("sb", "session b prompt")):
        nodes.append((sid, f"{sid}1", None, user(text), t - 9000))
    build(db.path, [("sa", cwd, t - 9000, t - age_a, "sa1"), ("sb", cwd_b or cwd, t - 9000, t - age_b, "sb1")], nodes)


def test_the_most_recent_session_in_this_working_directory_is_chosen(cli, db):
    two_sessions(cli, db)
    _, out = db.json("--list-turns")
    assert [t["preview"] for t in out["turns"]] == ["session b prompt"]


def test_only_sessions_of_this_working_directory_are_considered(cli, db, tmp_path):
    two_sessions(cli, db, cwd_b=str(tmp_path / "elsewhere"))
    _, out = db.json("--list-turns")
    assert [t["preview"] for t in out["turns"]] == ["session a prompt"]


def test_session_id_selects_the_session(cli, db):
    two_sessions(cli, db)
    _, out = db.json("--list-turns", "--session-id", "sa")
    assert [t["preview"] for t in out["turns"]] == ["session a prompt"]


def test_two_recent_sessions_are_listed_not_guessed_without_prompts(cli, db):
    two_sessions(cli, db, age_a=60, age_b=120)
    code, out = db.json()
    assert code == 3 and {a["session"] for a in out["ambiguous"]} == {"sa", "sb"}
    row = out["ambiguous"][0]
    assert set(row) == {"session", "working_directory", "created", "last_activity"}
    assert "prompt" not in json.dumps(out) and "tokens" not in out


def test_no_session_for_this_directory_is_the_degraded_report(cli, db, tmp_path):
    two_sessions(cli, db, cwd_b=str(tmp_path / "elsewhere"))
    cwd_a = db.path
    code, out = db.json("--session-id", "missing")
    assert code == 6 and out["transcript_found"] is False


def test_the_default_path_is_used_when_no_path_is_given(cli, db):
    default = cli.home / "dbhost" / "sessions.db"
    default.parent.mkdir()
    db.path = default
    simple(cli, db)
    proc = cli.run("--mapping", "-", "--format", "json", stdin=MAPPING)
    assert proc.returncode == 0 and json.loads(proc.stdout)["window"]["turns_total"] == 2


# ---- safety ---------------------------------------------------------------------------------------------------

def test_a_wal_database_is_read_while_another_connection_holds_it_open_for_writing(cli, db):
    writer = simple(cli, db, wal=True)
    writer.execute("BEGIN IMMEDIATE")                         # a running app mid-write; its WAL is not checkpointed
    writer.execute("INSERT INTO nodes VALUES ('s1', 'n7', 'n3', '{\"type\": \"user\", \"text\": \"uncommitted\"}', 0)")
    try:
        code, out = db.json()
    finally:
        writer.rollback()
        writer.close()
    assert code == 0 and out["window"]["turns_total"] == 2


def test_committed_but_uncheckpointed_wal_rows_are_included(cli, db):
    writer = simple(cli, db, wal=True)
    writer.execute("UPDATE sessions SET head_node = 'n4'")
    writer.execute("INSERT INTO nodes VALUES ('s1', 'n4', 'n3', ?, ?)", (json.dumps(assistant("late")), now() + 5))
    writer.commit()                                           # stays in the -wal file while the writer is open
    try:
        _, out = db.json("--events", "0:9")
    finally:
        writer.close()
    assert [e["text"] for e in out["events"]][-1] == "late"


def test_the_database_is_opened_read_only(cli, db):
    simple(cli, db).close()
    before = db.path.read_bytes()
    db.json()
    assert db.path.read_bytes() == before and not list(db.path.parent.glob("sessions.db-*"))


@pytest.mark.parametrize("flag", [(), ("--sample", "50"), ("--events", "0:99"), ("--list-turns",), ("--format", "md")])
def test_another_sessions_rows_never_appear_in_any_output(cli, db, flag):
    secret = "sk-ant-PLANTEDSECRET0123456789abcdef"
    cwd, t = str(cli.repo.resolve()), now()
    build(db.path, [("s1", cwd, t, t + 10, "n2"), ("other", "/somewhere/else", t, t + 5, "o2")],
          [("s1", "n1", None, user("mine"), t), ("s1", "n2", "n1", assistant("ok"), t + 1),
           ("other", "o1", None, user(f"the key is {secret}"), t), ("other", "o2", "o1", assistant("PLANTED reply"), t + 1)],
          heads=[("other", "agent-x", "o2")])
    proc = db.run(*flag)
    assert proc.returncode == 0 and "PLANTED" not in proc.stdout + proc.stderr and "somewhere" not in proc.stdout


def test_a_missing_column_is_drift_and_names_it(cli, db):
    conn = simple(cli, db)
    conn.close()
    bad = MAPPING.replace('payload = "payload"', 'payload = "body"')
    code, out = db.json(mapping=bad)
    assert code == 2 and out["missing"] == ["column nodes.body"]
    assert "nodes.body" in out["error"]


def test_a_missing_table_is_drift_and_names_it(cli, db):
    simple(cli, db).close()
    code, out = db.json(mapping=MAPPING.replace('table = "subagent_heads"', 'table = "agent_heads"'))
    assert code == 2 and out["missing"] == ["table agent_heads"]


def test_a_virtual_table_is_never_read(cli, db):
    conn = simple(cli, db)
    try:
        conn.execute("CREATE VIRTUAL TABLE fts USING fts5(payload)")
    except sqlite3.OperationalError:
        pytest.skip("this SQLite has no FTS5")
    conn.commit()
    conn.close()
    code, out = db.json(mapping=MAPPING.replace('table = "nodes"', 'table = "fts"'))
    assert code == 2 and "virtual" in out["error"]


def test_a_file_that_is_not_a_database_is_refused(cli, db):
    db.path.write_text("not a database at all")
    code, out = db.json()
    assert code == 2 and "database" in out["error"]


@pytest.mark.parametrize("evil", ['nodes; DROP TABLE x', 'x" OR 1=1', "nodes--", "nodes UNION SELECT 1", "no des", "", "1nodes"])
def test_hostile_identifiers_in_an_override_are_rejected_before_any_query(cli, db, evil):
    conn = simple(cli, db)
    conn.close()
    before = db.path.read_bytes()
    bad = MAPPING.replace('table = "nodes"', f'table = "{evil.replace(chr(34), chr(92) + chr(34))}"')
    code, out = db.json(mapping=bad)
    assert code == 2 and "plain identifier" in out["error"]
    assert db.path.read_bytes() == before


def test_identifiers_are_vetted_before_the_file_is_even_looked_at(cli, db):
    bad = MAPPING.replace('id = "node_id"', 'id = "node_id; DROP TABLE nodes"')
    code, out = db.json(mapping=bad)                          # the database file does not exist
    assert code == 2 and "plain identifier" in out["error"]


def test_a_column_name_cannot_smuggle_sql_either(cli, db):
    simple(cli, db).close()
    code, out = db.json(mapping=MAPPING.replace('head = "head_node"', 'head = "head_node) FROM sessions --"', 1))
    assert code == 2 and "plain identifier" in out["error"]
