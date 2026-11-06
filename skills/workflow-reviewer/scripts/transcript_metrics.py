#!/usr/bin/env python3
"""Deterministic, read-only metrics for one coding-agent task.

Reads a host transcript (Claude Code, Codex, Cursor), applies a declarative TOML
mapping, and prints a compact redacted summary on stdout. Standard library only.
It never writes files and never touches the network.
"""
import sys

EXIT_PYTHON_VERSION = 5
EXIT_NO_TRANSCRIPT = 6
if sys.version_info < (3, 11):
    sys.stderr.write("workflow-reviewer needs Python 3.11+, found %d.%d\n" % sys.version_info[:2])
    sys.exit(EXIT_PYTHON_VERSION)

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

MAPPING_DIR = Path(__file__).resolve().parent / "mappings"
HOSTS = {"claude-code": "claude_code", "codex": "codex", "cursor": "cursor"}


# ---- mapping engine: dotted paths, [] wildcards, [k=v] filters, no eval ----------------

def _segments(path):
    for seg in path.split("."):
        name, _, rest = seg.partition("[")
        yield name, (rest[:-1] if rest else None)


def get_all(obj, path):
    cur = [obj]
    for name, bracket in _segments(path):
        nxt = []
        for c in cur:
            if name:
                if isinstance(c, str) and c.lstrip().startswith("{"):
                    try:
                        c = json.loads(c)
                    except ValueError:
                        continue
                if not isinstance(c, dict) or name not in c:
                    continue
                c = c[name]
            if bracket is None:
                nxt.append(c)
            elif isinstance(c, list):
                key, eq, val = bracket.partition("=")
                for e in c:
                    if not eq or (isinstance(e, dict) and str(e.get(key)) == val):
                        nxt.append(e)
        cur = nxt
    return cur


def get_first(obj, path):
    if not path:
        return None
    for v in get_all(obj, path):
        if v is not None:
            return v
    return None


def _flatten_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(t for t in (_flatten_text(v) for v in value) if t)
    if isinstance(value, dict):
        return _flatten_text(value.get("text", ""))
    return ""


def get_text(obj, paths):
    for p in paths if isinstance(paths, list) else [paths]:
        text = "\n".join(t for t in (_flatten_text(v) for v in get_all(obj, p)) if t)
        if text:
            return text
    return ""


def _matches(obj, match):
    for path, want in match.items():
        got = get_first(obj, path)
        wants = want if isinstance(want, list) else [want]
        if got is None or str(got) not in [str(w) for w in wants]:
            return False
    return True


