"""Synthetic Cline sessions: a folder with <id>.json (metadata) and <id>.messages.json (the conversation). Placeholder content only."""
import json
import sqlite3
from pathlib import Path

T0 = 1_780_000_000_000          # ms


def user_input(text, i, t):
    return {"id": f"user-{i:04d}", "role": "user", "content": [{"type": "text", "text": f'<user_input mode="act">{text}</user_input>'}], "ts": T0 + t * 1000}


def other_user_text(text, i, t):
    return {"id": f"user-{i:04d}", "role": "user", "content": [{"type": "text", "text": text}], "ts": T0 + t * 1000}


def tool_use(name, inp, call_id):
    return {"type": "tool_use", "id": call_id, "name": name, "input": inp}


def assistant(i, t, tool_uses=(), text="ok", usage=(100, 10, 60, 0), model="test-model"):
    inp, out, read, write = usage
    return {"id": f"msg_{i:04d}", "role": "assistant", "ts": T0 + t * 1000,
            "content": [{"type": "thinking", "thinking": "t"}, *([{"type": "text", "text": text}] if text else []), *tool_uses],
            "metrics": {"inputTokens": inp, "outputTokens": out, "cacheReadTokens": read, "cacheWriteTokens": write},
            "modelInfo": {"id": model, "provider": "test"}}


def tool_result(call_id, name, results=None, text=None, i=0, t=0):
    content = text if text is not None else [{"query": q, "result": r, "success": ok} for q, r, ok in results]
    return {"id": f"user-{i:04d}", "role": "user", "ts": T0 + t * 1000,
            "content": [{"type": "tool_result", "tool_use_id": call_id, "name": name, "content": content}]}


def ok_command(cmd="python3 -m pytest -q", out="1 passed"):
    return (cmd, out, True)


def failed_command(cmd="python3 -c 'import sys; sys.exit(2)'"):
    return (cmd, "[Command exited with code 2]", False)


def totals(messages):
    sums = [m["metrics"] for m in messages if m.get("metrics")]
    s = {k: sum(x[k] for x in sums) for k in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")}
    return s


def write_session(root, sid, cwd, messages, version="4.1.26", usage_override=None):
    folder = Path(root) / sid
    folder.mkdir(parents=True, exist_ok=True)
    s = totals(messages)
    usage = usage_override or {"inputTokens": s["inputTokens"], "outputTokens": s["outputTokens"], "cacheReadTokens": s["cacheReadTokens"],
                               "cacheWriteTokens": s["cacheWriteTokens"], "totalCost": 0}
    meta = {"session_id": sid, "source": "vscode", "status": "idle", "started_at": "2026-11-01T10:00:00.000Z", "cwd": str(cwd),
            "workspace_root": str(cwd), "model": "test-model", "provider": "test", "prompt": "placeholder",
            "metadata": {"usage": usage, "tokensIn": usage["inputTokens"] - usage["cacheReadTokens"], "tokensOut": usage["outputTokens"],
                         "modelId": "test-model", "sessionHistoryOrigin": {"mode": "user", "version": version}}}
    (folder / f"{sid}.json").write_text(json.dumps(meta))
    (folder / f"{sid}.messages.json").write_text(json.dumps({"version": 1, "sessionId": sid, "messages": messages}))
    return folder


def write_db(path, rows):
    """rows: (session_id, parent_session_id, is_subagent)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (session_id TEXT, parent_session_id TEXT, is_subagent INTEGER, cwd TEXT)")
    conn.executemany("INSERT INTO sessions (session_id, parent_session_id, is_subagent) VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


def normal_messages():
    return [user_input("run the tests", 1, 0),
            assistant(1, 2, [tool_use("run_commands", {"commands": ["python3 -m pytest -q"]}, "call-a")]),
            tool_result("call-a", "run_commands", [ok_command()], i=2, t=3),
            assistant(3, 4, text="done"),
            user_input("thanks", 4, 10),
            assistant(5, 12, text="welcome")]
