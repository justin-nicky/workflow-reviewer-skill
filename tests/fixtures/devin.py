"""Synthetic Devin databases: the real table layout, placeholder content only.

`DevinDb(path)` creates sessions.db with sessions, message_nodes, subagent_heads, refinery_schema_history and a
message_search table that must never be read. `Chain` appends nodes to one session in order.
"""
import itertools
import sqlite3
import time

SECRET = "SECRET-IN-SEARCH-TABLE"
_ids = itertools.count(1)


def _mid():
    return f"m{next(_ids):05d}"


def _ext(**extra):
    return {k.replace("__", "/"): v for k, v in extra.items()}


def system(text="placeholder system text", **ext):
    return {"message_id": _mid(), "role": "system", "content": text,
            "metadata": {"is_user_input": None, "metrics": None, "finish_reason": None, "extensions": _ext(**ext)}}


def skills_loaded(names, hidden=(), descriptions=None):
    """A skills-loaded listing: model-invocable skills, and some that are hidden from the model."""
    descriptions = descriptions or {}
    listing = [{"name": n, "description": descriptions.get(n, "d"), "path": f"~/proj/.cognition/skills/{n}/SKILL.md", "model_invocable": True,
                "provider": "devin"} for n in names]
    listing += [{"name": n, "description": descriptions.get(n, "d"), "path": f"~/proj/.cognition/skills/{n}/SKILL.md", "model_invocable": False,
                 "provider": "devin"} for n in hidden]
    return system("", **{"agent-ext__skills-loaded": {"skills": listing, "is_initial_listing": True}})


def rules_loaded(paths, content_bytes=100):
    return system("", **{"agent-ext__rules-loaded": {"rule_paths": list(paths), "available_rule_paths": [],
                                                      "content_bytes": content_bytes, "is_always_on": True}})


def user(text="placeholder prompt", slash=None):
    ext = {"chisel/client-message-id": "c"}
    if slash:
        ext["chisel/acp-content-blocks"] = [{"type": "text", "text": slash}]
    return {"message_id": _mid(), "role": "user", "content": text,
            "metadata": {"is_user_input": True, "metrics": None, "finish_reason": None, "extensions": ext}}