def load_mapping(name=None, text=None):
    if text is None:
        text = (MAPPING_DIR / f"{name}.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


# ---- parsing --------------------------------------------------------------------------

def parse_ts(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 1000 if v > 1e12 else float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else None


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def parse_lines(lines, mapping, actor="main"):
    """Return (events, stats). Each line is classified by the first matching rule."""
    fields = mapping.get("fields", {})
    events, last_total = [], None
    stats = {"lines": 0, "classified": 0, "unseen_types": {}, "truncated_last_line": False, "version": None}
    lines = [ln.strip() for ln in lines if ln.strip()]
    for n, raw in enumerate(lines):
        try:
            obj = json.loads(raw)
        except ValueError:
            if n == len(lines) - 1:
                stats["truncated_last_line"] = True
                continue
            stats["lines"] += 1
            continue
        stats["lines"] += 1
        if stats["version"] is None and fields.get("version"):
            stats["version"] = get_first(obj, fields["version"])
        rule = next((r for r in mapping.get("records", [])
                     if _matches(obj, r.get("match", {}))), None)
        if rule is None:
            t = get_first(obj, mapping.get("type_path", "type"))
            key = str(t) if t is not None else "<missing>"
            stats["unseen_types"][key] = stats["unseen_types"].get(key, 0) + 1
            continue
        stats["classified"] += 1
        if rule["role"] == "ignore":
            continue
        ev = {"actor": actor, "role": rule["role"], "text": get_text(obj, rule["text"]) if "text" in rule else "",
              "msg_id": get_first(obj, rule.get("msg_id", fields.get("msg_id"))),
              "model": get_first(obj, rule.get("model", fields.get("model"))),
              "usage": None, "calls": [], "results": [],
              "ts": parse_ts(get_first(obj, rule.get("timestamp", fields.get("timestamp")))),
              "event_id": get_first(obj, fields.get("event_id"))}
        if "calls" in rule:
            for c in get_all(obj, rule["calls"]):
                inp = get_first(c, rule.get("call_input"))
                if isinstance(inp, str) and inp.lstrip().startswith("{"):
                    try:
                        inp = json.loads(inp)
                    except ValueError:
                        pass
                ev["calls"].append({"name": get_first(c, rule.get("call_name")) or "",
                                    "input": inp,
                                    "id": get_first(c, rule.get("call_id"))})
        if "results" in rule:
            for r in get_all(obj, rule["results"]):
                code = get_first(r, rule.get("result_exit_code"))
                text = get_text(r, rule["result_text"]) if "result_text" in rule else ""
                if code is None and rule.get("result_exit_code_regex"):
                    m = re.search(rule["result_exit_code_regex"], text)
                    code = int(m.group(1)) if m else None
                err = bool(get_first(r, rule.get("result_error"))) or (
                    isinstance(code, (int, float)) and code != 0)
                ev["results"].append({"id": get_first(r, rule.get("result_id")), "error": err,
                                      "bytes": len(text.encode("utf-8"))})
        if "usage_output" in rule or "usage_input" in rule:
            u = {k: _int(get_first(obj, rule.get(f"usage_{k}"))) for k in
                 ("input", "output", "cache_read", "cache_write")}
            if rule.get("usage_input_includes_cache"):
                u["input"] = max(0, u["input"] - u["cache_read"])
            key = rule.get("usage_dedupe_key")
            total = json.dumps(get_first(obj, key), sort_keys=True) if key else None
            if total is not None and total == last_total:
                u = dict.fromkeys(u, 0)               # the same cumulative total re-emitted
            last_total = total if total is not None else last_total
            if any(u.values()):
                ev["usage"] = u
        events.append(ev)
    return events, stats


# ---- analysis -------------------------------------------------------------------------

def token_summary(events):
    by_id, no_id = {}, []
    for ev in events:
        u = ev["usage"]
        if not u:
            continue
        if ev["msg_id"] is None:
            no_id.append(u)
            continue
        cur = by_id.setdefault(ev["msg_id"], dict.fromkeys(u, 0))
        for k, v in u.items():
            cur[k] = max(cur[k], v)
    calls = list(by_id.values()) + no_id
    main = [e["usage"] for e in events if e["usage"] and e["actor"] == "main"]
    last = main[-1] if main else None
    total = {k: sum(c[k] for c in calls) for k in ("input", "output", "cache_read", "cache_write")}
    return {
        "fresh_input": total["input"], "output": total["output"],
        "cache_read": total["cache_read"], "cache_write": total["cache_write"],
        "api_calls": len(calls), "calls_without_id": len(no_id),
        "method": "deduplicated by message id; max per field; records without id counted and flagged",
        "context_at_invocation": (last["input"] + last["cache_read"] + last["cache_write"]) if last else None,
    }


# ---- shell command detection ----------------------------------------------------------

def split_commands(cmd, depth=0):
    """Split a shell string into simple commands (lists of words).

    Handles quotes, $(...) words, heredoc bodies, ; && || | and ( ), and descends
    into `bash -c '...'`. Text that merely mentions a command stays inside one word.
    """
    cmds, words, buf, heredocs = [], [], [], []
    state = {"inword": False}

    def flush_word():
        if state["inword"]:
            words.append("".join(buf))
            buf.clear()
            state["inword"] = False

    def flush_cmd():
        flush_word()
        if words:
            cmds.append(list(words))
            words.clear()

    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c in "'\"":
            j = i + 1
            while j < n and cmd[j] != c:
                j += 2 if (c == '"' and cmd[j] == "\\") else 1
            buf.append(cmd[i + 1:j])
            state["inword"] = True
            i = j + 1
        elif c == "$" and cmd[i + 1:i + 2] == "(":
            level, j = 0, i + 1
            while j < n:
                level += (cmd[j] == "(") - (cmd[j] == ")")
                if level == 0:
                    break
                j += 1
            buf.append(cmd[i:j + 1])
            state["inword"] = True
            i = j + 1
        elif c == "\\":
            buf.append(cmd[i + 1:i + 2])
            state["inword"] = True
            i += 2
        elif c == "<" and cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            m = re.match(r"<<-?\s*(['\"]?)(\w+)\1", cmd[i:])
            if m:
                heredocs.append(m.group(2))
                i += m.end()
            else:
                i += 2
        elif c == "\n":
            flush_cmd()
            i += 1
            for delim in heredocs:
                while i < n:
                    end = cmd.find("\n", i)
                    end = n if end < 0 else end
                    line, i = cmd[i:end], end + 1
                    if line.strip() == delim:
                        break
            heredocs.clear()
        elif c == "&" and (cmd[i - 1:i] == ">" or cmd[i + 1:i + 2] == ">"):
            buf.append(c)
            state["inword"] = True
            i += 1
        elif c in ";&|()":
            flush_cmd()
            i += 1
        elif c.isspace():
            flush_word()
            i += 1
        else:
            buf.append(c)
            state["inword"] = True
            i += 1
    flush_cmd()
    out = []
    for words in cmds:
        out.append(words)
        core = strip_wrappers(words)
        if depth < 3 and core and core[0] in ("bash", "sh", "zsh") and len(core) > 2:
            for k, w in enumerate(core[1:-1], 1):
                if w.startswith("-") and "c" in w[1:]:
                    out += split_commands(core[k + 1], depth + 1)
                    break
    return out


def strip_wrappers(words):
    words = list(words)
    while words:
        w = words[0]
        if re.match(r"^[A-Za-z_]\w*=", w) or w in ("time", "command", "exec", "sudo", "nohup",
                                                     "env", "npx", "bunx"):
            words.pop(0)
        elif w in ("uv", "poetry", "pipenv") and words[1:2] == ["run"]:
            del words[:2]
        elif w == "pnpm" and words[1:2] == ["exec"]:
            del words[:2]
        elif re.match(r"^python[\d.]*$", w) and words[1:2] == ["-m"]:
            del words[:2]
        else:
            break
    return words


VERIFY_KINDS = [
    ("typecheck", r"(mypy|pyright|tsc|ty check)\b|(npm|pnpm|yarn|bun)( run)? (typecheck|type-check)\b|make typecheck\b"),
    ("lint", r"(ruff|eslint|flake8|pylint|golangci-lint|rubocop|biome|stylelint)\b|(npm|pnpm|yarn|bun)( run)? lint\b|make lint\b|cargo clippy\b"),
    ("build", r"(npm|pnpm|yarn|bun)( run)? build\b|make build\b|cargo build\b|go build\b|gradlew? build\b"),
    ("test", r"(pytest|py\.test|jest|vitest|mocha|rspec|phpunit|tox|nox|unittest)\b|(npm|pnpm|yarn|bun)( run)? test\b|go test\b|cargo (test|nextest)\b|dotnet test\b|gradlew? test\b|make (test|tests|check)\b|just test\b|playwright test\b"),
    ("verify", r"make (verify|ci|all)\b|(npm|pnpm|yarn|bun)( run)? (verify|ci|check)\b|just (verify|ci|check)\b"),
]


def verify_kind(words, custom=()):
    core = " ".join(strip_wrappers(words))
    for pat in custom:
        if re.match(pat, core):
            return "verify"
    for kind, pat in VERIFY_KINDS:
        if re.match(pat, core):
            return kind
    return None


def command_text(tool_input):
    if not isinstance(tool_input, dict):
        return ""
    cmd = tool_input.get("command", tool_input.get("cmd", ""))
    if isinstance(cmd, list):
        cmd = cmd[-1] if len(cmd) >= 3 and cmd[1] in ("-c", "-lc") else " ".join(map(str, cmd))
    return cmd if isinstance(cmd, str) else ""


CATEGORIES = {
    "read": {"read", "notebookread", "view", "read_file"},
    "search": {"grep", "glob", "search", "codebase_search", "grep_search", "file_search", "list_dir", "websearch"},
    "edit": {"edit", "write", "strreplace", "multiedit", "notebookedit", "apply_patch", "edit_file", "str_replace_editor", "create_file"},
    "shell": {"bash", "shell", "exec_command", "shell_command", "local_shell", "run_terminal_cmd", "powershell"},
    "agent": {"task", "agent", "spawn_agent"},
    "skill": {"skill"},
}


def categorise(name):
    low = name.lower()
    return next((c for c, names in CATEGORIES.items() if low in names), "other")


def call_path(call):
    inp = call["input"]
    if isinstance(inp, dict):
        for k in ("file_path", "path", "notebook_path"):
            if isinstance(inp.get(k), str):
                return [inp[k]]
        patch = inp.get("input") or inp.get("patch")
        if isinstance(patch, str):
            return re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch, re.M)
    return []


def tool_summary(events, custom_patterns, has_results):
    results = {}
    for ev in events:
        for r in ev["results"]:
            if r["id"] is not None:
                results[r["id"]] = r
    by_cat, reads, edits, failed, kinds = {}, {}, {}, {}, {}
    seq, last_edit, errors = [], -1, 0
    for ev in events:
        for c in ev["calls"]:
            cat = categorise(c["name"])
            by_cat[cat] = by_cat.get(cat, 0) + 1
            paths = call_path(c)
            if cat == "read":
                for p in paths:
                    reads[p] = reads.get(p, 0) + 1
            if cat == "edit":
                last_edit = max(last_edit, ev["i"])
                for p in paths:
                    edits[p] = edits.get(p, 0) + 1
            if cat == "shell":
                cmd = command_text(c["input"])
                kind = next((k for k in (verify_kind(w, custom_patterns) for w in split_commands(cmd)) if k), None)
                if kind:
                    seq.append((ev["i"], kind))
                    kinds[kind] = kinds.get(kind, 0) + 1
                res = results.get(c["id"])
                if res and res["error"]:
                    failed[cmd.strip()] = failed.get(cmd.strip(), 0) + 1
    errors = sum(1 for r in results.values() if r["error"])
    calls_by_id = {c["id"]: c for ev in events for c in ev["calls"] if c["id"] is not None}
    outputs = sorted(({"bytes": r["bytes"], "call_id": k, "tool": calls_by_id.get(k, {}).get("name"),
                       "command": command_text(calls_by_id.get(k, {}).get("input"))[:80] or None}
                      for k, r in results.items()), key=lambda o: -o["bytes"])[:3]
    return {
        "total": sum(by_cat.values()), "by_category": by_cat,
        "errors": errors if has_results else None,
        "repeated_reads": {p: n for p, n in reads.items() if n > 1},
        "edits_per_file": edits,
        "retried_failures": [{"command": c, "times": n} for c, n in failed.items() if n > 1] if has_results else None,
        "largest_outputs": outputs if has_results else None,
    }, {
        "total": len(seq), "by_kind": kinds,
        "before_last_edit": sum(1 for i, _ in seq if i < last_edit),
    }


def tokens_for(events, mapping):
    if not mapping["capabilities"].get("has_tokens"):
        return {"not_measurable": "host records no token usage"}
    return token_summary(events)


def analyse(events, stats, mapping, custom_patterns=()):
    has_results = mapping.get("capabilities", {}).get("has_tool_results", False)
    tools, verify = tool_summary(events, custom_patterns, has_results)
    return {"tokens": tokens_for(events, mapping), "tools": tools, "verify": verify}


# ---- window, time, turns ---------------------------------------------------------------

REVIEWER_RE = re.compile(r"(?:^|[\s>])/workflow-reviewer\b|skills[/\\]workflow-reviewer", re.I)
LAG_SECONDS = 30


def assign_turns(events):
    """Number main-agent user prompts as turns; every other event joins the current turn."""
    turn = 1
    started = False
    for ev in events:
        if ev["actor"] == "main" and ev["role"] == "user" and ev["text"].strip():
            if started:
                turn += 1
            started = True
        ev["turn"] = turn


def reviewer_cut(events):
    for ev in reversed(events):
        if ev["actor"] == "main" and ev["role"] == "user" and REVIEWER_RE.search(ev["text"]):
            return ev["i"]
    return None


def tool_seconds(events):
    """Seconds spent waiting on tools by these events' calls, with overlapping intervals merged."""
    starts, spans = {}, []
    for e in events:
        if e["ts"] is None:
            continue
        for c in e["calls"]:
            if c["id"] is not None:
                starts[c["id"]] = e["ts"]
        spans += [(starts[r["id"]], e["ts"]) for r in e["results"] if r["id"] in starts]
    total, end = 0.0, None
    for lo, hi in sorted(spans):
        if end is None or lo > end:
            total += max(0.0, hi - lo)
            end = hi
        elif hi > end:
            total += hi - end
            end = hi
    return total


def time_summary(events, idle):
    stamped = [e for e in events if e["ts"] is not None]
    if not stamped:
        return {"not_measurable": "host records no timestamps"}
    wall = stamped[-1]["ts"] - stamped[0]["ts"]
    waiting, prev = 0.0, None
    tool = tool_seconds([e for e in stamped if e["actor"] == "main"])
    for e in stamped:
        if e["actor"] == "main" and e["role"] == "user" and e["text"].strip() and prev is not None:
            gap = e["ts"] - prev
            if gap > idle:
                waiting += gap
        prev = e["ts"]
    return {"wall_s": round(wall), "waiting_s": round(waiting), "tool_s": round(tool),
            "active_s": round(max(0.0, wall - waiting - tool)),
            "tool_note": "main agent only, overlapping calls merged; may include permission prompts; "
                         "subagent time is reported per actor"}


def preview(text, n=80):
    return " ".join(text.split())[:n]


def turn_rows(events, mapping, custom):
    rows = []
    for t in sorted({e["turn"] for e in events}):
        evs = [e for e in events if e["turn"] == t]
        first = next((e for e in evs if e["actor"] == "main" and e["role"] == "user" and e["text"].strip()), None)
        tools, verify = tool_summary(evs, custom, mapping["capabilities"].get("has_tool_results", False))
        rows.append({"turn": t, "preview": preview(first["text"]) if first else "",
                     "tokens": tokens_for(evs, mapping), "tool_calls": tools["total"],
                     "verify_runs": verify["total"]})
    return rows


def actor_rows(events, mapping, custom):
    rows = []
    for actor in dict.fromkeys(e["actor"] for e in events):
        evs = [e for e in events if e["actor"] == actor]
        tools, verify = tool_summary(evs, custom, mapping["capabilities"].get("has_tool_results", False))
        first = next((e for e in evs if e["role"] == "user" and e["text"].strip()), None)
        rows.append({"actor": actor, "models": sorted({e["model"] for e in evs if e["model"]}),
                     "brief_bytes": len(first["text"].encode()) if first else 0,
                     "tokens": tokens_for(evs, mapping), "tool_calls": tools["total"],
                     "verify_runs": verify["total"],
                     "tool_s": round(tool_seconds(evs)) if mapping["capabilities"].get("has_timestamps") else None})
    return rows


def event_view(ev, limit=200):
    calls = []
    for c in ev["calls"]:
        item = {"name": c["name"], "id": c["id"]}
        cmd = command_text(c["input"])
        if cmd:
            item["command"] = cmd[:limit]
        paths = call_path(c)
        if paths:
            item["paths"] = paths
        calls.append(item)
    return {"i": ev["i"], "actor": ev["actor"], "turn": ev["turn"], "role": ev["role"],
            "ts": iso(ev["ts"]), "text": ev["text"][:limit], "calls": calls, "results": ev["results"]}


# ---- transcript discovery --------------------------------------------------------------

RECENT_SECONDS = 600


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def claude_root():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def codex_root():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def codex_cwd(path):
    try:
        with open(path, encoding="utf-8") as fh:
            meta = json.loads(fh.readline())
        return str(Path(meta["payload"]["cwd"]).resolve())
    except (OSError, ValueError, KeyError, TypeError):
        return None


def host_candidates(host, repo):
    """Transcripts that belong to this repo for one host, newest first."""
    if host == "claude-code":
        found = list((claude_root() / slug(repo)).glob("*.jsonl"))
    elif host == "codex":
        found = [p for p in codex_root().glob("**/rollout-*.jsonl") if codex_cwd(p) == str(repo.resolve())]
    else:
        base = Path.home() / ".cursor" / "projects" / slug(str(repo).lstrip("/\\"))
        found = list(base.glob("agent-transcripts/*/*.jsonl"))
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def find_by_id(host, session_id):
    if host == "claude-code":
        return next(iter(claude_root().glob(f"*/{session_id}.jsonl")), None)
    if host == "codex":
        return next(iter(codex_root().glob(f"**/rollout-*{session_id}.jsonl")), None)
    return None


def first_prompt(path, mapping):
    events, _ = parse_lines(read_lines(path), mapping)
    return next((preview(e["text"]) for e in events if e["role"] == "user" and e["text"].strip()), "")


def discover(args, repo):
    """Return (paths, error_output). error_output is set when the choice is ambiguous."""
    hosts = list(HOSTS) if args.host == "auto" else [args.host]
    ids = [(h, args.session_id) for h in hosts] if args.session_id else []
    if not ids and "codex" in hosts and os.environ.get("CODEX_THREAD_ID"):
        ids = [("codex", os.environ["CODEX_THREAD_ID"])]   # undocumented, may change
    for h, sid in ids:
        hit = find_by_id(h, sid)
        if hit:
            return [hit], None
    cands = [(h, p) for h in hosts for p in host_candidates(h, repo)]
    cands.sort(key=lambda hp: hp[1].stat().st_mtime, reverse=True)
    recent = [c for c in cands if time.time() - c[1].stat().st_mtime < RECENT_SECONDS]
    if len(recent) >= 2:
        return [], {"ambiguous": [{"host": h, "path": str(p), "modified": iso(p.stat().st_mtime),
                                   "preview": first_prompt(p, load_mapping(HOSTS[h]))} for h, p in recent],
                    "note": "Several recent transcripts; ask the user which one, then pass its path."}
    return ([cands[0][1]] if cands else []), None


def expand_subagents(path):
    """Claude Code keeps them in <session>/subagents/, Cursor beside <id>/<id>.jsonl."""
    dirs = [path.with_suffix("") / "subagents"]
    if path.stem == path.parent.name:
        dirs.append(path.parent / "subagents")
    return sorted(f for d in dirs if d.is_dir() for f in d.glob("*.jsonl"))


def spawn_text(call):
    inp = call["input"]
    return " ".join(v for v in inp.values() if isinstance(v, str)) if isinstance(inp, dict) else ""


def place_subagents(events):
    """Without timestamps, put each subagent's events right after the call that spawned it (matched by brief)."""
    out = [e for e in events if e["actor"] == "main"]
    subs = {}
    for e in events:
        if e["actor"] != "main":
            subs.setdefault(e["actor"], []).append(e)
    for evs in subs.values():
        brief = next((e["text"] for e in evs if e["role"] == "user" and e["text"].strip()), "")[:80]
        pos = next((k for k, e in enumerate(out) if e["actor"] == "main" and brief and any(
            categorise(c["name"]) == "agent" and brief in spawn_text(c) for c in e["calls"])), None)
        if pos is None:
            out += evs
            continue
        while pos + 1 < len(out) and out[pos + 1]["actor"] != "main":
            pos += 1
        out[pos + 1:pos + 1] = evs
    return out


def dedupe(events):
    """Drop events already seen in another file (forked sessions, duplicated subagents)."""
    first_file, ordinal, kept = {}, {}, []
    for ev in events:
        key = ev["event_id"]
        if key is None:
            base = json.dumps([ev["role"], ev["text"], [(c["name"], c["input"]) for c in ev["calls"]]],
                              sort_keys=True, default=str)
            n = ordinal[(ev["file"], base)] = ordinal.get((ev["file"], base), -1) + 1
            key = (base, n)
        if first_file.setdefault(key, ev["file"]) != ev["file"]:
            continue
        kept.append(ev)
    return kept


# ---- host detection and drift -----------------------------------------------------------

def classified_ratio(lines, mapping):
    lines = [ln for ln in lines if ln.strip()][:300]
    if not lines:
        return 0.0
    _, st = parse_lines(lines, mapping)
    return st["classified"] / max(1, st["lines"])


def detect_host(lines, forced):
    if forced != "auto":
        return forced
    ratios = [(classified_ratio(lines, load_mapping(HOSTS[h])), -n, h) for n, h in enumerate(HOSTS)]
    return max(ratios)[2]


def drift_report(mapping, stats, events, threshold):
    pct = 100.0 * stats["classified"] / stats["lines"] if stats["lines"] else 100.0
    caps = mapping["capabilities"]
    missing = []
    if caps.get("has_tokens") and any(e["role"] == "assistant" for e in events) \
            and not any(e["usage"] for e in events):
        missing.append("usage")
    if any(not c["name"] for e in events for c in e["calls"]):
        missing.append("tool_name")
    unseen = stats["unseen_types"]
    ok = pct >= threshold and not missing
    parts = [f"{pct:.0f}% coverage"]
    if missing:
        parts.append("missing: " + ", ".join(missing))
    if unseen:
        parts.append("new types: " + ", ".join(unseen))
    if stats.get("version"):
        parts.append(f"transcript version {stats['version']}")
    warning = None if ok else (f"\u26a0 {mapping.get('host')} mapping (tested up to "
                               f"{mapping.get('tested_versions')}): " + "; ".join(parts))
    return {"ok": ok, "coverage_pct": round(pct, 1), "missing_fields": missing,
            "unseen_types": unseen, "warning": warning}


def not_measurable(mapping):
    labels = {"has_tokens": "tokens", "has_timestamps": "time", "has_tool_results": "tool results",
              "has_ids": "message ids"}
    return [f"{labels[k]} ({k}=false)" for k, v in mapping["capabilities"].items()
            if not v and k in labels]


# ---- redaction and output ----------------------------------------------------------------

SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9_\-]{16,}", r"gh[pousr]_[A-Za-z0-9]{20,}", r"xox[baprs]-[A-Za-z0-9\-]{10,}",
    r"AKIA[0-9A-Z]{16}", r"AIza[0-9A-Za-z_\-]{30,}", r"Bearer\s+[A-Za-z0-9._\-]{16,}",
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*",
]
SECRET_KEY = r"\w*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_KEY|ACCESS_KEY)\w*"
KEY_VALUE_RE = re.compile(                      # KEY=value, KEY: value, KEY = 'value'
    rf"(?i)({SECRET_KEY}\s*[=:]\s*\\?[\"']?)('[^']*'|[^\s\"',;&\\{{\[][^\s\"',;&\\]*)")
