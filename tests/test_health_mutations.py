"""Small mutations of each host's golden replica: the right health row must flip, unrelated core rows must stay ok.

The mutations are made here, in memory, and never stored as files.
"""
import copy
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import ROOT

GOLDEN = ROOT / "tests" / "fixtures" / "golden"
CORE = ("turns", "tool_calls", "tokens")


# ---- jsonl mutations: each takes the parsed records of one file and returns the mutated records ------------------------------

def bump(records, match, change):
    out = []
    for r in records:
        r = copy.deepcopy(r)
        if match(r):
            change(r)
        out.append(r)
    return out


def rename(d, old, new):
    d[new] = d.pop(old)


def first_index(records, match):
    return next(i for i, r in enumerate(records) if match(r))


def duplicate(records, match, change):
    i = first_index(records, match)
    twin = copy.deepcopy(records[i])
    change(twin)
    return records[:i + 1] + [twin] + records[i + 1:]


def claude_prompt(r):
    return r.get("type") == "user" and isinstance(r.get("message", {}).get("content"), str)


def claude_result(r):
    c = r.get("message", {}).get("content") if r.get("type") == "user" else None
    return isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def claude_tool_assistant(r):
    c = r.get("message", {}).get("content") if r.get("type") == "assistant" else None
    return isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in c)


def codex_type(*kinds):
    return lambda r: r.get("payload", {}).get("type") in kinds


def cursor_assistant_with_tool(r):
    c = r.get("message", {}).get("content")
    return r.get("role") == "assistant" and isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in c)


def twin_uuid(r):
    r["uuid"] = r["uuid"] + "-twin"


def twin_ordinal(r):
    r["ordinal"] = r["ordinal"] + 100000


def padded(records, known):
    """Add enough records of a known type that coverage stays at 90% or more, whatever was renamed."""
    return records + [dict(known, **({"ordinal": 900000 + i} if "ordinal" in known else {})) for i in range(10 * len(records))]


