"""Synthetic transcript builders. Fixtures are always synthetic, never real sessions."""
import json
import zlib
from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_jsonl(path, records, truncate_last=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    text = "\n".join(lines) + "\n"
    if truncate_last:
        text = text.rstrip("\n")[:-15]
    path.write_text(text, encoding="utf-8")
    return path


# ---- Claude Code ----------------------------------------------------------

def cc_user(text, t, uuid=None, session="s1"):
    return {"type": "user", "uuid": uuid or f"u-{t}-{zlib.crc32(text.encode()) % 10**6}",
            "sessionId": session, "timestamp": at(t),
            "message": {"role": "user", "content": text}}


def cc_assistant(msg_id, t, content=None, usage=None, model="claude-sonnet-x", uuid=None):
    message = {"id": msg_id, "role": "assistant", "model": model,
               "content": content or [{"type": "text", "text": "ok"}]}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "uuid": uuid or f"a-{msg_id}-{t}", "timestamp": at(t),
            "message": message}


def usage(inp=0, out=0, read=0, write=0):
    return {"input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": read, "cache_creation_input_tokens": write}


def tool_use(name, tool_input, call_id):
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


def bash(command, call_id):
    return tool_use("Bash", {"command": command}, call_id)


def cc_tool_result(call_id, text, t, is_error=False, uuid=None):
    return {"type": "user", "uuid": uuid or f"r-{call_id}", "timestamp": at(t),
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": text,
                 "is_error": is_error}]}}


def cc_bash_call(msg_id, call_id, command, t, result_text="ok", is_error=False, result_dt=1):
    """An assistant message running one Bash command, plus its tool result."""
    return [
        cc_assistant(msg_id, t, [bash(command, call_id)], usage(1, 1)),
        cc_tool_result(call_id, result_text, t + result_dt, is_error),
    ]


# ---- Codex ----------------------------------------------------------------

def cx_line(kind, payload, t):
    return {"timestamp": at(t), "type": kind, "payload": payload}


def cx_meta(session_id, cwd, t=0):
    return cx_line("session_meta", {"id": session_id, "cwd": str(cwd)}, t)


def cx_user(text, t):
    """A real user turn: Codex records it as an event_msg user_message."""
    return cx_line("event_msg", {"type": "user_message", "message": text}, t)


def cx_injected_user(text, t):
    """Context Codex injects as a user-role response item; not a user turn."""
    return cx_line("response_item", {"type": "message", "role": "user",
                                     "content": [{"type": "input_text", "text": text}]}, t)


def cx_developer(text, t):
    return cx_line("response_item", {"type": "message", "role": "developer",
                                     "content": [{"type": "input_text", "text": text}]}, t)


def cx_call(command, call_id, t):
    args = json.dumps({"cmd": command, "workdir": "/repo", "max_output_tokens": 1000})
    return cx_line("response_item", {"type": "function_call", "name": "exec_command",
                                     "arguments": args, "call_id": call_id}, t)


def cx_output(call_id, text, exit_code, t):
    out = (f"Chunk ID: ab12cd\nWall time: 1.2 seconds\nProcess exited with code {exit_code}\n"
           f"Original token count: 5\nOutput:\n{text}")
    return cx_line("response_item", {"type": "function_call_output", "call_id": call_id, "output": out}, t)


def cx_tokens(inp, cached, out, t, total=None):
    usage = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}
    info = {"last_token_usage": usage, "total_token_usage": total or usage}
    return cx_line("event_msg", {"type": "token_count", "info": info}, t)


# ---- Cursor ---------------------------------------------------------------

def cu_user(text):
    return {"role": "user", "message": {"content": [{"type": "text", "text": text}]}}


def cu_assistant(text=None, calls=()):
    content = [{"type": "text", "text": text}] if text else []
    content += [{"type": "tool_use", "name": n, "input": i} for n, i in calls]
    return {"role": "assistant", "message": {"content": content}}


def cu_turn_ended():
    return {"type": "turn_ended", "status": "success"}