JSON_KEY_RE = re.compile(rf"(?i)({SECRET_KEY}\\?[\"']\s*:\s*\\?[\"'])([^\"'\\]+)")   # "key": "value"
URL_CREDS_RE = re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(@)", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
HOME_RE = re.compile(r"(?:/Users|/home)/[^/\s\"'\\]+")
TOKEN_RE = re.compile(r"[A-Za-z0-9_+=\-]{24,}")


def _entropy(token):
    counts = {c: token.count(c) for c in set(token)}
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


ID_RE = re.compile(r"^(?:toolu|srvtoolu|call|msg|req|resp|chatcmpl)_", re.I)


def _mask_token(m):
    t = m.group(0)
    if ID_RE.match(t):
        return t
    if re.search(r"\d", t) and re.search(r"[A-Za-z]", t) and _entropy(t) >= 4.0:
        return "[REDACTED]"
    return t


def redact(text):
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~")
    for pat in SECRET_PATTERNS:
        text = re.sub(pat, "[REDACTED]", text)
    text = URL_CREDS_RE.sub(r"\1[REDACTED]\2", text)
    text = KEY_VALUE_RE.sub(r"\1[REDACTED]", text)
    text = JSON_KEY_RE.sub(r"\1[REDACTED]", text)
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = HOME_RE.sub("~", text)
    return TOKEN_RE.sub(_mask_token, text)


