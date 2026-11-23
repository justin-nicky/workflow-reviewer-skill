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
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# ---- Claude Code ----------------------------------------------------------

def cc_user(text, t, uuid=None, session="s1", **extra):
    rec = {"type": "user", "uuid": uuid or f"u-{t}-{zlib.crc32(text.encode()) % 10**6}",
           "sessionId": session, "timestamp": at(t),
           "message": {"role": "user", "content": text}}
    rec.update(extra)          # e.g. isMeta=True, isCompactSummary=True
    return rec


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


def cx_meta(session_id, cwd, t=0, cli_version="0.142.2"):
    return cx_line("session_meta", {"id": session_id, "cwd": str(cwd), "cli_version": cli_version}, t)


def cx_aborted(reason, t):
    return cx_line("event_msg", {"type": "turn_aborted", "reason": reason, "turn_id": "t1",
                                 "duration_ms": 1200, "completed_at": 1}, t)


def cx_event(kind, t):
    return cx_line("event_msg", {"type": kind}, t)


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


def cu_spawn(prompt, description="explore", tool="Task"):
    """A Cursor subagent spawn: the parent's CallDynamicTool call, naming the tool and carrying its arguments."""
    return ("CallDynamicTool", {"namespace": "builtin", "toolName": tool,
                                "arguments": {"description": description, "model": "m", "subagent_type": "generalPurpose",
                                              "run_in_background": False, "prompt": prompt}})


# ---- Codex Desktop (rollout shape of 0.162.x) ------------------------------------------------
# Every record carries an `ordinal`; user prompts are response_item messages; commands are JavaScript.

DX_VERSION = "0.162.0-alpha.17.2"


def desktop(records):
    """Number the records the way Codex Desktop does (ordinal = position) unless they already have one."""
    return [{**r, "ordinal": r.get("ordinal", n)} if isinstance(r, dict) else r for n, r in enumerate(records)]


def dx_meta(session_id, cwd, t=0, version=DX_VERSION, parent=None, forked_from=None, history_start=None,
            agent_path=None):
    payload = {"id": session_id, "cwd": str(cwd), "cli_version": version, "originator": "Codex Desktop",
               "source": "vscode", "thread_source": "user"}
    if parent:
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}}
        payload["thread_source"] = "subagent"
        payload["parent_thread_id"] = parent
    if agent_path:
        payload["agent_path"] = agent_path
    if forked_from:
        payload["forked_from_id"] = forked_from
    if history_start is not None:
        payload["subagent_history_start_ordinal"] = history_start
    return cx_line("session_meta", payload, t)


def dx_task_started(turn_id, t):
    return cx_line("event_msg", {"type": "task_started", "turn_id": turn_id, "root_turn_id": turn_id}, t)


def dx_task_complete(turn_id, t):
    return cx_line("event_msg", {"type": "task_complete", "turn_id": turn_id}, t)


def dx_user(text, t):
    return cx_injected_user(text, t)


def dx_assistant(text, t, phase="final_answer"):
    return cx_line("response_item", {"type": "message", "role": "assistant", "phase": phase,
                                     "content": [{"type": "output_text", "text": text}]}, t)


def dx_agent_message(text, t, author="/root", recipient="/root/worker"):
    return cx_line("response_item", {"type": "agent_message", "author": author, "recipient": recipient,
                                     "content": [{"type": "input_text", "text": text}]}, t)


def dx_exec_js(cmd, extra=""):
    """The JavaScript Codex Desktop sends to its `exec` tool for one shell command."""
    return ("const r = await tools.exec_command({cmd:%s,workdir:\"/repo\",max_output_tokens:2000});\n"
            "text(r.output);\n%s" % (json.dumps(cmd), extra))


def dx_exec(js, call_id, t):
    return cx_line("response_item", {"type": "custom_tool_call", "name": "exec", "input": js,
                                     "call_id": call_id, "status": "completed"}, t)


def dx_exec_cmd(cmd, call_id, t, report_exit=True):
    extra = "text(`exit_code=${r.exit_code}`);\n" if report_exit else ""
    return dx_exec(dx_exec_js(cmd, extra), call_id, t)


def dx_exec_output(call_id, t, text="", exit_code=None):
    parts = [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
             {"type": "input_text", "text": text}]
    if exit_code is not None:
        parts.append({"type": "input_text", "text": f"exit_code={exit_code}"})
    return cx_line("response_item", {"type": "custom_tool_call_output", "call_id": call_id, "output": parts}, t)


def dx_usage(response_id, inp, cached, out, t):
    usage = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
             "cache_write_input_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": inp + out}
    return cx_line("token_usage_record", {"response_id": response_id, "usage": usage,
                                          "turn_token_usage": usage, "thread_token_usage": usage}, t)


def dx_spawn(call_id, t, task_name="/root/worker", message="do the thing"):
    args = json.dumps({"task_name": task_name, "fork_turns": "all", "message": message})
    return cx_line("response_item", {"type": "function_call", "name": "spawn_agent", "namespace": "collaboration",
                                     "arguments": args, "call_id": call_id}, t)


def dx_world_state(t):
    return cx_line("world_state", {"full": True, "state": {"model": "m"}}, t)


def dx_turn_context(t, model=None):
    payload = {"turn_id": "t1", "cwd": "/repo"}
    if model:
        payload["model"] = model
    return cx_line("turn_context", payload, t)


def dx_item_completed(t):
    return cx_line("event_msg", {"type": "item_completed", "thread_id": "x", "item": {"type": "Plan"}}, t)


def dx_settings(t):
    return cx_line("event_msg", {"type": "thread_settings_applied", "thread_id": "x"}, t)


def dx_inter_agent(t):
    return cx_line("inter_agent_communication_metadata", {"trigger_turn": True}, t)


def dx_aborted(t, reason="interrupted"):
    return cx_line("event_msg", {"type": "turn_aborted", "turn_id": "t1", "reason": reason}, t)


def cu_turn_error(error):
    """A turn that ended in error; user_stopped_generation is the user pressing stop."""
    return {"type": "turn_ended", "status": "error", "error": error}
