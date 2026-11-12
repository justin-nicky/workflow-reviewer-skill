"""A chain that cannot be walked to its root is reported, never ended silently."""
import json
import time

from test_sqlite_container import assistant, build, db, user  # noqa: F401  (db is a fixture)


def chain_db(db, cli, nodes, heads=()):
    cwd, t = str(cli.repo.resolve()), time.time() - 3600
    build(db.path, [("s1", cwd, t, t + 10, nodes[-1][1])],
          [("s1", i, p, d, t + k) for k, (_, i, p, d) in enumerate(nodes)], heads=heads)


def test_a_parent_that_is_set_but_missing_is_reported(cli, db):
    chain_db(db, cli, [("s1", "n1", "gone-1", user("one")), ("s1", "n2", "n1", assistant("a")), ("s1", "n3", "n2", user("two"))])
    code, out = db.json()
    assert code == 0 and out["window"]["turns_total"] == 2
    assert "chain broken: 1 earlier nodes unreachable" in out["coverage"]["notes"]
    assert "gone-1" not in json.dumps(out)


def test_a_loop_in_the_chain_is_reported_with_a_masked_id(cli, db):
    chain_db(db, cli, [("s1", "loop-a", "loop-c", user("one")), ("s1", "loop-b", "loop-a", assistant("a")), ("s1", "loop-c", "loop-b", user("two"))])
    code, out = db.json()
    notes = out["coverage"]["notes"]
    assert code == 0 and any(n.startswith("chain loop at node ") for n in notes)
    assert "loop-a" not in json.dumps(out) and "loop-c" not in json.dumps(out)


def test_a_broken_subagent_chain_is_reported_too(cli, db):
    chain_db(db, cli, [("s1", "n1", None, user("go")), ("s1", "a2", "missing-9", assistant("x")), ("s1", "n2", "n1", assistant("done"))],
             heads=[("s1", "agent", "a2")])
    _, out = db.json()
    assert "chain broken: 1 earlier nodes unreachable (subagent)" in out["coverage"]["notes"]


def test_a_whole_chain_has_no_chain_note(cli, db):
    chain_db(db, cli, [("s1", "n1", None, user("one")), ("s1", "n2", "n1", assistant("a"))])
    _, out = db.json()
    assert "notes" not in out["coverage"]
