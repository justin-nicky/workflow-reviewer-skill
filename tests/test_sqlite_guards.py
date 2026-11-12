"""Guards on the SQLite reader: it never writes, never reads another session's rows, and never ends a broken chain silently."""
import importlib.util
import json
import sqlite3
import time

import pytest

from conftest import SCRIPT
from test_sqlite_container import MAPPING, assistant, build, db, user  # noqa: F401  (db is a fixture)

spec = importlib.util.spec_from_file_location("transcript_metrics_guard", SCRIPT)
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)


def test_a_database_opened_by_the_script_cannot_be_written_to(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (a)")
    conn.commit()
    conn.close()
    ro = tm.open_sqlite_readonly(path)
    for statement in ("INSERT INTO t VALUES (1)", "CREATE TABLE u (a)", "DELETE FROM t"):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute(statement)
    ro.close()


def run_in_process(monkeypatch, capsys, repo, db_path, statements):
    real = tm.open_sqlite_readonly

    def traced(path):
        conn = real(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(tm, "open_sqlite_readonly", traced)
    mapping = repo.parent / "mapping.toml"
    mapping.write_text(MAPPING)
    monkeypatch.chdir(repo)
    code = tm.main([str(db_path), "--mapping", str(mapping), "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


def two_sessions_with_drafts(cli, tmp_path):
    cwd, t = str(cli.repo.resolve()), time.time() - 3600
    nodes = []
    for sid, drafts in (("s1", 2), ("s2", 3)):
        nodes += [(sid, f"{sid}a", None, user("prompt"), t), (sid, f"{sid}b", f"{sid}a", assistant("reply"), t + 1)]
        nodes += [(sid, f"{sid}d{k}", f"{sid}a", user("draft"), t + 2) for k in range(drafts)]
    path = tmp_path / "two.db"
    build(path, [("s1", cwd, t, t + 100, "s1b"), ("s2", cwd, t, t + 5, "s2b")],
          nodes, heads=[("s1", "agent", "s1a"), ("s2", "agent", "s2a")])
    return path


def test_the_off_chain_count_covers_only_the_chosen_session(cli, tmp_path, monkeypatch, capsys):
    path = two_sessions_with_drafts(cli, tmp_path)
    code, out = run_in_process(monkeypatch, capsys, cli.repo, path, [])
    assert code == 0 and out["coverage"]["notes"] == ["off-chain nodes: 2"]   # s2's three drafts are not counted


def test_every_select_on_nodes_and_subagent_heads_is_filtered_by_session(cli, tmp_path, monkeypatch, capsys):
    path = two_sessions_with_drafts(cli, tmp_path)
    statements = []
    run_in_process(monkeypatch, capsys, cli.repo, path, statements)
    reads = [s for s in statements if s.lstrip().upper().startswith("SELECT") and ('FROM "nodes"' in s or 'FROM "subagent_heads"' in s)]
    assert len(reads) == 2
    assert all('WHERE "session_id" = ' in s for s in reads), reads
    assert all("'s2'" not in s for s in reads)