def harmless(records, known):
    """Add unseen types no metric is hinted at, enough to bring coverage to about 80%."""
    return records + [dict(known, ordinal=800000 + i) if "ordinal" in known else dict(known) for i in range(len(records) // 4 + 1)]


MUTATIONS = {
    "claude-code": {
        "turn marker": lambda rs: bump(rs, claude_prompt, lambda r: rename(r["message"], "content", "body")),
        "result records": lambda rs: [r for r in rs if not claude_result(r)],
        "usage field": lambda rs: bump(rs, lambda r: r.get("type") == "assistant", lambda r: rename(r["message"], "usage", "usage_x")),
        "unknown type": lambda rs: rs + [{"type": "tool_result_v2", "sessionId": "s"}],
        "duplicate assistant": lambda rs: duplicate(rs, claude_tool_assistant, twin_uuid),
        "assistant renamed": lambda rs: padded(bump(rs, lambda r: r.get("type") == "assistant", lambda r: r.update(type="response")), {"type": "last-prompt", "sessionId": "s"}),
        "usage carrier renamed": None,
        "harmless types": lambda rs: harmless(rs, {"type": "zzz_novel"}),
    },
    "codex": {
        # the marker and the user prompts it falls back to both stop being readable
        "turn marker": lambda rs: bump(rs, lambda r: codex_type("task_started")(r) or (r.get("type") == "response_item" and r["payload"].get("role") == "user"),
                                       lambda r: r["payload"].update(type="task_began") if r["payload"].get("type") == "task_started" else rename(r["payload"], "content", "body")),
        "result records": lambda rs: [r for r in rs if not codex_type("function_call_output", "custom_tool_call_output")(r)],
        "usage field": lambda rs: bump(rs, lambda r: r.get("type") == "token_usage_record", lambda r: rename(r["payload"]["usage"], "output_tokens", "output_tokens_x")),
        "unknown type": lambda rs: rs + [{"type": "tool_event", "ordinal": 99999}],
        "duplicate assistant": lambda rs: duplicate(rs, codex_type("function_call", "custom_tool_call"), twin_ordinal),
        "assistant renamed": None,                    # Codex keeps its calls and usage in records of their own
        "usage carrier renamed": lambda rs: padded(bump(rs, lambda r: r.get("type") in ("token_usage_record",) or r.get("payload", {}).get("type") == "token_count",
                                                       lambda r: r.update(type="ledger_row") if r.get("type") == "token_usage_record" else r["payload"].update(type="ledger_total")),
                                                  {"type": "world_state"}),
        "harmless types": lambda rs: harmless(rs, {"type": "zzz_novel", "ordinal": 0}),
    },
    "cursor": {
        "turn marker": lambda rs: bump(rs, lambda r: r.get("role") == "user", lambda r: rename(r, "message", "msg")),
        "result records": None,                       # Cursor records no tool results
        "usage field": None,                          # Cursor records no token usage
        "unknown type": lambda rs: rs + [{"type": "tool_event"}],
        "duplicate assistant": lambda rs: duplicate(rs, cursor_assistant_with_tool, lambda r: None),
        "assistant renamed": None,                    # Cursor records neither results nor usage to prove the replies existed
        "usage carrier renamed": None,
        "harmless types": lambda rs: harmless(rs, {"type": "zzz_novel"}),
    },
}


def files_of(host, tmp_path):
    """Copy the golden into tmp; return (paths to mutate, paths to pass to the script)."""
    if host == "claude-code":
        src = next(iter(sorted((GOLDEN / "claude-code").glob("*.jsonl"))))
        shutil.copytree(GOLDEN / "claude-code", tmp_path / "g")
        main = tmp_path / "g" / src.name
        return [main, *sorted((tmp_path / "g").glob("*/subagents/*.jsonl"))], [main.with_suffix("")]
    if host == "codex":
        shutil.copytree(GOLDEN / "codex-desktop", tmp_path / "g")
        files = sorted((tmp_path / "g").glob("rollout-*.jsonl"))
        return files, files
    shutil.copytree(GOLDEN / "cursor-chat1", tmp_path / "g")
    folder = next(iter(sorted((tmp_path / "g").glob("*/"))))
    return [folder / f"{folder.name}.jsonl"], [folder]


def read(path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").split("\n") if ln.strip()]


def write(path, records):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def health(cli, host, tmp_path, mutation=None):
    to_mutate, args = files_of(host, tmp_path)
    if mutation:
        for f in to_mutate:
            write(f, MUTATIONS[host][mutation](read(f)))
    code, out = cli.json(*args, "--host", "codex" if host == "codex" else host, "--health-all")
    return code, {r["metric"]: r["status"] for r in out["coverage"]["health"]["rows"]}, out


# ---- devin: the golden is a database -----------------------------------------------------------------------------------------

def devin_health(cli, tmp_path, mutate=None):
    db = tmp_path / "sessions.db"
    shutil.copy(GOLDEN / "devin-subagent" / "sessions.db", db)
    if mutate:
        conn = sqlite3.connect(db)
        mutate(conn)
        conn.commit()
        conn.close()
    code, out = cli.json(db, "--host", "devin", "--session-id", "golden-devin-subagent", "--health-all")
    return code, {r["metric"]: r["status"] for r in out["coverage"]["health"]["rows"]}, out


def edit_messages(conn, change, only=None):
    for rowid, text in conn.execute("SELECT row_id, chat_message FROM message_nodes").fetchall():
        m = json.loads(text)
        if only is None or only(m):
            change(m)
            conn.execute("UPDATE message_nodes SET chat_message = ? WHERE row_id = ?", (json.dumps(m), rowid))


def devin_turn_marker(conn):
    edit_messages(conn, lambda m: m["metadata"].update(is_user_input_x=m["metadata"].pop("is_user_input")), lambda m: m["role"] == "user")


def devin_drop_results(conn):
    """Remove every tool-result node and join its children to its parent, so the chains stay whole."""
    for nid, parent, text in conn.execute("SELECT node_id, parent_node_id, chat_message FROM message_nodes ORDER BY node_id").fetchall():
        if json.loads(text)["role"] != "tool":
            continue
        conn.execute("UPDATE message_nodes SET parent_node_id = ? WHERE parent_node_id = ?", (parent, nid))
        conn.execute("UPDATE sessions SET main_chain_id = ? WHERE main_chain_id = ?", (parent, nid))
        conn.execute("UPDATE subagent_heads SET chain_node_id = ? WHERE chain_node_id = ?", (parent, nid))
        conn.execute("DELETE FROM message_nodes WHERE node_id = ?", (nid,))


def devin_usage_field(conn):
    def change(m):
        if m["metadata"].get("metrics"):
            rename(m["metadata"]["metrics"], "input_tokens", "input_tokens_x")
    edit_messages(conn, change, lambda m: m["role"] == "assistant")


def devin_unknown_type(conn):
    head = conn.execute("SELECT main_chain_id FROM sessions").fetchone()[0]
    nid = conn.execute("SELECT MAX(node_id) FROM message_nodes").fetchone()[0] + 1
    conn.execute("INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) VALUES (?,?,?,?,?)",
                 ("golden-devin-subagent", nid, head, json.dumps({"message_id": "x", "role": "tool_summary", "content": "", "metadata": {}}), 1))
    conn.execute("UPDATE sessions SET main_chain_id = ?", (nid,))


def devin_duplicate_assistant(conn):
    rows = conn.execute("SELECT chat_message FROM message_nodes WHERE session_id = 'golden-devin-subagent'").fetchall()
    head = conn.execute("SELECT main_chain_id FROM sessions").fetchone()[0]
    on_chain = {}
    nid = head
    while nid is not None:
        row = conn.execute("SELECT parent_node_id, chat_message FROM message_nodes WHERE node_id = ?", (nid,)).fetchone()
        on_chain[nid] = row
        nid = row[0]
    text = next(r[1] for r in on_chain.values() if json.loads(r[1])["role"] == "assistant" and json.loads(r[1]).get("tool_calls"))
    new = conn.execute("SELECT MAX(node_id) FROM message_nodes").fetchone()[0] + 1
    conn.execute("INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) VALUES (?,?,?,?,?)",
                 ("golden-devin-subagent", new, head, text, 1))
    conn.execute("UPDATE sessions SET main_chain_id = ?", (new,))


def devin_assistant_renamed(conn):
    edit_messages(conn, lambda m: m.update(role="response"), lambda m: m["role"] == "assistant")
    add_nodes(conn, {"message_id": "p", "role": "system", "content": "x", "metadata": {}}, 400)


def devin_usage_carrier(conn):
    def change(m):
        if m["metadata"].get("metrics") is not None:
            rename(m["metadata"], "metrics", "ledger")
    edit_messages(conn, change, lambda m: m["role"] == "assistant")


def devin_harmless(conn):
    add_nodes(conn, {"message_id": "h", "role": "zzz_novel", "content": "", "metadata": {}}, 12)


def add_nodes(conn, message, count):
    head = conn.execute("SELECT main_chain_id FROM sessions").fetchone()[0]
    nid = conn.execute("SELECT MAX(node_id) FROM message_nodes").fetchone()[0]
    for _ in range(count):
        nid += 1
        conn.execute("INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) VALUES (?,?,?,?,?)",
                     ("golden-devin-subagent", nid, head, json.dumps(message), 1))
        head = nid
    conn.execute("UPDATE sessions SET main_chain_id = ?", (head,))


DEVIN = {"assistant renamed": devin_assistant_renamed, "usage carrier renamed": devin_usage_carrier, "harmless types": devin_harmless,
         "turn marker": devin_turn_marker, "result records": devin_drop_results, "usage field": devin_usage_field,
         "unknown type": devin_unknown_type, "duplicate assistant": devin_duplicate_assistant}


def run_cell(cli, tmp_path, host, mutation):
    if host == "devin":
        return devin_health(cli, tmp_path, DEVIN[mutation])
    return health(cli, host, tmp_path, mutation)


# host, mutation -> (exit code, rows that flip). Everything else must be ok or one_way.
EXPECTED = {
    ("claude-code", "turn marker"): (2, {"turns": "broken"}),
    ("claude-code", "result records"): (2, {"tool_calls": "broken"}),
    ("claude-code", "usage field"): (2, {"tokens": "broken"}),
    ("claude-code", "unknown type"): (2, {"tool_calls": "broken"}),   # a type named like a tool record is evidence against tool_calls
    ("claude-code", "duplicate assistant"): (0, {"tool_calls": "warn"}),
    ("codex", "turn marker"): (2, {"turns": "broken"}),
    ("codex", "result records"): (2, {"tool_calls": "broken"}),
    ("codex", "usage field"): (2, {"tokens": "broken"}),
    ("codex", "unknown type"): (2, {"tool_calls": "broken"}),   # a type named like a tool record is evidence against tool_calls
    ("codex", "duplicate assistant"): (0, {"tool_calls": "warn"}),   # call ids are deduplicated, so the metric stays right
    ("cursor", "turn marker"): (0, {"turns": "warn"}),
    ("cursor", "unknown type"): (2, {"tool_calls": "broken"}),   # a type named like a tool record is evidence against tool_calls
    ("cursor", "duplicate assistant"): (0, {}),          # Cursor records no call ids or results: a duplicated call cannot be seen
    ("devin", "turn marker"): (2, {"turns": "broken"}),
    ("devin", "result records"): (2, {"tool_calls": "broken"}),
    ("devin", "usage field"): (2, {"tokens": "broken"}),
    ("devin", "unknown type"): (2, {"tool_calls": "broken"}),   # a type named like a tool record is evidence against tool_calls
    ("devin", "duplicate assistant"): (0, {"tool_calls": "warn"}),
    # the pause is tied to evidence, not to coverage: padded with known records so coverage stays at 90% or more
    ("claude-code", "assistant renamed"): (2, {"tokens": "broken", "tool_calls": "broken"}),
    ("devin", "assistant renamed"): (2, {"tokens": "broken", "tool_calls": "broken"}),
    ("codex", "usage carrier renamed"): (2, {"tokens": "broken"}),
    ("devin", "usage carrier renamed"): (2, {"tokens": "broken"}),
}
HOSTS = {"claude-code": "claude-code", "codex": "codex-desktop", "cursor": "cursor-chat1", "devin": "devin-subagent"}


@pytest.mark.parametrize("host", HOSTS)
def test_the_golden_itself_is_all_ok_or_one_way(cli, tmp_path, host):
    code, rows, _ = devin_health(cli, tmp_path) if host == "devin" else health(cli, host, tmp_path)
    assert code == 0 and set(rows.values()) <= {"ok", "one_way", "not_measurable"}, rows


@pytest.mark.parametrize("host, mutation", sorted(EXPECTED))
def test_a_mutation_flips_the_right_row_and_leaves_unrelated_core_rows_ok(cli, tmp_path, host, mutation):
    code, rows, _ = run_cell(cli, tmp_path, host, mutation)
    exit_code, flipped = EXPECTED[host, mutation]
    assert {m: s for m, s in rows.items() if s in ("warn", "broken")} == flipped, rows
    assert code == exit_code
    assert all(rows[m] in ("ok", "one_way", "not_measurable") for m in CORE if m not in flipped)


@pytest.mark.parametrize("host, mutation", [("claude-code", "assistant renamed"), ("devin", "assistant renamed"),
                                             ("codex", "usage carrier renamed"), ("devin", "usage carrier renamed")])
def test_the_evidence_rows_break_with_coverage_at_90_percent_or_more(cli, tmp_path, host, mutation):
    code, rows, out = run_cell(cli, tmp_path, host, mutation)
    assert out["coverage"]["drift"]["coverage_pct"] >= 90 and code == 2 and not out["coverage"]["drift"]["ok"]
    names = {c["name"] for r in out["coverage"]["health"]["rows"] for c in r["checks"] if not c["passed"]}
    assert "no token usage found" in names
    if mutation == "assistant renamed":
        assert "results without calls" in names


@pytest.mark.parametrize("host", HOSTS)
def test_harmless_unknown_types_at_80_percent_coverage_only_warn(cli, tmp_path, host):
    code, rows, out = run_cell(cli, tmp_path, host, "harmless types")
    assert code == 0 and out["coverage"]["drift"]["ok"]
    assert 70 <= out["coverage"]["drift"]["coverage_pct"] < 90
    assert "broken" not in rows.values()
    assert all(s == "warn" for m, s in rows.items() if s != "not_measurable")           # every measurable row warns on coverage
    assert any("zzz_novel" in w for w in out["coverage"]["warnings"])


def test_a_session_with_only_ai_title_records_added_is_still_all_ok(cli, tmp_path):
    to_mutate, args = files_of("claude-code", tmp_path)
    for f in to_mutate[:1]:
        write(f, read(f) + [{"type": "ai-title", "aiTitle": "t", "sessionId": "s"} for _ in range(9)])
    code, out = cli.json(*args, "--host", "claude-code", "--health-all")
    assert code == 0 and out["coverage"]["drift"]["coverage_pct"] == 100.0
    assert {r["status"] for r in out["coverage"]["health"]["rows"]} <= {"ok", "one_way", "not_measurable"}


def test_the_cells_a_host_cannot_show_are_marked_not_applicable():
    assert MUTATIONS["cursor"]["result records"] is None and MUTATIONS["cursor"]["usage field"] is None


def test_every_expected_cell_exists():
    for (host, mutation) in EXPECTED:
        assert host == "devin" or MUTATIONS[host][mutation] is not None
        assert host != "devin" or mutation in DEVIN