def _fmt(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    return str(v)


def render_md(out):
    lines = ["# workflow-reviewer metrics"]
    warning = out.get("coverage", {}).get("drift", {}).get("warning")
    if warning:
        lines.append(warning)
    for key, value in out.items():
        lines.append(f"## {key.replace('_', ' ').capitalize()}")
        if isinstance(value, dict):
            lines += [f"- {k}: {_fmt(v)}" for k, v in value.items()]
        elif isinstance(value, list):
            lines += [f"- {_fmt(v)}" for v in value]
        else:
            lines.append(str(value))
    return "\n".join(lines)


def emit(out, fmt, do_redact):
    text = json.dumps(out, indent=2, ensure_ascii=False) if fmt == "json" else render_md(out)
    print(redact(text) if do_redact else text)


# ---- skills, instruction files, degraded mode ---------------------------------------------

def skill_dirs(host, repo):
    home = Path.home()
    parents = [repo, *list(repo.parents)[:4]]
    if host == "claude-code":
        cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        return [p / ".claude" / "skills" for p in parents] + [cfg / "skills"]
    if host == "codex":
        return ([p / ".agents" / "skills" for p in parents] + [home / ".agents" / "skills",
                home / ".codex" / "skills", Path("/etc/codex/skills")])
    if host == "cursor":
        return ([repo / ".cursor" / "skills", home / ".cursor" / "skills"]
                + [p / ".agents" / "skills" for p in parents] + [home / ".agents" / "skills",
                   repo / ".claude" / "skills", home / ".claude" / "skills", repo / ".codex" / "skills"])
    return [repo / ".kiro" / "skills", home / ".kiro" / "skills"]


def skill_inventory(host, repo):
    seen, inventory = set(), []
    for base in skill_dirs(host, repo):
        for md in sorted(base.glob("*/SKILL.md")):
            try:
                data = md.read_bytes()
            except OSError:
                continue
            key = (md.parent.name, hashlib.sha256(data).hexdigest())
            if key in seen:
                continue
            seen.add(key)
            m = re.search(r"^description:\s*(.*)$", data.decode("utf-8", "replace"), re.M)
            desc = m.group(1).strip().strip("\"'") if m else ""
            inventory.append({"name": md.parent.name, "description_chars": len(desc), "bytes": len(data)})
    return inventory


def instruction_files(repo):
    found = []
    for pattern in ("CLAUDE.md", "AGENTS.md", ".cursor/rules/*", ".kiro/steering/*"):
        for f in sorted(repo.glob(pattern)):
            if f.is_file():
                found.append({"path": f.relative_to(repo).as_posix(), "bytes": f.stat().st_size})
    global_md = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "CLAUDE.md"
    if global_md.is_file():
        found.append({"path": "~/.claude/CLAUDE.md", "bytes": global_md.stat().st_size})
    return found


def skills_summary(events, host, repo):
    results = {r["id"]: r for e in events for r in e["results"] if r["id"] is not None}
    loaded = {}
    for ev in events:
        for c in ev["calls"]:
            cat = categorise(c["name"])
            if cat == "skill" and isinstance(c["input"], dict) and c["input"].get("skill"):
                loaded.setdefault(c["input"]["skill"], {"name": c["input"]["skill"],
                                                        "detected_by": "skill-tool", "bytes": None})
            if cat != "read":
                continue
            for path in call_path(c):
                parts = tuple(p for p in re.split(r"[\\/]+", path) if p)
                if "skills" not in parts or parts.index("skills") + 1 >= len(parts) - 1:
                    continue
                name = parts[parts.index("skills") + 1]
                entry = loaded.setdefault(name, {"name": name, "detected_by": "SKILL.md read", "bytes": 0})
                res = results.get(c["id"])
                entry["bytes"] = (entry["bytes"] or 0) + (res["bytes"] if res else 0)
    inventory = skill_inventory(host, repo)
    sizes = {i["name"]: i["bytes"] for i in inventory}
    for entry in loaded.values():
        if entry["bytes"] is None:
            entry["bytes"] = sizes.get(entry["name"])
    return {"loaded": list(loaded.values()), "inventory": inventory,
            "instruction_files": instruction_files(repo)}


def _git(repo, *args):
    try:
        proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def working_tree_report(repo):
    """What the task left in the working tree: same-session work is usually uncommitted."""
    status = _git(repo, "status", "--porcelain")
    if status is None:
        return None
    return {"status": status.splitlines(),
            "unstaged_diff_stat": _git(repo, "diff", "--stat") or "",
            "staged_diff_stat": _git(repo, "diff", "--cached", "--stat") or ""}


def searched_locations(host, repo):
    where = {"claude-code": claude_root() / slug(repo), "codex": codex_root(),
             "cursor": Path.home() / ".cursor" / "projects" / slug(str(repo).lstrip("/\\")) / "agent-transcripts"}
    return [str(where[h]) for h in (HOSTS if host == "auto" else [host]) if h in where]


def degraded_report(host, repo, searched=None):
    report = {"mode": "degraded", "confidence_cap": "medium",
              "evidence_rule": "No usable transcript. The agent's own recollection is never evidence; "
                               "only the working tree, the diff and repo records below count.",
              "git": working_tree_report(repo),
              "skills": {"inventory": skill_inventory(host if host != "auto" else "claude-code", repo),
                         "instruction_files": instruction_files(repo)}}
    if searched is not None:
        report.update(transcript_found=False, searched=searched)
    return report


def load_config(repo):
    try:
        return tomllib.loads((repo / ".workflow-review.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---- cli ------------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="transcript_metrics.py")
    p.add_argument("paths", nargs="*")
    p.add_argument("--host", default="auto", choices=["auto", *HOSTS, "kiro"])
    p.add_argument("--format", default="md", choices=["md", "json"])
    p.add_argument("--verify-pattern", action="append", default=[], dest="verify_patterns")
    p.add_argument("--list-turns", action="store_true")
    p.add_argument("--from-turn", type=int)
    p.add_argument("--to-turn", type=int)
    p.add_argument("--events")
    p.add_argument("--sample", type=int)
    p.add_argument("--session-id")
    p.add_argument("--mapping")
    p.add_argument("--drift-threshold", type=float)
    p.add_argument("--no-redact", action="store_true")
    p.add_argument("--idle-threshold", type=float)
    return p


def read_lines(path):
    return path.read_text(encoding="utf-8").split("\n")      # JSONL: only \n ends a record


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    repo = Path.cwd()
    cfg = load_config(repo)
    args.verify_patterns = list(cfg.get("verify_patterns", [])) + args.verify_patterns
    if args.idle_threshold is None:
        args.idle_threshold = cfg.get("idle_threshold", 120)
    if args.drift_threshold is None:
        args.drift_threshold = cfg.get("drift_threshold", 90)
    redact_on = not args.no_redact
    if args.host == "kiro":
        emit(degraded_report("kiro", repo), args.format, redact_on)
        return 0
    paths = [Path(p) for p in args.paths]
    zst = [str(p) for p in paths if p.suffix == ".zst"]
    if zst:
        emit({"unsupported": zst, "note": "compressed rollouts are not supported; use a live .jsonl"},
             args.format, redact_on)
        return 4
    if not paths:
        paths, ambiguous = discover(args, repo)
        if ambiguous:
            emit(ambiguous, args.format, redact_on)
            return 3
    if not paths:
        emit(degraded_report(args.host, repo, searched_locations(args.host, repo)), args.format, redact_on)
        return EXIT_NO_TRANSCRIPT
    first_lines = read_lines(paths[0])
    host = detect_host(first_lines, args.host)
    if args.mapping:
        text = sys.stdin.read() if args.mapping == "-" else Path(args.mapping).read_text(encoding="utf-8")
        mapping, source = load_mapping(text=text), "override"
        mapping.setdefault("capabilities", {})
    else:
        mapping, source = load_mapping(HOSTS[host]), "builtin"
    events, raw = [], []
    stats = {"lines": 0, "classified": 0, "unseen_types": {}, "truncated_last_line": False, "version": None}
    for path in paths:
        for file in [path, *expand_subagents(path)]:
            actor = "main" if file is path else f"subagent:{file.stem}"
            lines = read_lines(file)
            if file is path:
                raw += lines
            ev, st = parse_lines(lines, mapping, actor)
            for e in ev:
                e["file"] = str(file)
            events += ev
            stats["lines"] += st["lines"]
            stats["classified"] += st["classified"]
            stats["truncated_last_line"] |= st["truncated_last_line"]
            stats["version"] = stats["version"] or st["version"]
            for k, v in st["unseen_types"].items():
                stats["unseen_types"][k] = stats["unseen_types"].get(k, 0) + v
    events = dedupe(events)
    if events and all(e["ts"] is not None for e in events):
        events.sort(key=lambda e: e["ts"])
    else:
        events = place_subagents(events)
    for i, ev in enumerate(events):
        ev["i"] = i
    assign_turns(events)
    cut = reviewer_cut(events)
    if args.events:
        lo, _, hi = args.events.partition(":")
        out = {"events": [event_view(e) for e in events if int(lo) <= e["i"] <= int(hi or lo)]}
    elif args.sample is not None:
        out = {"sample": [ln[:300] for ln in raw if ln.strip()][:args.sample]}
    else:
        window = [e for e in events if cut is None or e["i"] < cut]
        if args.list_turns:
            rows = turn_rows(window, mapping, args.verify_patterns)
            firsts = {e["turn"]: e for e in window if e["actor"] == "main" and e["role"] == "user" and e["text"].strip()}
            out = {"turns": [{"index": r["turn"], "time": iso(firsts[r["turn"]]["ts"]) if r["turn"] in firsts else None,
                              "preview": r["preview"]} for r in rows]}
        else:
            lo = args.from_turn or 1
            window = [e for e in window if e["turn"] >= lo and (args.to_turn is None or e["turn"] <= args.to_turn)]
            out = analyse(window, stats, mapping, args.verify_patterns)
            last_ts = max((e["ts"] for e in events if e["ts"] is not None), default=None)
            lag = None
            if last_ts is not None and datetime.now(timezone.utc).timestamp() - last_ts < LAG_SECONDS:
                lag = "latest turn may be incomplete: last record is under %ds old" % LAG_SECONDS
            drift = drift_report(mapping, stats, window, args.drift_threshold)
            out["coverage"] = {"host": host, "tested_versions": mapping.get("tested_versions"),
                               "mapping_source": source, "drift": drift, "not_measurable": not_measurable(mapping),
                               "capabilities": mapping["capabilities"], "last_record_ts": iso(last_ts),
                               "lag_note": lag, "truncated_last_line": stats["truncated_last_line"]}
            out["window"] = {"reviewer_invocation_found": cut is not None, "events": len(window),
                             "from_turn": lo, "to_turn": args.to_turn}
            out["time"] = time_summary(window, args.idle_threshold) if mapping["capabilities"].get("has_timestamps") \
                else {"not_measurable": "host records no timestamps"}
            out["turns"] = turn_rows(window, mapping, args.verify_patterns)
            out["actors"] = actor_rows(window, mapping, args.verify_patterns)
            out["skills"] = skills_summary(window, host, repo)
    emit(out, args.format, redact_on)
    return 2 if out.get("coverage", {}).get("drift", {}).get("ok") is False else 0


if __name__ == "__main__":
    sys.exit(main())