def metrics(fresh, cache_read, out, cache_write=None, context=None):
    return {"input_tokens": fresh, "output_tokens": out, "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_write, "context_input_tokens": fresh + cache_read if context is None else context}


def call(call_id, name, kind, raw):
    return {"id": call_id, "name": name, "arguments": dict(raw), "kind": "function", "_kind": kind, "_raw": raw}


def assistant(text="placeholder reply", calls=(), tokens=None, finish="stop", message_id=None):
    tool_calls = [{"id": c["id"], "name": c["name"], "arguments": c["arguments"], "index": k, "kind": "function"}
                  for k, c in enumerate(calls)]
    ext = {}
    if calls:
        ext["chisel/tool_call_content"] = {c["id"]: {"toolCallId": c["id"], "title": "t", "status": "pending",
                                                      "kind": c["_kind"], "rawInput": c["_raw"]} for c in calls}
    msg = {"message_id": message_id or _mid(), "role": "assistant", "content": text,
           "metadata": {"is_user_input": None, "metrics": tokens, "finish_reason": finish, "extensions": ext}}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def tool_result(call_id, text="ok", kind="read", exit_code=None, success=True, timing=None, **ext):
    extensions = {"chisel/tool_result_meta": {"success": success, "kind": kind}}
    if exit_code is not None:
        extensions["chisel/terminal_output"] = {"text": text, "cwd": "~/proj", "exit": {"terminal_id": "t", "exit_code": exit_code}}
    if timing:
        extensions["chisel/tool_call_timing"] = {"started_at": timing[0], "finished_at": timing[1], "duration_ms": timing[2]}
    extensions.update(_ext(**ext))
    return {"message_id": _mid(), "role": "tool", "content": text, "tool_call_id": call_id,
            "metadata": {"is_user_input": None, "metrics": None, "finish_reason": None, "extensions": extensions}}


def interrupted():
    return system("[Response interrupted by user]")


def read(call_id, path="a.py"):
    return call(call_id, "read", "read", {"file_path": path})


def edit(call_id, path="a.py"):
    return call(call_id, "edit", "edit", {"file_path": path, "old_string": "a", "new_string": "b"})


def shell(call_id, command="python3 -m pytest -q"):
    return call(call_id, "exec", "execute", {"command": command})


def skill_call(call_id, name="hello"):
    return call(call_id, "skill", "other", {"skill": name, "command": "not a shell command"})   # the trap: a command field


def search(call_id, query="q"):
    return call(call_id, "find_file_by_name", "search", {"path": ".", "query": query})


def spawn(call_id, task="placeholder task", profile="Explore"):
    return call(call_id, "run_subagent", "other", {"task": task, "profile": profile, "title": "t", "is_background": False})


class DevinDb:
    def __init__(self, path, schema=20):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT NOT NULL, backend_type TEXT NOT NULL,
              model TEXT NOT NULL, agent_mode TEXT NOT NULL, created_at INTEGER NOT NULL, last_activity_at INTEGER NOT NULL,
              title TEXT, main_chain_id INTEGER);
            CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, node_id INTEGER NOT NULL,
              parent_node_id INTEGER, chat_message TEXT NOT NULL, created_at INTEGER NOT NULL, metadata TEXT, UNIQUE(session_id, node_id));
            CREATE TABLE subagent_heads (session_id TEXT NOT NULL, agent_id TEXT NOT NULL, chain_node_id INTEGER NOT NULL,
              updated_at INTEGER NOT NULL, PRIMARY KEY (session_id, agent_id));
            CREATE TABLE refinery_schema_history (version int4 PRIMARY KEY, name VARCHAR(255), applied_on VARCHAR(255), checksum VARCHAR(255));
            CREATE TABLE message_search (session_id TEXT, body TEXT);
        """)
        self.conn.executemany("INSERT INTO refinery_schema_history VALUES (?,?,?,?)",
                              [(v, f"m{v}", "2026-01-01", "0") for v in range(1, schema + 1)])
        self.conn.execute("INSERT INTO message_search VALUES ('x', ?)", (SECRET,))
        self.conn.commit()

    def session(self, sid, cwd, created=None, model="swe-test", last=None):
        created = created if created is not None else int(time.time()) - 7200
        return Chain(self, sid, cwd, created, last if last is not None else created + 600, model)


class Chain:
    def __init__(self, db, sid, cwd, created, last, model):
        self.db, self.sid, self.cwd, self.created, self.last, self.model = db, sid, cwd, created, last, model
        self.next_id, self.head, self.t = 0, None, created
        self.heads = []

    def _insert(self, msg, parent, step=1):
        import json
        nid = self.next_id
        self.next_id += 1
        self.t += step
        self.db.conn.execute("INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) VALUES (?,?,?,?,?)",
                             (self.sid, nid, parent, json.dumps(msg), self.t))
        return nid

    def add(self, *msgs, step=1):
        """Append messages to the main chain."""
        for m in msgs:
            self.head = self._insert(m, self.head, step)
        return self

    def draft(self, *msgs, parent=None):
        """Nodes that are not on any chain (edited-away or abandoned branches)."""
        for m in msgs:
            self._insert(m, parent)
        return self

    def subagent(self, agent_id, *msgs):
        head = None
        for m in msgs:
            head = self._insert(m, head)
        self.heads.append((agent_id, head))
        return self

    def done(self):
        self.db.conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                             (self.sid, self.cwd, "b", self.model, "m", self.created, self.last, None, self.head))
        self.db.conn.executemany("INSERT INTO subagent_heads VALUES (?,?,?,?)",
                                 [(self.sid, a, h, self.last) for a, h in self.heads])
        self.db.conn.commit()
        return self.db
