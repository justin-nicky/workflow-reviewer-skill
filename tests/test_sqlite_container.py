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
