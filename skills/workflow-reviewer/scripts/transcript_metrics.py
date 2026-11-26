#!/usr/bin/env python3
"""Deterministic, read-only metrics for one coding-agent task.

Reads a host transcript (Claude Code, Codex, Cursor), applies a declarative TOML
mapping, and prints a compact redacted summary on stdout. Standard library only.
It never writes files and never touches the network.
"""
import sys

EXIT_PYTHON_VERSION = 5
EXIT_NO_TRANSCRIPT = 6
if sys.version_info < (3, 9):
    sys.stderr.write("workflow-reviewer needs Python 3.9+, found %d.%d\n" % sys.version_info[:2])
    sys.exit(EXIT_PYTHON_VERSION)

import argparse
import glob
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode      # builds a string only; the script never opens a URL

try:
    import tomllib
except ImportError:                     # Python 3.9 and 3.10: use the bundled copy of tomli (what tomllib was made from)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "_vendor"))     # python -I drops the script folder
    import tomli as tomllib

MAPPING_DIR = Path(__file__).resolve().parent / "mappings"
HOSTS = {"claude-code": "claude_code", "codex": "codex", "cursor": "cursor"}      # picked by auto-detection
ACTIVE_HOSTS = {}                                  # host registry of this run (built-in and user adapters)
STORE_HOSTS = {"devin": "devin", "cline": "cline"}      # hosts whose sessions are a database or JSON documents: named with --host, found by data presence


# ---- mapping engine: dotted paths, [] wildcards, [k=v] filters, no eval ----------------

def _segments(path):
    for seg in path.split("."):
        name, _, rest = seg.partition("[")
        yield name, (rest[:-1] if rest else None)


def get_all(obj, path):
    """All values at a path. A list of paths lists alternatives: the first one present wins."""
    if isinstance(path, list):
        return next((found for found in (get_all(obj, p) for p in path) if found), [])
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
                    if not eq or (isinstance(e, dict) and _norm(e.get(key)) == val):
                        nxt.append(e)
        cur = nxt
    return cur


def get_first(obj, path):
    if not path:
        return None
    if isinstance(path, list):
        return next((v for v in (get_first(obj, p) for p in path) if v is not None), None)
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
        if want == "$present":                       # the field exists, whatever it holds
            if got is None:
                return False
            continue
        if want == "$absent":                        # the field is not there (or is null)
            if got is not None:
                return False
            continue
        wants = want if isinstance(want, list) else [want]
        if got is None or _norm(got) not in [_norm(w) for w in wants]:
            return False
    return True


def _norm(v):
    return str(v).lower() if isinstance(v, bool) else str(v)


def type_key(obj, type_path):
    """Record type for reporting; type_path may be a list, joined with '/'."""
    paths = type_path if isinstance(type_path, list) else [type_path]
    parts = [str(v) for v in (get_first(obj, p) for p in paths) if v is not None]
    return "/".join(parts) if parts else "<missing>"


SCHEMA_MAX = 1          # the newest mapping schema this script understands


def schema_check(raw):
    """(error, note) for a mapping's schema_version. A missing one counts as 1, with a note."""
    version = raw.get("schema_version")
    if version is None:
        return None, "mapping has no schema_version; treated as 1"
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return f"mapping schema_version must be a whole number from 1 (got {version!r})", None
    if version > SCHEMA_MAX:
        return f"mapping schema {version} is newer than this script supports (max {SCHEMA_MAX})", None
    return None, None


def load_mapping(name=None, text=None):
    if text is None:
        text = (MAPPING_DIR / f"{name}.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


# ---- variants: one mapping file may describe several transcript shapes ------------------------

def resolve_variants(raw):
    """A mapping with [[variants]] becomes one merged mapping per variant; otherwise just itself."""
    variants = raw.get("variants")
    if not variants:
        return [raw]
    base = {k: v for k, v in raw.items() if k != "variants"}
    merged = []
    for v in variants:
        m = dict(base)
        for k, val in v.items():
            m[k] = {**base.get(k, {}), **val} if k in ("fields", "capabilities") and isinstance(val, dict) else val
        m["variant"] = v.get("name")
        merged.append(m)
    return merged


def version_in(version, spec):
    """Is `version` inside a 'low to high' (or open-ended 'low and newer') range string?

    False when either side has no version.
    """
    if str(spec or "").strip().startswith("schema"):          # a database schema number: "schema 18 to 25", "schema 20 and newer"
        nums = [int(n) for n in re.findall(r"\d+", str(spec))]
        if not (str(version).isdigit() and nums):
            return False
        return nums[0] <= int(version) if re.search(r"\band newer\b", str(spec)) else nums[0] <= int(version) <= nums[-1]
    seen = _version_tuple(version) if version else None
    bounds = VERSION_RE.findall(str(spec or ""))
    if not (seen and bounds):
        return False
    if re.search(r"\band newer\b", str(spec)):
        return _version_tuple(bounds[0]) <= seen
    return _version_tuple(bounds[0]) <= seen <= _version_tuple(bounds[-1])


def score_variant(m, lines):
    """How well one variant fits a sample: classified ratio, version seen, and unmet required markers."""
    lines = [ln for ln in lines if ln.strip()][:300]
    _, st = parse_lines(lines, m)
    req = m.get("requires") or {}
    types, fields_seen = set(), set()
    for ln in lines:
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        types.add(type_key(obj, m.get("type_path", "type")))
        fields_seen.update(f for f in req.get("fields", []) if get_first(obj, f) is not None)
    missing = [t for t in req.get("types", []) if t not in types] + \
              [f for f in req.get("fields", []) if f not in fields_seen]
    return {"ratio": st["classified"] / max(1, st["lines"]) if lines else 0.0,
            "version": st["version"], "missing": missing}


def choose_variant(raw, lines, want_score=False, version=None):
    """Pick the variant by shape: best classified ratio among those whose required markers appear.

    The optional `versions` range only breaks ties between equal shapes; listing order breaks the rest.
    Returns (mapping, info) where info has variant, score, and, for several variants, scores and reason.
    """
    variants = resolve_variants(raw)
    if len(variants) == 1:
        score = score_variant(variants[0], lines)["ratio"] if want_score else None
        return variants[0], {"variant": variants[0].get("variant") or "default", "score": score,
                             "scores": None, "reason": None}
    scored = [(m, score_variant(m, lines)) for m in variants]
    for _, sc in scored:
        sc["version"] = sc["version"] or version                  # e.g. the database's own schema number
    qualified = [ms for ms in scored if not ms[1]["missing"]]
    pool = qualified or scored
    top = max(round(sc["ratio"], 6) for _, sc in pool)
    tied = [ms for ms in pool if round(ms[1]["ratio"], 6) == top]
    matching = [ms for ms in tied if version_in(ms[1]["version"], ms[0].get("versions"))]
    chosen = (matching or tied)[0]
    if len(tied) == 1:
        reason = "best classified ratio" if len(qualified) == len(scored) else "required markers met, best classified ratio"
    else:
        reason = "version range tiebreak" if 0 < len(matching) < len(tied) else "listed first"
    return chosen[0], {"variant": chosen[0].get("variant"), "score": chosen[1]["ratio"],
                       "scores": {m.get("variant"): round(100.0 * sc["ratio"], 1) for m, sc in scored},
                       "reason": reason}


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


def js_string(literal):
    """The text of a quoted JavaScript string literal (double or single quotes)."""
    body = literal[1:-1]
    if literal[0] == "'":
        body = body.replace("\\'", "'").replace('"', '\\"')
    try:
        return json.loads('"' + body + '"') if literal[0] == "'" else json.loads(literal)
    except ValueError:
        return body


def commands_from_script(script, pattern):
    """Each command a tool script passes as cmd:"…" (pattern's group 1 is the quoted literal), in order."""
    return [js_string(m.group(1)) for m in re.finditer(pattern, script)]


def prompt_time(text, spec):
    """Epoch seconds from a clock the host writes inside the prompt, or None.

    spec: regex (group 1 the date-time text, optional groups 2 and 3 the UTC offset hours and minutes)
    and the strptime format of group 1.
    """
    m = re.search(spec["regex"], text)
    if not m:
        return None
    try:
        when = datetime.strptime(m.group(1).strip(), spec["format"])
    except ValueError:
        return None
    hours = int(m.group(2)) if len(m.groups()) > 1 and m.group(2) else 0
    minutes = int(m.group(3)) if len(m.groups()) > 2 and m.group(3) else 0
    offset = timedelta(hours=hours, minutes=minutes if hours >= 0 else -minutes)
    return when.replace(tzinfo=timezone(offset)).timestamp()


def skill_names_in(text, block_pattern, item_pattern):
    """Names listed in the block of `text` the first pattern finds (its group 1), or None when there is no block."""
    block = re.search(block_pattern, text)
    return re.findall(item_pattern, block.group(1)) if block else None


def skill_descriptions_in(text, block_pattern, item_pattern):
    """{name: description} for each listing line the item pattern (groups: name, description) finds in the block."""
    if block_pattern:
        block = re.search(block_pattern, text)
        text = block.group(1) if block else ""
    return {name: " ".join(desc.split()) for name, desc in re.findall(item_pattern, text)}


USAGE_FIELDS = ("input", "output", "cache_read", "cache_write")
USAGE_NOUN = {"input": "input token counts", "output": "output token counts", "cache_read": "cache-read token counts",
              "cache_write": "cache-write token counts"}
TOKEN_KEY = {"input": "fresh_input", "output": "output", "cache_read": "cache_read", "cache_write": "cache_write"}
FIELD_OF_TOKEN_KEY = {v: k for k, v in TOKEN_KEY.items()}
QUANTITIES_OF_FIELD = {"input": ("tokens_fresh", "tokens_input_incl_cache", "tokens_all", "context_counted"),
                       "output": ("tokens_output", "tokens_all"),
                       "cache_read": ("tokens_cache_read", "tokens_input_incl_cache", "tokens_all", "context_counted"),
                       "cache_write": ("tokens_cache_write", "tokens_all")}


def explicit_null(obj, path):
    """Does the record hold this dotted path with a null value (the key is written, the value is null)? A missing key, or a
    path with alternatives or selectors, is not an explicit null."""
    if not isinstance(path, str) or any(ch in path for ch in "[]{}|*"):
        return False
    node, keys = obj, path.split(".")
    for key in keys[:-1]:
        if not isinstance(node, dict) or not isinstance(node.get(key), dict):
            return False
        node = node[key]
    return isinstance(node, dict) and keys[-1] in node and node[keys[-1]] is None


def unrecorded_usage_fields(mapping):
    """Usage fields that no usage rule of the mapping reads: the host never records them, so they are null, never 0."""
    rules = [r for r in mapping.get("records", []) if "usage_output" in r or "usage_input" in r]
    return [k for k in USAGE_FIELDS if rules and not any(r.get(f"usage_{k}") for r in rules)]


def usage_carriers(events):
    """The records a usage rule matched. Fallback carriers count only when no primary one exists."""
    carriers = [e for e in events if e.get("usage_declared")]
    if any(not e["usage_fallback"] for e in carriers):
        carriers = [e for e in carriers if not e["usage_fallback"]]
    return carriers


def usage_calls(events):
    """One entry per call: a message id (its records together) or one record without an id. A call has a field if any of its
    records does; it is interrupted if every record of it is (no token count at all, the context included)."""
    calls = {}
    for i, e in enumerate(usage_carriers(events)):
        call = calls.setdefault(("id", e["msg_id"]) if e["msg_id"] is not None else ("record", i), {"fields": {}, "interrupted": True})
        for k, present in e["usage_declared"].items():
            call["fields"][k] = call["fields"].get(k, False) or bool(present)
        call["interrupted"] = call["interrupted"] and bool(e.get("usage_interrupted"))
    return list(calls.values())


def usage_gaps(events):
    """{field: (calls without it, of those the interrupted ones, calls declaring it)} for each declared usage field some call lacks."""
    calls, out = usage_calls(events), {}
    for k in USAGE_FIELDS:
        have = [c for c in calls if k in c["fields"]]
        lacking = [c for c in have if not c["fields"][k]]
        if lacking:
            out[k] = (len(lacking), sum(1 for c in lacking if c["interrupted"]), len(have))
    return out


def partial_usage_fields(events):
    """{field: (calls without it, calls)} for fields some calls have and some lack, interrupted calls left out (they get
    a warning of their own). A field no call has is a missing number instead."""
    return {k: (m - i, n) for k, (m, i, n) in usage_gaps(events).items() if m < n and m - i}


def interrupted_calls(events):
    """(interrupted calls, calls) among the usage carriers."""
    calls = usage_calls(events)
    return sum(1 for c in calls if c["interrupted"]), len(calls)


def missing_usage_fields(events):
    """{field: carrier records} for each usage field a mapping declares that no carrier record in these events has.

    Carriers are the records a usage rule matched. Fallback carriers count only when no primary one exists."""
    carriers = [e for e in events if e.get("usage_declared")]
    if any(not e["usage_fallback"] for e in carriers):
        carriers = [e for e in carriers if not e["usage_fallback"]]
    missing = {}
    for k in USAGE_FIELDS:
        declared = [e for e in carriers if k in e["usage_declared"]]
        if declared and not any(e["usage_declared"][k] for e in declared):
            missing[k] = len(declared)
    return missing


def parse_lines(lines, mapping, actor="main"):
    """Return (events, stats). Each line is classified by the first matching rule."""
    fields = mapping.get("fields", {})
    events, last_total = [], None
    stats = {"lines": 0, "classified": 0, "unseen_types": {}, "ignored_types": {},
             "truncated_last_line": False, "version": None, "skill_list_unparsed": 0}
    prefixes = mapping.get("not_a_turn_prefixes", [])
    null_zero = set(mapping.get("null_means_zero", []))
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
            key = type_key(obj, mapping.get("type_path", "type"))
            stats["unseen_types"][key] = stats["unseen_types"].get(key, 0) + 1
            continue
        stats["classified"] += 1
        if rule["role"] == "ignore":
            key = type_key(obj, mapping.get("type_path", "type"))
            stats["ignored_types"][key] = stats["ignored_types"].get(key, 0) + 1
            continue
        ev = {"actor": actor, "role": rule["role"], "text": get_text(obj, rule["text"]) if "text" in rule else "",
              "msg_id": get_first(obj, rule.get("msg_id", fields.get("msg_id"))),
              "model": get_first(obj, rule.get("model", fields.get("model"))),
              "usage": None, "calls": [], "results": [],
              "ts": parse_ts(get_first(obj, rule.get("timestamp", fields.get("timestamp")))),
              "event_id": get_first(obj, fields.get("event_id")), "non_turn": None,
              "interrupted": bool(rule.get("interrupt")), "turn_start": bool(rule.get("turn_start")),
              "usage_fallback": bool(rule.get("usage_fallback")), "turn_error": bool(rule.get("turn_error")),
              "prompt_ts": prompt_time(get_text(obj, rule["text"]), rule["prompt_time"]) if rule.get("prompt_time") else None,
              "spawned_agent": get_first(obj, rule.get("spawned_agent")),
              "offered": get_first(obj, rule.get("offered_skills")),
              "usage_context": None, "slash_skill": None, "instruction": None, "spawned_model": None, "descriptions": {},
              "line": n}
        if rule.get("offered_desc_item"):                  # descriptions printed in the same listing as the names
            listing_text = get_text(obj, rule.get("offered_desc_from") or rule.get("offered_from", []))
            ev["descriptions"] = skill_descriptions_in(listing_text, None if rule.get("offered_desc_from") else rule.get("offered_block"),
                                                       rule["offered_desc_item"])
        if rule.get("offered_desc_pairs"):                 # a list of {name, description} entries
            cfg = rule["offered_desc_pairs"]
            for item in get_all(obj, cfg["list"]):
                name, desc = get_first(item, cfg["name"]), get_first(item, cfg["description"])
                if isinstance(name, str) and isinstance(desc, str):
                    ev["descriptions"][name] = " ".join(desc.split())
        if rule.get("instruction_items"):                  # loaded instruction files whose content the record carries
            cfg = rule["instruction_items"]
            items = [(get_first(i, cfg["path"]), get_first(i, cfg["content"])) for i in get_all(obj, cfg["list"])]
            items = [(p, c) for p, c in items if isinstance(p, str)]
            ev["instruction"] = {"paths": [p for p, _ in items], "contents": [c if isinstance(c, str) else None for _, c in items],
                                 "bytes": 0, "always_on": True}
        if rule.get("offered_each"):                       # one name per match of a wildcard path
            ev["offered"] = [n for n in get_all(obj, rule["offered_each"]) if isinstance(n, str)]
        if rule.get("spawned_model"):
            ev["spawned_model"] = get_first(obj, rule["spawned_model"])
        if rule.get("skill_command"):                      # a typed /name at the start of the prompt
            cmd = rule["skill_command"]
            found = re.match(cmd["regex"], get_text(obj, cmd["path"]))
            ev["slash_skill"] = found.group(1) if found else None
        if rule.get("instruction_paths"):                  # always-on rule files the host loaded
            ev["instruction"] = {"paths": [p for p in get_all(obj, rule["instruction_paths"]) if isinstance(p, str)],
                                 "bytes": _int(get_first(obj, rule.get("instruction_bytes"))),
                                 "always_on": bool(get_first(obj, rule.get("instruction_always_on")))}
        if rule.get("offered_from"):                       # a skill list written as prose: block, then one name per item
            listing = get_text(obj, rule["offered_from"])
            ev["offered"] = skill_names_in(listing, rule["offered_block"], rule["offered_item"])
            if not ev["offered"] and re.search(rule.get("offered_section", "(?!)"), listing):
                stats["skill_list_unparsed"] += 1       # the message has a skills section, but no names came out
        if ev["role"] == "user":
            if rule.get("turn") is False:
                ev["non_turn"] = rule.get("non_turn_reason", "rule")
            elif rule.get("turn_regex") and not re.search(rule["turn_regex"], ev["text"]):
                ev["non_turn"] = "not user input"             # the person's prompt is wrapped; anything else is injected
            else:
                head = ev["text"].lstrip()
                ev["non_turn"] = next((f"prefix:{p}" for p in prefixes if head.startswith(p)), None)
        if "calls" in rule:
            for c in get_all(obj, rule["calls"]):
                inp = get_first(c, rule.get("call_input"))
                if isinstance(inp, str) and inp.lstrip().startswith("{"):
                    try:
                        inp = json.loads(inp)
                    except ValueError:
                        pass
                name, call_id = get_first(c, rule.get("call_name")) or "", get_first(c, rule.get("call_id"))
                via = rule.get("call_via")
                if via and name == via["tool"] and isinstance(inp, dict) and isinstance(inp.get(via["name"]), str):
                    name, inp = inp[via["name"]], inp.get(via["input"])    # a generic call that carries the real tool and its arguments
                category = None
                if rule.get("call_kind"):         # the kind and raw input sit on the message, keyed by call id
                    at_id = lambda path: path.replace("{id}", str(call_id))
                    raw_in = get_first(obj, at_id(rule["call_raw"])) if rule.get("call_raw") else None
                    inp = raw_in if raw_in is not None else inp
                    kind = get_first(obj, at_id(rule["call_kind"]))
                    category = (mapping.get("call_categories") or {}).get(kind)
                if category is None:               # a host that names its tools plainly needs no kind: the name decides
                    category = (mapping.get("call_name_categories") or {}).get(name.lower())
                cmds = commands_from_script(inp, rule["call_command_regex"]) \
                    if rule.get("call_command_regex") and isinstance(inp, str) else []
                split = inp.get(rule["call_split"]) if rule.get("call_split") and isinstance(inp, dict) else None
                if isinstance(split, list) and split:       # a list of commands in one call: each is its own call
                    for item in split:
                        command = item.get("command") if isinstance(item, dict) else item
                        ev["calls"].append({"name": name, "input": {"command": command}, "id": call_id,
                                            **({"category": category} if category else {})})
                elif cmds:                  # a script that runs shell commands: one call per command, never joined
                    ev["calls"] += [{"name": rule.get("call_command_name", name), "input": {"cmd": cmd}, "id": call_id}
                                    for cmd in cmds]
                else:
                    ev["calls"].append({"name": name, "input": inp, "id": call_id})
                    if category:
                        ev["calls"][-1]["category"] = category
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
                if rule.get("result_started"):         # the host's own start and end of the tool call
                    span = (parse_ts(get_first(r, rule["result_started"])), parse_ts(get_first(r, rule.get("result_finished"))))
                    if None not in span:
                        ev["results"][-1]["span"] = span
        if rule.get("call_id") and "calls" in rule:
            ev["declares_call_id"] = True
        if "usage_output" in rule or "usage_input" in rule:
            raw_usage = {k: get_first(obj, rule[f"usage_{k}"]) for k in USAGE_FIELDS if rule.get(f"usage_{k}")}
            ev["usage_declared"] = {k: v is not None for k, v in raw_usage.items()}
            u = {k: _int(get_first(obj, rule.get(f"usage_{k}"))) for k in
                 ("input", "output", "cache_read", "cache_write")}
            if rule.get("usage_input_includes_cache"):
                u["input"] = max(0, u["input"] - u["cache_read"])
            context = get_first(obj, rule["usage_context"]) if rule.get("usage_context") else None
            nulls = [k for k in raw_usage if raw_usage[k] is None and explicit_null(obj, rule[f"usage_{k}"])]
            if rule.get("usage_context") and context is None and nulls and not any(raw_usage.values()):
                ev["usage_interrupted"] = True      # no token count at all, the context included: an interrupted call
            vouched = [k for k in nulls if TOKEN_KEY[k] in null_zero]
            if vouched and isinstance(context, (int, float)) and not isinstance(context, bool) \
                    and u["input"] + u["cache_read"] == _int(context):
                for k in vouched:                       # the host's null is a 0: the context size adds up with it as 0
                    ev["usage_declared"][k] = True
            key = rule.get("usage_dedupe_key")
            total = json.dumps(get_first(obj, key), sort_keys=True) if key else None
            if total is not None and total == last_total:
                u = dict.fromkeys(u, 0)               # the same cumulative total re-emitted
            last_total = total if total is not None else last_total
            if any(u.values()) or (rule.get("usage_present") and get_first(obj, rule["usage_present"]) is not None):
                ev["usage"] = u                       # usage_present: a call whose metrics are all zero still counts as a call
            if rule.get("usage_context"):
                ev["usage_context"] = get_first(obj, rule["usage_context"])
        events.append(ev)
    if any(e["usage"] and not e["usage_fallback"] for e in events):
        for e in events:                    # primary usage records exist: fallback rows would count twice
            if e["usage_fallback"]:
                e["usage"] = None
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
    contexts = {}
    for ev in events:                     # a second way of counting: input + cache_read must equal the host's context size
        if ev["usage"] and ev.get("usage_context") is not None and ev["msg_id"] is not None:
            contexts[ev["msg_id"]] = (ev["usage"]["input"] + ev["usage"]["cache_read"], _int(ev["usage_context"]))
    main = [e["usage"] for e in events if e["usage"] and e["actor"] == "main"]
    last = main[-1] if main else None
    total = {k: sum(c[k] for c in calls) for k in ("input", "output", "cache_read", "cache_write")}
    check = {"calls": len(contexts), "context_input_total": sum(c for _, c in contexts.values()),
             "mismatches": sum(1 for a, c in contexts.values() if a != c)} if contexts else None
    gaps = [TOKEN_KEY[k] for k, (m, _, n) in usage_gaps(events).items()]
    return {
        **({"context_check": check} if check else {}),
        **({"lower_bound": gaps} if gaps else {}),        # some records lack these fields: the totals are at least this
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
    ("test", r"(pytest|py\.test|jest|vitest|mocha|rspec|phpunit|tox|nox|unittest)\b|(npm|pnpm|yarn|bun)( run)? test\b|go test\b|cargo (test|nextest)\b|dotnet test\b|gradlew? test\b|make (test|tests|check)\b|just test\b|playwright test\b"
              r"|python[\d.]*\s+(?:-\S+\s+)*(?:\S*/)?(?:tests?/\S*|test\w*|\w*_test)\.py\b"       # python3 test_calc.py
              r"|node\s+(?:--test\b|(?:-\S+\s+)*\S*\.(?:test|spec)\.[cm]?[jt]s\b)"),                # node calc.test.js
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


def call_cat(call):
    """The call's category: the mapping's own (from the host's kind) when it gave one, else by tool name."""
    return call.get("category") or categorise(call["name"])


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
            cat = call_cat(c)
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
    repeated, repeated_more = cap_counts({p: n for p, n in reads.items() if n > 1}, TOP_ITEMS)
    edited, edited_more = cap_counts(edits, TOP_ITEMS)
    repeated = {short_path(p): n for p, n in repeated.items()}
    edited = {short_path(p): n for p, n in edited.items()}
    retries = sorted(((c, n) for c, n in failed.items() if n > 1), key=lambda cn: (-cn[1], cn[0]))
    summary = {
        "total": sum(by_cat.values()), "by_category": by_cat,
        "errors": errors if has_results else None,
        "repeated_reads": repeated, "edits_per_file": edited,
        "retried_failures": [{"command": c[:80], "times": n} for c, n in retries[:TOP_FAILURES]] if has_results else None,
        "largest_outputs": outputs if has_results else None,
    }
    if repeated_more:
        summary["repeated_reads_more"] = {"files": repeated_more}
    if edited_more:
        summary["edits_per_file_more"] = {"files": edited_more}
    if len(retries) > TOP_FAILURES:
        summary["retried_failures_more"] = len(retries) - TOP_FAILURES
    return summary, {
        "total": len(seq), "by_kind": kinds,
        "before_last_edit": sum(1 for i, _ in seq if i < last_edit),
    }


def tokens_for(events, mapping):
    if not mapping["capabilities"].get("has_tokens"):
        return {"not_measurable": "host records no token usage"}
    summary = token_summary(events)
    used_fallback = any(e["usage"] and e["usage_fallback"] for e in events)
    method = mapping.get("token_method_fallback") if used_fallback else mapping.get("token_method")
    if method:                                  # the mapping says what its rows really are
        summary["method"] = method
    return summary


def analyse(events, stats, mapping, custom_patterns=()):
    has_results = mapping.get("capabilities", {}).get("has_tool_results", False)
    tools, verify = tool_summary(events, custom_patterns, has_results)
    return {"tokens": tokens_for(events, mapping), "tools": tools, "verify": verify}


# ---- window, time, turns ---------------------------------------------------------------

REVIEWER_RE = re.compile(    # the invocation itself, in each host's own form: a typed /workflow-reviewer or $workflow-reviewer starting
    r"^\s*(?:<user_input[^>]*>\s*)?(?:[/$]workflow-reviewer\b|\[[/$]workflow-reviewer\]\()"   # the prompt (Cline wraps it in <user_input>; Codex 0.171 sends a link),
    r"|^\s*(?:<user_input[^>]*>\s*)?(?:please\s+)?(?:use|run|invoke|start)\s+(?:the\s+)?[/$]?workflow-reviewer\b"   # asking for it by name,
    r"|<command-(?:name|message)>/?workflow-reviewer"                              # Claude Code's command markers,
    r"|<manually_attached_skills>(?:(?!</manually_attached_skills>).)*?\bSkill Name:\s*workflow-reviewer\b",   # Cursor's attached skill
    re.I | re.S)


def is_invocation(ev):
    """The prompt that invokes the skill: its text, or the typed /name the mapping read from it (Devin)."""
    return bool(REVIEWER_RE.search(ev["text"])) or ev.get("slash_skill") == "workflow-reviewer"
LAG_SECONDS = 30
MAX_EVENTS = 100
MAX_INTERRUPTION_TURNS = 50
MAX_SAMPLE = 50


def is_turn_start(ev):
    """A real prompt from the human to the main agent (injected records are not turns)."""
    return ev["actor"] == "main" and ev["role"] == "user" and bool(ev["text"].strip()) and not ev["non_turn"]


def assign_turns(events):
    """Number turns; every other event joins the current turn.

    A host that marks turns itself (a rule with turn_start = true, e.g. Codex task_started) gets
    one turn per marker. Otherwise each main-agent user prompt starts a turn.
    """
    marked = any(ev["actor"] == "main" and ev["turn_start"] for ev in events)
    turn = 1
    started = False
    for ev in events:
        ev["turn_open"] = False
        if (ev["actor"] == "main" and ev["turn_start"]) if marked else is_turn_start(ev):
            if started:
                turn += 1
            started = True
            ev["turn_open"] = True
        ev["turn"] = turn


def mark_repeated_prompts(events):
    """A host that does not store a cancelled reply shows it as two prompts in a row with no assistant record between them.
    The interruption belongs to the first prompt; the second is a new turn."""
    waiting = None
    for ev in events:
        if ev["actor"] != "main":
            continue
        if ev["role"] == "assistant":
            waiting = None
        elif is_turn_start(ev):
            if waiting is not None:
                waiting["interrupted"] = True
            waiting = ev


def reviewer_cut(events):
    """Index of the event that opens the turn holding the latest /workflow-reviewer prompt."""
    for ev in reversed(events):
        if is_turn_start(ev) and is_invocation(ev):
            opener = next((e for e in events if e["turn_open"] and e["turn"] == ev["turn"]), ev)
            return min(opener["i"], ev["i"])
    return None


def default_window(reviewer_turns, turns):
    """The turns a review covers when nobody names them: (from, to, note), to None meaning to the end.

    It ends just before the latest reviewer invocation and starts just after the one before it (or at the
    session start). Reviewer turns are never in it. When nothing but reviewer turns lie between the last two
    invocations (a re-run), it is the window the earlier invocation had."""
    if not reviewer_turns:
        return 1, None, None
    end, rerun_of = reviewer_turns[-1], None
    while True:
        before = [t for t in reviewer_turns if t < end]
        start = (before[-1] + 1) if before else 1
        if any(start <= t < end for t in turns) or not before:
            break
        end, rerun_of = before[-1], rerun_of or reviewer_turns[-1]
    note = None
    if rerun_of is not None:
        note = (f"re-run: no new work between the /workflow-reviewer invocations; the window is the one the invocation in "
                f"turn {end} had (turns {start}\u2013{end - 1}); pass --from-turn and --to-turn to override")
    elif before:
        note = (f"window starts at turn {start}, after the earlier /workflow-reviewer invocation in turn {before[-1]} "
                f"(already audited); pass --from-turn N to override")
    return start, end - 1, note


def tool_seconds(events, spans_only=False):
    """Seconds spent waiting on tools by these events' calls, with overlapping intervals merged.

    A result may carry the host's own start and end (a span); with spans_only those are all that count.
    """
    starts, spans = {}, []
    for e in events:
        spans += [r["span"] for r in e["results"] if r.get("span")]
        if e["ts"] is None or spans_only:
            continue
        for c in e["calls"]:
            if c["id"] is not None:
                starts[c["id"]] = e["ts"]
        spans += [(starts[r["id"]], e["ts"]) for r in e["results"] if r["id"] in starts and not r.get("span")]
    total, end = 0.0, None
    for lo, hi in sorted(spans):
        if end is None or lo > end:
            total += max(0.0, hi - lo)
            end = hi
        elif hi > end:
            total += hi - end
            end = hi
    return total


def interruption_summary(events, mapping):
    """Plain count of user-interruption markers in the window, with the turn each falls in.

    A marker is either a user record starting with one of the mapping's interruption_prefixes,
    or a record matched by a mapping rule with interrupt = true.
    """
    prefixes = tuple(mapping.get("interruption_prefixes", []))
    if not prefixes and not any(r.get("interrupt") for r in mapping.get("records", [])) and not mapping.get("interrupt_on_repeated_prompt"):
        return {"not_measurable": "no interruption marker is known for this host"}
    turns = [e["turn"] for e in events if e["interrupted"]
             or (prefixes and e["role"] == "user" and e["text"].lstrip().startswith(prefixes))]
    return {"count": len(turns), "turns": turns[:MAX_INTERRUPTION_TURNS]}


def turn_error_summary(events, mapping):
    """Turns that ended in an error that was not the user stopping them (None when the host has no such record)."""
    if not any(r.get("turn_error") for r in mapping.get("records", [])):
        return None
    turns = [e["turn"] for e in events if e["turn_error"]]
    return {"count": len(turns), "turns": turns[:MAX_INTERRUPTION_TURNS]}


COARSE_LABEL = "coarse: prompt timestamps only, minute precision; last turn's duration unknown"
COARSE_NEXT_LABEL = "coarse: prompt timestamps only, minute precision; until the next prompt"


def coarse_time_summary(events, later=None):
    """Wall time for hosts that only write a clock inside prompts.

    With a prompt after the window (the next task, or the reviewer's own invocation) the window runs until that
    prompt. Without one it runs from the first to the last prompt, and the last turn's duration is unknown."""
    stamps = [e["prompt_ts"] for e in events if e["actor"] == "main" and e["prompt_ts"] is not None]
    na = {"not_measurable": "host records no timestamps beyond the prompt clock"}
    if later is not None and stamps:
        wall, label = round(later - min(stamps)), COARSE_NEXT_LABEL
    elif len(stamps) >= 2:
        wall, label = round(max(stamps) - min(stamps)), COARSE_LABEL
    else:
        return {"not_measurable": "fewer than two prompts carry a timestamp"}
    wall = max(0, wall)
    # minute precision: each prompt time may be off by up to a minute either way
    return {"wall_s": wall, "wall_range_s": [max(0, wall - 60), wall + 60], "precision": label,
            "tool_s": na, "waiting_s": na}


def tool_timing_recorded(events):
    """False when these events have tool results and none carries the host's own start and end (a span)."""
    results = [r for e in events for r in e["results"]]
    return not results or any(r.get("span") for r in results)


def time_summary(events, idle, spans_only=False):
    stamped = [e for e in events if e["ts"] is not None]
    if not stamped:
        return {"not_measurable": "host records no timestamps"}
    wall = stamped[-1]["ts"] - stamped[0]["ts"]
    waiting, prev = 0.0, None
    tool = tool_seconds([e for e in stamped if e["actor"] == "main"], spans_only)
    for e in stamped:
        if is_turn_start(e) and prev is not None:
            gap = e["ts"] - prev
            if gap > idle:
                waiting += gap
        if not (e["role"] == "user" and e["non_turn"]):      # injected records are not agent activity
            prev = e["ts"]
    if spans_only and not tool_timing_recorded(events):
        # the mapping reads the host's own tool timing, but no result here carries it: tool time is unknown, not 0
        return {"wall_s": round(wall), "waiting_s": round(waiting), "tool_s": None, "active_s": round(max(0.0, wall - waiting)),
                "tool_note": "tool time not recorded: no tool result in the window carries the host's own tool timing; "
                             "active time includes time in tools", "not_recorded": ["tool_s"]}
    return {"wall_s": round(wall), "waiting_s": round(waiting), "tool_s": round(tool),
            "active_s": round(max(0.0, wall - waiting - tool)),
            "tool_note": ("main agent only, from the host's own tool timing, overlapping calls merged; "
                          if spans_only else "main agent only, overlapping calls merged; may include permission prompts; ")
                         + "subagent time is reported per actor"}


def preview(text, n=80):
    return " ".join(text.split())[:n]


# ---- output caps: the audit runs inside the session it audits, so keep output small --------

TOP_ROWS = 5            # per-turn and per-actor rows shown before an "other" aggregate
LIST_TURNS_SHOWN = 30
TOP_ITEMS = 10          # repeated reads, edits per file, skills inventory
TOP_FAILURES = 5
TOP_LOADED_SKILLS = 15
TOP_TYPES = 15


def row_weight(row):
    tokens = row["tokens"]
    if "not_measurable" in tokens:
        return row["tool_calls"]
    return sum(tokens[k] for k in ("fresh_input", "output", "cache_read", "cache_write"))


ROW_TOKEN_KEYS = ("fresh_input", "output", "cache_read", "cache_write", "api_calls")


def row_tokens(events, mapping):
    """Token fields for a per-turn or per-actor row (the method note stays in the top-level summary)."""
    tokens = tokens_for(events, mapping)
    if "not_measurable" in tokens:
        return tokens
    row = {k: tokens[k] for k in ROW_TOKEN_KEYS}
    if "lower_bound" in tokens:
        row["lower_bound"] = tokens["lower_bound"]
    if "context_check" in tokens:
        row["context_check"] = tokens["context_check"]
    return row


def short_path(path, n=60):
    """Mask the home folder first, then shorten, so a cut can never leave a username fragment."""
    path = mask_home(path)
    return path if len(path) <= n else "\u2026" + path[-(n - 1):]


def merge_rows(rows, label):
    merged = {"tool_calls": sum(r["tool_calls"] for r in rows), "verify_runs": sum(r["verify_runs"] for r in rows)}
    if any("not_measurable" in r["tokens"] for r in rows):
        merged["tokens"] = rows[0]["tokens"]
    else:
        merged["tokens"] = {k: sum(r["tokens"][k] for r in rows) for k in ROW_TOKEN_KEYS}
        gaps = [k for k in ROW_TOKEN_KEYS if any(k in r["tokens"].get("lower_bound", []) for r in rows)]
        if gaps:
            merged["tokens"]["lower_bound"] = gaps
    return {**label, **merged}


def cap_rows(rows, n, label_key, count_key, other_label, keep=None):
    """Top n rows by weight (kept in original order) plus one aggregate row for the rest."""
    if len(rows) <= n:
        return rows
    order = sorted(range(len(rows)), key=lambda i: -row_weight(rows[i]))
    chosen = order[:n]
    forced = next((i for i, r in enumerate(rows) if keep and r[label_key] == keep), None)
    if forced is not None and forced not in chosen:
        chosen[-1] = forced
    chosen = sorted(chosen)
    rest = [r for i, r in enumerate(rows) if i not in chosen]
    return [rows[i] for i in chosen] + [merge_rows(rest, {label_key: other_label, count_key: len(rest)})]


def cap_counts(counts, n):
    """Largest n entries of a name -> count dict; returns (shown, how many were left out)."""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(ranked[:n]), max(0, len(ranked) - n)


def with_other(counts, n):
    shown, more = cap_counts(counts, n)
    if more:
        shown[f"(other {more} types)"] = sum(v for k, v in counts.items() if k not in shown)
    return shown


def turn_rows(events, mapping, custom):
    rows = []
    by_turn = {}
    if not any(is_turn_start(e) for e in events):        # no prompt at all: records without a prompt are not a turn
        return rows
    for e in events:
        by_turn.setdefault(e["turn"], []).append(e)
    for t in sorted(by_turn):
        evs = by_turn[t]
        first = next((e for e in evs if is_turn_start(e)), None)
        tools, verify = tool_summary(evs, custom, mapping["capabilities"].get("has_tool_results", False))
        rows.append({"turn": t, "preview": preview(first["text"]) if first else "",
                     "tokens": row_tokens(evs, mapping), "tool_calls": tools["total"],
                     "verify_runs": verify["total"]})
    return rows


def model_forms(names):
    """Names that match once case, spaces, hyphens and underscores are ignored are one model: report its id form
    (lowercase, no spaces), else the first name seen."""
    groups = {}
    for name in names:
        groups.setdefault(re.sub(r"[\s_\-]+", "", name.lower()), []).append(name)
    return {name: next((n for n in group if n == n.lower() and " " not in n), group[0])
            for group in groups.values() for name in group}


def actor_rows(events, mapping, custom, links=None, metas=None):
    rows = []
    links, metas = links or {}, metas or {}
    forms = model_forms([m for a in metas.values() for m in [a.get("model")] if m] + [e["model"] for e in events if e["model"]])
    for actor in dict.fromkeys(e["actor"] for e in events):
        evs = [e for e in events if e["actor"] == actor]
        tools, verify = tool_summary(evs, custom, mapping["capabilities"].get("has_tool_results", False))
        first = next((e for e in evs if e["role"] == "user" and e["text"].strip()), None)
        models = [metas[actor]["model"]] if metas.get(actor, {}).get("model") \
            else sorted({e["model"] for e in evs if e["model"]})
        models = list(dict.fromkeys(forms.get(m, m) for m in models))
        rows.append({"actor": actor, "models": models,
                     "brief_bytes": links.get(actor, {}).get("brief_bytes",
                                                             len(first["text"].encode()) if first else 0),
                     "tokens": row_tokens(evs, mapping), "tool_calls": tools["total"],
                     "verify_runs": verify["total"],
                     "tool_s": round(tool_seconds(evs, mapping.get("tool_time_source") == "result_spans")) if mapping["capabilities"].get("has_timestamps") else None})
        if actor in links:
            rows[-1]["link"] = links[actor]["link"]
            if "match_score" in links[actor]:
                rows[-1].update(match_score=links[actor]["match_score"], runner_up_score=links[actor]["runner_up_score"])
        history = metas.get(actor, {}).get("history")
        if history:
            rows[-1].update(history_from_ordinal=history["from_ordinal"], history_records_skipped=history["skipped"],
                              history_note=history["note"])
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
    text = ev["text"][:limit]
    if isinstance(ev.get("offered"), list):               # a skill listing: say how long, never list the names
        text = f"skill listing ({len(ev['offered'])} names)"
    return {"i": ev["i"], "actor": ev["actor"], "turn": ev["turn"], "role": ev["role"],
            "ts": iso(ev["ts"]), "text": text, "calls": calls, "results": ev["results"]}


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
    elif host == "cursor":
        base = Path.home() / ".cursor" / "projects" / slug(str(repo).lstrip("/\\"))
        found = list(base.glob("agent-transcripts/*/*.jsonl"))
    else:                                              # a user adapter's jsonl host: the files its container.globs name
        found = []
        for pattern in ((ACTIVE_HOSTS.get(host, {}).get("raw", {}).get("container") or {}).get("globs") or []):
            expanded = os.path.expanduser(pattern)
            found += [Path(m) for m in glob.glob(expanded, recursive=True) if Path(m).is_file()]
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


def discover(args, repo, host_list=None, with_host=False):
    """Return (paths, error_output); with_host: (host, paths, error_output). error_output is set when the choice is ambiguous."""
    hosts = host_list if host_list is not None else (list(HOSTS) if args.host == "auto" else [args.host])
    wrap = (lambda h, paths, err: (h, paths, err)) if with_host else (lambda h, paths, err: (paths, err))
    ids = [(h, args.session_id) for h in hosts] if args.session_id else []
    if not ids and "codex" in hosts and os.environ.get("CODEX_THREAD_ID"):
        ids = [("codex", os.environ["CODEX_THREAD_ID"])]   # undocumented, may change
    for h, sid in ids:
        hit = find_by_id(h, sid)
        if hit:
            return wrap(h, [hit], None)
    cands = [(h, p) for h in hosts for p in host_candidates(h, repo)]
    cands.sort(key=lambda hp: hp[1].stat().st_mtime, reverse=True)
    recent = [c for c in cands if time.time() - c[1].stat().st_mtime < RECENT_SECONDS]
    if len(recent) >= 2:
        return wrap(None, [], {"ambiguous": [{"host": h, "path": str(p), "modified": iso(p.stat().st_mtime),
                                              "preview": first_prompt(p, load_mapping(HOSTS[h]))} for h, p in recent],
                               "note": "Several recent transcripts; ask the user which one, then pass its path."})
    return wrap(cands[0][0] if cands else None, [cands[0][1]] if cands else [], None)


def expand_subagents(path):
    """Claude Code keeps them in <session>/subagents/, Cursor beside <id>/<id>.jsonl."""
    dirs = [path.with_suffix("") / "subagents"]
    if path.stem == path.parent.name:
        dirs.append(path.parent / "subagents")
    return sorted(f for d in dirs if d.is_dir() for f in d.glob("*.jsonl"))


def main_transcript(folder):
    """The main transcript of a session folder, or None.

    Cursor keeps <id>/<id>.jsonl inside the folder, Claude Code keeps <id>.jsonl beside
    the <id>/ folder (which holds subagents/), Codex has one rollout-*.jsonl per folder.
    """
    for cand in (folder / f"{folder.name}.jsonl", folder.parent / f"{folder.name}.jsonl"):
        if cand.is_file():
            return cand
    rollouts = sorted(folder.glob("rollout-*.jsonl"))
    return rollouts[0] if len(rollouts) == 1 else None


def is_subagent_file(path, first_line, mapping):
    """A subagent transcript passed on its own: it sits in a subagents/ folder, or its first record says so."""
    if path.parent.name == "subagents":
        return True
    marker = mapping.get("subagent_marker")
    try:
        return bool(marker and get_first(json.loads(first_line), marker))
    except ValueError:
        return False


def spawn_text(call):
    inp = call["input"]
    return " ".join(v for v in inp.values() if isinstance(v, str)) if isinstance(inp, dict) else ""


def words(text):
    return set(re.findall(r"\w+", text.lower()))


def brief_score(brief, spawn):
    """How much a subagent's first message and a spawn call's arguments say the same thing, 0 to 1.

    One text inside the other counts as 1; otherwise the overlap of their word sets (Jaccard)."""
    brief = brief.strip()
    if not brief or not spawn.strip():
        return 0.0
    if brief in spawn or spawn.strip() in brief:
        return 1.0
    a, b = words(brief), words(spawn)
    return len(a & b) / len(a | b) if a | b else 0.0


MIN_BRIEF_SCORE = 0.3


def place_subagents(events):
    """Without timestamps, put each subagent's events right after the call that spawned it.

    The spawn is the call whose arguments overlap the subagent's first message best (briefs often share a
    template, so the first match is not good enough). A spawn serves one subagent: pairs are taken best score
    first, and equal scores go in order (the spawn that came first to the subagent that came first).
    Returns (events, placed) where placed maps each placed actor to its score and the best other score.
    """
    out = [e for e in events if e["actor"] == "main"]
    subs = {}
    for e in events:
        if e["actor"] != "main":
            subs.setdefault(e["actor"], []).append(e)
    spawns = [(k, c) for k, e in enumerate(out) for c in e["calls"] if call_cat(c) == "agent"]
    briefs = {a: next((e["text"] for e in evs if e["role"] == "user" and e["text"].strip()), "") for a, evs in subs.items()}
    scores = {(a, n): brief_score(briefs[a], spawn_text(c)) for a in subs for n, (_, c) in enumerate(spawns)}
    order = {a: n for n, a in enumerate(subs)}
    taken, chosen = set(), {}
    for (a, n), score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0][1], order[kv[0][0]])):
        if score >= MIN_BRIEF_SCORE and a not in chosen and n not in taken:
            chosen[a] = n
            taken.add(n)
    placed, inserted = {}, []
    for a, evs in subs.items():
        if a not in chosen:
            inserted.append(evs)
            continue
        others = [v for (b, n), v in scores.items() if b == a and n != chosen[a]]
        placed[a] = {"score": round(scores[(a, chosen[a])], 2), "runner_up": round(max(others), 2) if others else None,
                     "spawn": chosen[a]}
    after = {}
    for a, n in chosen.items():
        after.setdefault(spawns[n][0], []).append(subs[a])
    result = []
    for k, e in enumerate(out):
        result.append(e)
        for evs in after.get(k, []):
            result += evs
    for evs in inserted:                                              # no spawn explains these: at the end, flagged
        for e in evs:
            e["unplaced"] = True
        result += evs
    return result, placed


def first_record(lines):
    for ln in lines:
        if ln.strip():
            try:
                return json.loads(ln)
            except ValueError:
                return None
    return None


def read_subagent_meta(file, mapping, lines=()):
    """What links a subagent to its parent: the spawning call's id and the picked model from a sidecar
    meta file, and/or the parent thread id named in the rollout's own first record."""
    cfg = mapping.get("subagents")
    if not cfg:
        return {}
    found = {}
    if cfg.get("meta_file"):
        try:
            meta = json.loads(file.with_name(cfg["meta_file"].format(stem=file.stem)).read_text(encoding="utf-8"))
            found = {"call_id": get_first(meta, cfg.get("call_id")), "model": get_first(meta, cfg.get("model"))}
        except (OSError, ValueError, KeyError):
            pass
    first = first_record(lines) if cfg.get("parent_id") else None
    if first is not None:
        found["parent"] = get_first(first, cfg["parent_id"])
        found["agent_path"] = get_first(first, cfg.get("agent_path")) if cfg.get("agent_path") else None
    return found


def skip_copied_history(lines, mapping):
    """A forked subagent's rollout starts with a copy of its parent's history; keep only its own records.

    Returns (lines, info). info is None unless the first record names a fork and the ordinal its own records start at.
    """
    cfg = mapping.get("subagents") or {}
    first = first_record(lines) if cfg.get("history_start") else None
    if first is None or not get_first(first, cfg.get("forked_from")):
        return lines, None
    start = get_first(first, cfg["history_start"])
    if not isinstance(start, int) or isinstance(start, bool):
        return lines, None
    kept, dropped, first_seen = [], [], False
    for ln in lines:
        try:
            ordinal = get_first(json.loads(ln), cfg.get("ordinal")) if ln.strip() else None
        except ValueError:
            ordinal = None
        is_first = bool(ln.strip()) and not first_seen
        first_seen = first_seen or bool(ln.strip())
        if isinstance(ordinal, int) and ordinal < start:
            dropped.append((ordinal, is_first))
        else:
            kept.append(ln)
    copied = [o for o, is_first in dropped if not is_first]       # the first record is the rollout's own session_meta
    note = None
    if copied:
        note = (f"copied history skipped: {len(copied)} records (ordinals {min(copied)}\u2013{max(copied)})"
                + ("; session_meta read for linking only" if len(copied) < len(dropped) else ""))
    return kept, {"from_ordinal": start, "skipped": len(dropped), "note": note,
                  "version": get_first(first, mapping.get("fields", {}).get("version"))}


def spawn_for(events, mapping, agent_path):
    """(event, brief size in bytes) of the spawn call that named this agent path, or None."""
    cfg = mapping.get("subagents") or {}
    if not (agent_path and cfg.get("spawn_key") and cfg.get("brief_key")):
        return None
    for ev in events:
        for c in ev["calls"]:
            inp = c["input"]
            if ev["actor"] == "main" and call_cat(c) == "agent" and isinstance(inp, dict) \
                    and isinstance(inp.get(cfg["spawn_key"]), str) and isinstance(inp.get(cfg["brief_key"]), str) \
                    and (agent_path == inp[cfg["spawn_key"]] or agent_path.endswith("/" + inp[cfg["spawn_key"]].lstrip("/"))):
                return ev, len(inp[cfg["brief_key"]].encode("utf-8"))
    return None


def link_subagents(events, metas, mapping, placed, main_ids=()):
    """Attach each subagent to the call that spawned it. Returns {actor: {link, call_id}}.

    Exact: the sidecar meta file names the spawning call, or the call's result names the agent id,
    or the subagent's rollout names its parent thread (parent_thread_id), or the parent's text links to
    its id (markdown_link).
    Fallbacks: the brief matched against the spawning call (hosts without timestamps), then timestamps.
    """
    prefix = (mapping.get("subagents") or {}).get("file_prefix", "")
    spawners, by_agent = {}, {}
    for ev in events:
        if ev["actor"] != "main":
            continue
        for c in ev["calls"]:
            if c["id"] is not None:
                spawners.setdefault(c["id"], ev)
        if ev.get("spawned_agent") and ev["results"]:
            by_agent[str(ev["spawned_agent"])] = ev["results"][0]["id"]
    linked_in_text = {}
    if (mapping.get("subagents") or {}).get("link_regex"):       # the parent's text links to the subagent's id
        for ev in events:
            if ev["actor"] == "main" and ev["role"] == "assistant":
                for sub_id in re.findall(mapping["subagents"]["link_regex"], ev["text"]):
                    linked_in_text.setdefault(sub_id, ev)
    info = {}
    for actor in dict.fromkeys(e["actor"] for e in events if e["actor"] != "main"):
        evs = [e for e in events if e["actor"] == actor]
        stem = actor.split(":", 1)[1]
        call_id = metas.get(actor, {}).get("call_id")
        if metas.get(actor, {}).get("fixed_link"):           # the storage itself says whose chain this is
            info[actor] = {"link": metas[actor]["fixed_link"], "call_id": None}
            spawner = spawners.get(by_agent.get(stem))        # the spawning call's result names the agent id
            if spawner is not None:
                for e in evs:
                    e["turn"] = spawner["turn"]
                info[actor]["call_id"] = by_agent[stem]
                brief_key = (mapping.get("subagents") or {}).get("brief_key")
                call = next((c for c in spawner["calls"] if c["id"] == by_agent[stem]), None)
                if brief_key and call and isinstance(call["input"], dict) and isinstance(call["input"].get(brief_key), str):
                    info[actor]["brief_bytes"] = len(call["input"][brief_key].encode("utf-8"))
                model = next((e["spawned_model"] for e in events if e.get("spawned_agent") is not None
                              and str(e["spawned_agent"]) == stem and e["spawned_model"]), None)
                if model:
                    metas[actor]["model"] = model
            continue
        if call_id not in spawners:
            call_id = by_agent.get(stem[len(prefix):] if prefix and stem.startswith(prefix) else stem)
        spawner = spawners.get(call_id)
        if spawner is not None:
            for e in evs:
                e["turn"] = spawner["turn"]
            info[actor] = {"link": "tool_use_id", "call_id": call_id}
        elif stem in linked_in_text:
            for e in evs:
                e["turn"] = linked_in_text[stem]["turn"]
            info[actor] = {"link": "markdown_link", "call_id": None}
        elif metas.get(actor, {}).get("parent") in main_ids and main_ids:
            info[actor] = {"link": "parent_thread_id", "call_id": None}
            spawn = spawn_for(events, mapping, metas[actor].get("agent_path"))
            if spawn is not None:                 # the spawn call's message is the brief; only its size is kept
                info[actor]["brief_bytes"] = spawn[1]
                for e in evs:
                    e["turn"] = spawn[0]["turn"]
        elif actor in placed:
            info[actor] = {"link": "brief", "call_id": None, "match_score": placed[actor]["score"],
                           "runner_up_score": placed[actor]["runner_up"]}
        else:
            info[actor] = {"link": "timestamp" if any(e["ts"] is not None for e in evs) else "none", "call_id": None}
        if info[actor]["link"] != "none":
            for e in evs:
                e.pop("unplaced", None)                    # something explains where it belongs
    return info


def _event_keys(events):
    """A stable identity per event: its id, or its content plus how many identical ones the file had before."""
    ordinal, keys = {}, []
    for ev in events:
        key = ev["event_id"]
        if key is None:
            base = json.dumps([ev["role"], ev["text"], [(c["name"], c["input"], c["id"]) for c in ev["calls"]],
                               [r["id"] for r in ev["results"]], ev["msg_id"], ev["usage"], ev["model"], ev["ts"], ev["turn_start"],
                               ev["interrupted"], ev["offered"], ev["instruction"], ev["spawned_agent"], ev["descriptions"]], sort_keys=True, default=str)
            n = ordinal[(ev["file"], base)] = ordinal.get((ev["file"], base), -1) + 1
            key = (base, n)
        keys.append(key)
    return keys


def dedupe_calls(events):
    """Count each call once: a call id repeated with the same name and input (a duplicated record, copied history) is dropped.

    Several commands that share one call id (a script run as separate commands) differ in input and stay.
    Returns how many repeats were dropped."""
    seen, dropped = set(), 0
    for ev in events:
        kept = []
        for c in ev["calls"]:
            key = (c["id"], c["name"], json.dumps(c["input"], sort_keys=True, default=str)) if c["id"] is not None else None
            if key is not None and key in seen:
                dropped += 1
                ev["dup_calls"] = ev.get("dup_calls", 0) + 1
                continue
            if key is not None:
                seen.add(key)
            kept.append(c)
        ev["calls"] = kept
    return dropped


def dedupe(events):
    """Drop events already seen in another file (forked sessions, duplicated subagents).

    A main-actor file whose every event also sits in a subagent file is that subagent passed
    on its own, so it is dropped whole: subagent records never become main turns.
    """
    keys = _event_keys(events)
    sub_keys = {k for k, ev in zip(keys, events) if ev["actor"] != "main"}
    by_file = {}
    for k, ev in zip(keys, events):
        by_file.setdefault(ev["file"], []).append(k)
    twins = {f for f, ks in by_file.items()
             if ks and all(k in sub_keys for k in ks)
             and all(ev["actor"] == "main" for ev in events if ev["file"] == f)}
    first_file, kept = {}, []
    for key, ev in zip(keys, events):
        if ev["file"] in twins or first_file.setdefault(key, ev["file"]) != ev["file"]:
            continue
        kept.append(ev)
    return kept


# ---- host detection and drift -----------------------------------------------------------

def detect_host(lines, forced):
    if forced != "auto":
        return forced
    ratios = [(choose_variant(load_mapping(HOSTS[h]), lines, want_score=True)[1]["score"], -n, h)
              for n, h in enumerate(HOSTS)]
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
    ok = pct >= threshold and not missing                  # the structure verdict; health_report makes the final call
    parts = [f"{pct:.0f}% coverage"]
    if missing:
        parts.append("missing: " + ", ".join(missing))
    if unseen:
        parts.append("new types: " + ", ".join(unseen))
    if stats.get("version"):
        parts.append(f"transcript version {stats['version']}")
    text = (f"\u26a0 {mapping.get('host')} mapping (tested on {mapping.get('tested_versions')}): " + "; ".join(parts))
    return {"ok": ok, "coverage_pct": round(pct, 1), "missing_fields": missing,
            "unseen_types": with_other(unseen, TOP_TYPES), "warning": None if ok else text, "_text": text}


def settle_drift(drift, health):
    """The decision: pause (drift not ok, exit 2) only when a core metric is broken."""
    broken = health["core_broken"]
    drift["ok"] = not broken
    text = drift.pop("_text")
    drift["warning"] = None if drift["ok"] else text + "; broken: " + ", ".join(broken)
    return drift


VERSION_RE = re.compile(r"\d+(?:\.\d+)+(?:-[A-Za-z]+(?:\.\d+)*)?")   # 0.162.0-alpha.17.2 is one version


def _version_tuple(text):
    m = VERSION_RE.search(str(text))
    return tuple(int(n) for n in re.match(r"\d+(?:\.\d+)+", m.group(0)).group(0).split(".")) if m else None


def version_note(version, tested, drift):
    """Say when the transcript's version is outside the mapping's tested range.

    Returns None when it is inside, or when either side has no comparable version.
    """
    seen = _version_tuple(version) if version else None
    bounds = VERSION_RE.findall(str(tested or ""))
    if seen is None or not bounds:
        return None
    lo, hi = _version_tuple(bounds[0]), _version_tuple(bounds[-1])
    side = "newer" if seen > hi else "older" if seen < lo else None
    if side is None:
        return None
    return f"{side} than tested; checks passed" if drift["ok"] else drift["warning"]


# ---- health: can the numbers still be trusted? structure checks, then every metric counted a second way ---------

HEALTH_METRICS = ("turns", "tool_calls", "tokens", "errors", "verify", "interruptions", "time", "skills", "subagents")
CORE_METRICS = ("turns", "tool_calls", "tokens")
SEVERITY = {"warn": 1, "broken": 2}
# Words that tie an unseen record type to the metric its record would feed (a mapping may override them with [health_hints]).
UNSEEN_HINTS = {"tokens": ("usage", "token", "assistant"), "tool_calls": ("tool", "call", "function", "exec", "command", "assistant"),
                "errors": ("error", "exit", "fail"), "turns": ("user", "prompt", "turn", "message"),
                "interruptions": ("interrupt", "abort", "cancel"), "skills": ("skill",), "subagents": ("agent", "spawn", "thread")}


def null_missing_numbers(out):
    """A number whose field is missing is null ("not measurable"), never 0: read from the broken presence checks."""
    t = out.get("tokens") or {}
    nulled = set()
    for row in out["coverage"]["health"].get("rows", []):
        for c in row.get("checks", []):
            if c.get("passed") or c.get("layer") != "presence":
                continue
            if row["metric"] == "tokens" and "not_measurable" not in t:
                fields = [c["field"]] if c.get("field") else list(USAGE_FIELDS)
                nulled.update(TOKEN_KEY[k] for k in fields)
                if c.get("field") is None:
                    nulled.add("api_calls")
                if set(fields) & {"input", "cache_read", "cache_write"}:
                    nulled.add("context_at_invocation")
            if row["metric"] == "turns" and c["name"] == "assistant activity without any turn":
                out["window"]["turns_total"] = None
    null_token_keys(out, nulled)


def null_token_keys(out, keys):
    """The same numbers are null in every breakdown that carries them: the window, each turn, each actor, the "other" rows."""
    t = out.get("tokens") or {}
    for tokens in [t] + [r.get("tokens") for r in out.get("turns", []) + out.get("actors", []) if isinstance(r, dict)]:
        if isinstance(tokens, dict) and "not_measurable" not in tokens:
            for k in keys:
                if k in tokens:
                    tokens[k] = None
            if "lower_bound" in tokens:                     # a null number is not a lower bound of anything
                tokens["lower_bound"] = [k for k in tokens["lower_bound"] if k not in keys]
                if not tokens["lower_bound"]:
                    del tokens["lower_bound"]


def null_unrecorded_numbers(out, mapping):
    """A number the host never records is null ("not recorded by <host>") everywhere, never 0. Expected, so not a failure:
    no health row, no exit code. The window's tokens name the fields in `not_recorded`."""
    t = out.get("tokens") or {}
    gone = [TOKEN_KEY[k] for k in unrecorded_usage_fields(mapping)] if "not_measurable" not in t else []
    if gone:
        t["not_recorded"] = gone
        null_token_keys(out, gone)
    if "tool_s" in (out.get("time") or {}).get("not_recorded", []):      # tool time is unknown for every actor too
        for row in out.get("actors", []):
            row["tool_s"] = None


def frozen_window(parsed, events, mapping, stats, lo, hi, whole_session):
    """The window's own stats and raw records, from the lines up to its last record.

    Each line belongs to the turn of the event at or before it in its file. Lines of a later turn (the review's own,
    still running) are never read; lines before the window are kept only as `prior`, for running totals.
    Returns (stats, records, prior, the kept lines of the first main file)."""
    by_source = {}
    for e in events:
        if "line" in e and (whole_session or not e.get("unplaced")):
            by_source.setdefault((e["actor"], e.get("file")), []).append((e["line"], e["turn"]))
    out = {"lines": 0, "classified": 0, "unseen_types": {}, "ignored_types": {}, "truncated_last_line": False,
           "version": stats["version"], "skill_list_unparsed": 0}
    records, prior, first_main = [], [], None
    for actor, label, lines in parsed:
        marks = sorted(by_source.get((actor, label), [])) or [(0, 1)]       # a file with no event at all: nothing to place it by
        kept, turns, k = [], [], 0
        for n, ln in enumerate(lines):
            while k + 1 < len(marks) and marks[k + 1][0] <= n:
                k += 1
            turn = marks[k][1]
            if hi is not None and turn > hi:
                continue
            kept.append(ln)
            turns.append(turn)
        if actor == "main" and first_main is None:
            first_main = kept
        _, st = parse_lines(kept, mapping, actor)
        for key in ("lines", "classified", "skill_list_unparsed"):
            out[key] += st[key]
        out["truncated_last_line"] |= st["truncated_last_line"]
        for bucket in ("unseen_types", "ignored_types"):
            for t, c in st[bucket].items():
                out[bucket][t] = out[bucket].get(t, 0) + c
        for ln, turn in zip(kept, turns):
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            (prior if turn < lo else records).append((actor, obj))
    return out, records, prior, first_main


def health_quantities(events):
    """Numbers the engine counted itself, which a mapping's checks can name on either side."""
    calls = [c for e in events for c in e["calls"]]
    call_ids = {c["id"] for c in calls if c["id"] is not None}
    result_ids = {r["id"] for e in events for r in e["results"] if r["id"] is not None}
    q = {"turns": sum(1 for e in events if e.get("turn_open")), "tool_calls": len(calls), "calls_with_id": len(call_ids),
         "results": len(result_ids), "calls_without_result": len(call_ids - result_ids),
         "results_without_call": len(result_ids - call_ids),
         # share of calls that got a result; one call may still be in flight when the run is live
         "calls_with_result_pct": (100.0 * (1 - max(0, len(call_ids - result_ids) - 1) / len(call_ids))) if call_ids else None, "subagents": len({e["actor"] for e in events if e["actor"] != "main"}),
         "interruptions": sum(1 for e in events if e["interrupted"])}
    if any(e["usage"] for e in events):
        t = token_summary(events)
        q.update(api_calls=t["api_calls"], tokens_output=t["output"], tokens_fresh=t["fresh_input"], tokens_cache_read=t["cache_read"],
                 tokens_input_incl_cache=t["fresh_input"] + t["cache_read"],
                 tokens_cache_write=t["cache_write"], tokens_all=t["output"] + t["fresh_input"] + t["cache_read"] + t["cache_write"])
        main = [e for e in events if e["actor"] == "main"]
        if any(e["usage"] for e in main):
            m = token_summary(main)
            q.update(api_calls_main=m["api_calls"], tokens_output_main=m["output"], tokens_fresh_main=m["fresh_input"],
                     tokens_input_incl_cache_main=m["fresh_input"] + m["cache_read"],
                     tokens_cache_read_main=m["cache_read"], tokens_cache_write_main=m["cache_write"])
        seen = {}
        for e in events:
            if e["usage"] and e.get("usage_context") is not None and e["msg_id"] is not None:
                seen[e["msg_id"]] = (e["usage"]["input"] + e["usage"]["cache_read"], _int(e["usage_context"]))
        if seen:
            q.update(context_counted=sum(a for a, _ in seen.values()), context_reported=sum(c for _, c in seen.values()))
    return q


def side_value(spec, quantities, records, prior=()):
    """One side of a check: an engine quantity by name, or a count/distinct/sum/last taken straight from the raw records.

    Records are the window's. A `last` side is a running total: its value at the window's end, minus its value
    before the window's first record when the window starts later than the session."""
    if isinstance(spec, str):
        return quantities.get(spec)
    if "const" in spec:
        return spec["const"]
    pick = lambda recs: [o for actor, o in recs if (spec.get("actors", "all") == "all" or actor == "main") and _matches(o, spec.get("match", {}))]
    chosen = pick(records)
    if "last" in spec and "sum" not in spec:
        number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        at = lambda recs: next((v for v in (get_first(o, spec["last"]) for o in reversed(pick(recs))) if number(v)), None)
        end = at(list(prior) + list(records))
        if end is None or at(records) is None:
            return None                       # no running total inside the window
        start = at(prior)
        return end - start if start is not None else end
    if "distinct" in spec:
        values = {json.dumps(v, sort_keys=True) for o in chosen for v in get_all(o, spec["distinct"]) if v is not None}
        if "found_in" in spec:            # only the values that also appear at another path of other records
            other = spec["found_in"]
            seen = {json.dumps(v, sort_keys=True) for actor, o in records
                    if (other.get("actors", "all") == "all" or actor == "main") and _matches(o, other.get("match", {}))
                    for v in get_all(o, other["path"]) if v is not None}
            values &= seen
        return len(values)
    if "sum" in spec or "last" in spec:
        values = [v for v in (get_first(o, spec.get("sum") or spec["last"]) for o in chosen)
                  if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return (sum(values) if "sum" in spec else values[-1] if values else None) if values or "sum" in spec else None
    return len(chosen)


def coverage_notes(mapping, records):
    """Info notes a mapping declares: entries of the window's records that are counted, never metrics, never warnings.

    Each [[coverage_notes]] names records (`match`), the entries inside them (`each`, a path) and which entries count
    (`where`); `split` counts subsets by name. `text` takes {count} and each split name. Only numbers, never content."""
    notes = []
    for spec in mapping.get("coverage_notes", []):
        entries = [e for _, o in records if _matches(o, spec.get("match", {}))
                   for e in (get_all(o, spec["each"]) if spec.get("each") else [o]) if isinstance(e, dict)]
        entries = [e for e in entries if _matches(e, spec.get("where", {}))]
        if entries:
            counts = {name: sum(1 for e in entries if _matches(e, m)) for name, m in spec.get("split", {}).items()}
            notes.append(spec["text"].format(count=len(entries), **counts))
    return notes


def check_passes(first, second, relation, tolerance_pct=0.0, slack_first=0):
    tol = tolerance_pct / 100.0
    if relation == "at_most":
        return first <= second * (1 + tol) + 1e-9
    if relation == "at_least":
        return first >= second * (1 - tol) - 1e-9
    if abs(first - second) <= tol * max(abs(first), abs(second)) + 1e-9:
        return True
    return 0 < first - second <= slack_first                    # the one thing still in flight when the run is live


def version_position(version, tested):
    seen = _version_tuple(version) if version else None
    bounds = VERSION_RE.findall(str(tested or ""))
    if seen is None or not bounds:
        return "unknown"
    lo, hi = _version_tuple(bounds[0]), _version_tuple(bounds[-1])
    return "newer" if seen > hi else "older" if seen < lo else "within"


def measurable_metrics(mapping):
    caps = mapping.get("capabilities", {})
    prefixes = mapping.get("interruption_prefixes") or any(r.get("interrupt") for r in mapping.get("records", [])) \
        or mapping.get("interrupt_on_repeated_prompt")
    return {"turns": True, "tool_calls": True, "tokens": bool(caps.get("has_tokens")), "errors": bool(caps.get("has_tool_results")),
            "verify": True, "interruptions": bool(prefixes), "time": bool(caps.get("has_timestamps")) or has_prompt_clock(mapping),
            "skills": True, "subagents": True}


def health_report(mapping, stats, events, records, drift, threshold, prior=(), whole=True):
    """One row per metric: its status and the checks that ran, each with its two values.

    events and records are the window's; prior holds the records before it, for running totals (a `last` side)."""
    measurable = measurable_metrics(mapping)
    quantities = health_quantities(events)
    quantities["duplicate_calls"] = sum(e.get("dup_calls", 0) for e in events)
    missing = missing_usage_fields(events) if measurable["tokens"] else {}
    for k in missing:                         # a number that is gone is never compared: its checks are skipped, not failed
        for q in QUANTITIES_OF_FIELD[k]:
            for name in (q, q + "_main"):
                if name in quantities:
                    quantities[name] = None
    checks = {m: [] for m in HEALTH_METRICS}

    def add(metric, name, layer, first, second, relation, passed, severity, **extra):
        checks[metric].append({"name": name, "layer": layer, "first": first, "second": second, "relation": relation,
                               "passed": passed, "severity": severity, **extra})

    for spec in mapping.get("checks", []):                                  # layer 2: the same metric counted a second way
        metric = spec["metric"]
        first, second = side_value(spec["first"], quantities, records, prior), side_value(spec["second"], quantities, records, prior)
        if metric not in checks or not measurable[metric] or first is None or second is None:
            continue
        if spec.get("skip_if_second_zero") and not second:      # the host does not write this second record at all
            continue
        if spec.get("scope") == "session" and not whole:          # a whole-session total says nothing about part of a session
            continue
        tol, slack = float(spec.get("tolerance_pct", 0)), int(spec.get("slack_first", 0))
        passed = check_passes(first, second, spec.get("relation", "equal"), tol, slack)
        skipped = {}
        if any(side in ("context_counted", "context_reported") for side in (spec["first"], spec["second"]) if isinstance(side, str)):
            stopped = interrupted_calls(events)[0]          # an interrupted call has no context size: left out, said so
            if stopped:
                skipped = {"skipped": f"skipped for {stopped} interrupted call{'' if stopped == 1 else 's'}"}
        # a second count that disagrees is a warning with its two values, never a pause: the first count still stands
        add(metric, spec["name"], "second count", first, second, spec.get("relation", "equal"), passed, "warn",
            **({"tolerance_pct": tol} if tol else {}), **skipped)
    second_passed = {m: any(c["layer"] == "second count" and c["passed"] for c in checks[m]) for m in HEALTH_METRICS}

    if measurable["turns"] and quantities["turns"] == 0:
        if any(e["role"] == "assistant" and e["actor"] == "main" for e in events):      # the turn marker is gone: turns are missing
            add("turns", "assistant activity without any turn", "presence", 0, 1, "at_least", False, "broken")
        elif any(e["role"] == "assistant" for e in events):                            # only a subagent's records: legitimate
            add("turns", "subagent activity without a main turn", "structure", 0, 1, "at_least", False, "warn")
    pct = drift["coverage_pct"]                                              # layer 1: structure
    for m in HEALTH_METRICS:                                                 # coverage alone never breaks anything
        if measurable[m]:
            add(m, "coverage", "structure", pct, threshold, "at_least", pct >= threshold, "warn")
    if "tool_name" in drift["missing_fields"]:
        for m in ("tool_calls", "verify", "errors"):
            if measurable[m]:
                add(m, "required field tool_name", "structure", "missing", "present", "equal", False,
                    "broken" if m in CORE_METRICS else "warn")
    # presence: evidence that a core metric lost its records
    if stats["lines"] and not stats["classified"]:                           # nothing at all was recognised
        for m in CORE_METRICS:
            if measurable[m]:
                add(m, "no records recognised", "presence", 0, stats["lines"], "at_least", False, "broken")
    caps = mapping.get("capabilities", {})
    results, calls = quantities["results"], quantities["tool_calls"]
    active = any(e["role"] == "assistant" for e in events) or results > 0 or calls > 0     # results prove the agent replied
    hints = {**UNSEEN_HINTS, **mapping.get("health_hints", {})}
    renamed = any(any(w in kind.lower() for w in hints.get("tokens", ())) for kind in stats["unseen_types"])
    if measurable["tokens"] and (active or renamed) and not any(e["usage"] for e in events):
        # no usage at all on a host that records it: the replies happened (or a record named like them is new) but carry none
        add("tokens", "no token usage found", "presence", 0, 1, "at_least", False, "broken")
    if results > 0 and calls == 0:
        add("tool_calls", "results without calls", "presence", calls, results, "at_least", False, "broken")
    for k, n in missing.items():
        add("tokens", f"{USAGE_NOUN[k]} missing from all {n} usage record{'' if n == 1 else 's'}", "presence", 0, n, "at_least",
            False, "broken", field=k)
    for k, (m, n) in (partial_usage_fields(events) if measurable["tokens"] else {}).items():
        # some records lack the field: the total is a lower bound, a warning naming how many, never a pause
        add("tokens", f"{USAGE_NOUN[k]} missing from {m} of {n} usage records", "partial", n - m, n, "equal", False, "warn", field=k)
    stopped, n_calls = interrupted_calls(events) if measurable["tokens"] else (0, 0)
    if stopped and stopped < n_calls:
        add("tokens", f"{stopped} of {n_calls} calls {'was' if stopped == 1 else 'were'} interrupted; "
                      f"{'its' if stopped == 1 else 'their'} token usage wasn't recorded", "partial", n_calls - stopped, n_calls, "equal", False, "warn")
    declared_calls = [c for e in events if e.get("declares_call_id") for c in e["calls"]]
    if declared_calls and not any(c["id"] is not None for c in declared_calls):
        n = len(declared_calls)
        add("tool_calls", f"call ids missing from all {n} tool call{'' if n == 1 else 's'}", "presence", 0, n, "at_least", False, "broken")
    started = next((r["result_started"] for r in mapping.get("records", []) if r.get("result_started")), None)
    if measurable["time"] and mapping.get("tool_time_source") == "result_spans" and started and not tool_timing_recorded(events):
        n = sum(len(e["results"]) for e in events)          # tool time is not core: a warning naming the field, never a pause
        add("time", f"tool timing missing from all {n} tool result{'' if n == 1 else 's'}: {started}", "presence", 0, n, "at_least",
            False, "warn", field=started)
    if caps.get("has_tool_results") and calls > 1 and results == 0:         # one call may still be running
        add("tool_calls", "calls without any results", "presence", results, calls, "at_least", False, "broken")
    hints = {**UNSEEN_HINTS, **mapping.get("health_hints", {})}
    unattributed = []
    for kind, n in stats["unseen_types"].items():
        hit = [m for m, words in hints.items() if m in checks and measurable.get(m) and any(w in kind.lower() for w in words)]
        unattributed += [] if hit else [kind]
        for m in hit:         # a record type nobody has seen is a warning naming it, never a pause
            add(m, "unseen record type", "structure", n, 0, "equal", False, "warn", type=kind)

    rows = []
    for m in HEALTH_METRICS:
        failed = [c for c in checks[m] if not c["passed"]]
        if failed:
            status = max((c["severity"] for c in failed), key=SEVERITY.get)
        elif not measurable[m]:
            status = "not_measurable"
        else:
            status = "ok" if second_passed[m] else "one_way"
        rows.append({"metric": m, "status": status, "core": m in CORE_METRICS and measurable[m],
                     "checks": [{k: v for k, v in c.items() if k != "severity"} for c in checks[m]] if measurable[m] else []})
    return {"rows": rows, "unattributed_types": unattributed[:TOP_TYPES],
            "core_broken": [r["metric"] for r in rows if r["core"] and r["status"] == "broken"]}


def health_view(health, mapping, version, show_all=False):
    """The table as printed: only rows that need attention, unless all are asked for; always with the version context."""
    rows = health["rows"]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    shown = rows if show_all else [r for r in rows if r["status"] in SEVERITY]
    tested = mapping.get("tested_versions")
    view = {"summary": counts, "version": {"transcript": version, "tested": tested, "position": version_position(version, tested)},
            "rows": [{**r, "checks": r["checks"] if show_all else [c for c in r["checks"] if not c["passed"]]} for r in shown]}
    if health["unattributed_types"]:
        view["unattributed_types"] = health["unattributed_types"]
    return view


def has_prompt_clock(mapping):
    return any(r.get("prompt_time") for r in mapping.get("records", []))


def not_measurable(mapping):
    """What the host does not record, in plain words (the capability flags stay in coverage.capabilities)."""
    labels = {"has_tokens": "tokens", "has_timestamps": "time", "has_tool_results": "tool results",
              "has_ids": "message ids"}
    if has_prompt_clock(mapping):                 # wall time is reported coarsely; only the rest is missing
        labels["has_timestamps"] = "tool timing"
    return [labels[k] for k, v in mapping["capabilities"].items() if not v and k in labels]


# ---- redaction and output ----------------------------------------------------------------

SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9_\-]{16,}", r"gh[pousr]_[A-Za-z0-9]{20,}", r"xox[baprs]-[A-Za-z0-9\-]{10,}",
    r"AKIA[0-9A-Z]{16}", r"AIza[0-9A-Za-z_\-]{30,}", r"Bearer\s+[A-Za-z0-9._\-]{16,}",
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*",
]
# A secret-like name ENDS with one of these words (DB_PASSWORD, GITHUB_TOKEN, accessToken, X-Api-Key).
# Nothing may follow it, so has_tokens, max_tokens and token_count are ordinary names, not secrets.
SECRET_KEY = r"\w*(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY|ACCESS[_-]?KEY)"
KEY_VALUE_RE = re.compile(                      # KEY=value, KEY: value, KEY = 'value'
    rf"(?i)({SECRET_KEY}\s*[=:]\s*\\?[\"']?)('[^']*'|[^\s\"',;&\\{{\[][^\s\"',;&\\]*)")
JSON_KEY_RE = re.compile(rf"(?i)({SECRET_KEY}\\?[\"']\s*:\s*\\?[\"'])([^\"'\\]+)")   # "key": "value"
URL_CREDS_RE = re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(@)", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
HOME_RE = re.compile(r"(?:/Users|/home)/[^/\s\"'\\]+")
WIN_HOME_RE = re.compile(r"[A-Za-z]:(?:\\{1,2})Users(?:\\{1,2})[^\\/\s\"']+")      # C:\Users\name, also JSON-escaped
TOKEN_RE = re.compile(r"[A-Za-z0-9_+=\-]{24,}")


def _entropy(token):
    counts = {c: token.count(c) for c in set(token)}
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


ID_RE = re.compile(
    r"^(?:toolu|srvtoolu|call|msg|req|resp|chatcmpl)_"
    # a UUID or UUIDv7-like id, optionally after short hyphen-joined words (rollout-2026-11-11T11-31-48-<uuid>)
    r"|^(?:[A-Za-z0-9]{1,8}-)*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _mask_token(m):
    t = m.group(0)
    if ID_RE.match(t):
        return t
    if re.search(r"\d", t) and re.search(r"[A-Za-z]", t) and _entropy(t) >= 4.0:
        return "[REDACTED]"
    return t


def mask_home(text):
    """Replace this machine's home folder, and any /Users/x, /home/x or C:\\Users\\x, with ~."""
    home = str(Path.home())
    if len(home) > 1:
        for variant in (home.replace("\\", "\\\\"), home, Path.home().as_posix()):   # JSON-escaped first
            text = text.replace(variant, "~")
    return WIN_HOME_RE.sub("~", HOME_RE.sub("~", text))


def redact(text):
    text = mask_home(text)
    for pat in SECRET_PATTERNS:
        text = re.sub(pat, "[REDACTED]", text)
    text = URL_CREDS_RE.sub(r"\1[REDACTED]\2", text)
    text = KEY_VALUE_RE.sub(r"\1[REDACTED]", text)
    text = JSON_KEY_RE.sub(r"\1[REDACTED]", text)
    text = EMAIL_RE.sub("[EMAIL]", text)
    return TOKEN_RE.sub(_mask_token, text)


MISSING_NUMBER_KEYS = frozenset(ROW_TOKEN_KEYS) | {"context_at_invocation", "turns_total"}


def _md_value(v, key=None, gone=None):
    """Markdown never prints None: a number the host never records is "not recorded by <host>" (`gone` maps those keys to
    the words), a missing number is "not measurable", any other empty value is "none"."""
    gone = gone or {}
    if v is None:
        return gone[key] if key in gone else NOT_MEASURABLE if key in MISSING_NUMBER_KEYS else "none"
    if isinstance(v, dict):
        lower = set(v.get("lower_bound") or []) if isinstance(v.get("lower_bound"), list) else set()
        return {k: f"at least {fmt_tokens(x)}" if k in lower and isinstance(x, int) else _md_value(x, k, gone) for k, x in v.items()}
    if isinstance(v, list):
        return [_md_value(x, key, gone) for x in v]
    return v


def _fmt(v, key=None, gone=None):
    v = _md_value(v, key, gone)
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    return str(v)


def coarse_time_view(time):
    """Markdown form of a coarse wall time: minutes, never a raw number of seconds."""
    low, high = time["wall_range_s"]
    if high <= 60:
        wall = "under 1 min"
    else:
        wall = f"about {round(time['wall_s'] / 60)} min (between {round(low / 60)}\u2013{round(high / 60)} min)"
    return {"wall": wall, **{k: v for k, v in time.items() if k not in ("wall_s", "wall_range_s")}}


def not_recorded_words(out):
    """{key: "not recorded by <host>"} for each number the host never records (tokens and time name them)."""
    cov = out.get("coverage") or {}
    host = HOST_NAMES.get(cov.get("host"), str(cov.get("host")).title())
    keys = [k for section in ("tokens", "time") if isinstance(out.get(section), dict) for k in out[section].get("not_recorded", [])]
    return {k: f"not recorded by {host}" for k in keys}


ISSUE_NEW = "https://github.com/justin-nicky/workflow-reviewer-skill/issues/new"
ISSUE_URL_LIMIT = 7000
VERSION_TEXT_RE = re.compile(r"^(?=[^\d]*\d)[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}$")
ADAPTER_KIND = {"builtin": "built-in", "user": "user", "override": "override"}


def os_family(plat=None):
    """The OS family, named as platform.system() names it, from sys.platform alone: platform.system() would import socket
    on Windows (platform.uname looks up the host name), and the script imports no network module."""
    plat = plat or sys.platform
    for prefix, name in (("win32", "Windows"), ("cygwin", "Windows"), ("darwin", "Darwin"), ("linux", "Linux"), ("freebsd", "FreeBSD"),
                         ("openbsd", "OpenBSD"), ("netbsd", "NetBSD")):
        if plat.startswith(prefix):
            return name
    return "other"


def skill_version():
    """The version in SKILL.md's front matter, beside this script."""
    try:
        text = (Path(__file__).resolve().parent.parent / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    m = re.search(r'^\s*version:\s*"?([0-9][0-9A-Za-z.+-]*)"?\s*$', text, re.M)
    return m.group(1) if m else "unknown"


def needs_issue(out):
    """A review that paused (exit 2) or has a health row needing attention."""
    cov = out.get("coverage") or {}
    return (cov.get("drift") or {}).get("ok") is False or any(r["status"] in SEVERITY for r in (cov.get("health") or {}).get("rows", []))


def issue_url(out, code):
    """A prefilled "new issue" URL holding only allowlisted diagnostics. Built offline; only the user opens and submits it.

    Never in it: paths, session ids, prompts, previews, commands, tool arguments, file names, model names, user adapter names,
    per-turn token numbers. Values that come from a transcript or a user adapter pass a strict pattern or are replaced."""
    cov = out["coverage"]
    builtin = cov.get("mapping_source") == "builtin"
    host = cov.get("host") if plain_key(str(cov.get("host"))) else "<user host>"
    tv = cov.get("transcript_version")
    tv = "none" if tv is None else str(tv) if VERSION_TEXT_RE.match(str(tv)) else "<unrecognised>"
    tested = cov.get("tested_versions")
    tested = "none" if tested is None else str(tested) if builtin else str(tested) if VERSION_TEXT_RE.match(str(tested)) else "<user text>"
    health = cov.get("health") or {}
    order = {m: i for i, m in enumerate(HEALTH_METRICS)}
    rows = sorted((r for r in health.get("rows", []) if r["status"] in SEVERITY),       # most important first
                  key=lambda r: (-SEVERITY[r["status"]], not r.get("core"), order.get(r["metric"], len(order))))
    names = []
    for r in rows:
        found = []
        for c in r.get("checks", []):
            if c.get("passed"):
                continue
            name = c["name"]
            if not builtin and c.get("layer") == "second count":     # a check a user adapter named: its name is the user's text
                name = "<adapter check>"
            elif not builtin and ": " in name:                         # a template naming a field of the user's mapping
                name = name.split(": ", 1)[0]
            found.append(name)
        names.append(list(dict.fromkeys(found)))
    drift = cov.get("drift") or {}
    unseen = drift.get("unseen_types") or {}
    shown_types = [t for t in unseen if plain_key(str(t))]
    summary = health.get("summary") or {}
    worst = rows[0]["status"] if rows else "exit 2" if code == 2 else "ok"
    title = f"Review problem: {host} {'schema ' + tv if tv.isdigit() else tv}, {worst}"      # a bare number is a schema (Devin)

    def make(names, types, truncated):
        block = [f"skill_version: {skill_version()}", f"exit_code: {code}", f"host: {host}", f"adapter_label: {cov.get('label')}",
                 f"adapter_kind: {ADAPTER_KIND.get(cov.get('mapping_source'), 'user')}", f"tested_range: {tested}",
                 f"transcript_version: {tv}", f"version_position: {(health.get('version') or {}).get('position', 'unknown')}",
                 "health_summary: " + (", ".join(f"{k} {summary[k]}" for k in sorted(summary)) or "none"),
                 "health_rows:" + ("" if rows else " none")]
        for r, n in zip(rows, names):
            block.append(f"  - {r['metric']}: {r['status']}" + (f"; {' | '.join(n)}" if n else ""))
        if truncated:
            block.append("  (truncated)")
        nr = (out.get("tokens") or {}).get("not_recorded") or []
        block += [f"not_recorded: {', '.join(nr) or 'none'}", f"drift_coverage_pct: {drift.get('coverage_pct')}",
                  f"unseen_types_count: {len(unseen)}", f"unseen_types: {', '.join(types) or 'none'}",
                  f"os: {os_family()}", f"python: {sys.version_info[0]}.{sys.version_info[1]}"]
        body = ("Diagnostics from workflow-reviewer: counts, statuses and check names only, with no paths, prompts, commands "
                "or file names. Read and edit anything before you submit.\n\n```\n" + "\n".join(block) + "\n```\n\n"
                "**What were you doing?**\n\n\n**What did you expect?**\n\n\n**Anything else?**\n")
        return ISSUE_NEW + "?" + urlencode({"title": title, "body": body}, quote_via=quote)

    url, truncated = make(names, shown_types, False), False
    for i in range(len(names) - 1, -1, -1):                 # over the limit: drop whole check lists, lowest-priority row first
        if len(url) < ISSUE_URL_LIMIT:
            break
        if names[i]:
            names[i], truncated = [], True
            url = make(names, shown_types, True)
    if len(url) >= ISSUE_URL_LIMIT:
        url = make(names, ["(truncated)"], True)
    return url


def render_md(out):
    gone = not_recorded_words(out)
    lines = ["# workflow-reviewer metrics"]
    if out.get("header"):
        lines.append(out["header"])
    warning = out.get("coverage", {}).get("drift", {}).get("warning")
    if warning:
        lines.append(warning)
    for key, value in out.items():
        if key == "header":
            continue
        lines.append(f"## {key.replace('_', ' ').capitalize()}")
        if key == "coverage" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "issue_url"}       # the link gets its own line, only when needed
        if key == "time" and isinstance(value, dict) and "wall_range_s" in value:
            lines += [f"- {k}: {_fmt(v)}" for k, v in coarse_time_view(value).items()]
        elif isinstance(value, dict):
            lower = set(value.get("lower_bound") or []) if isinstance(value.get("lower_bound"), list) else set()
            lines += [f"- {k}: " + (f"at least {fmt_tokens(v)}" if k in lower and isinstance(v, int) else _fmt(v, k, gone))
                      for k, v in value.items()]
        elif isinstance(value, list):
            lines += [f"- {_fmt(v, None, gone)}" for v in value]
        else:
            lines.append(str(value))
    if (out.get("coverage") or {}).get("issue_url") and needs_issue(out):
        lines.append(f"Report this problem: {out['coverage']['issue_url']}")
    return "\n".join(lines)


def emit(out, fmt, do_redact):
    text = json.dumps(out, indent=2, ensure_ascii=False) if fmt == "json" else render_md(out)
    print(redact(text) if do_redact else text)


# ---- skills, instruction files, degraded mode ---------------------------------------------

def skill_dirs(folders, repo):
    """Skill folders a mapping declares, as (path, scope). `path` is relative (the repo and up to four
    parents, or the repo alone with walk = false), starts with ~ (home), is absolute, or sits under a
    `base` that an environment variable (`base_env`) may replace."""
    home = Path.home()
    parents = [p for p in [repo, *list(repo.parents)[:4]] if p != home]       # home holds user-level skills
    found = []
    for f in folders or []:
        path, scope = f["path"], f.get("scope", "project")
        if f.get("glob"):                        # a pattern under the base: every folder it matches
            base = Path(os.path.expanduser(os.environ.get(f.get("base_env", "")) or f.get("base", "~")))
            found += [(m, scope) for m in sorted(base.glob(path)) if m.is_dir()]
        elif f.get("base") or f.get("base_env"):
            base = os.environ.get(f.get("base_env", "")) or f.get("base", "~")
            found.append((Path(os.path.expanduser(base)) / path, scope))
        elif path.startswith("~") or Path(path).is_absolute():
            found.append((Path(os.path.expanduser(path)), scope))
        elif f.get("walk", True):
            found += [(p / path, scope) for p in parents]
        else:
            found.append((repo / path, scope))
    return found


def builtin_folders(host):
    """The skill folders of a built-in host's mapping (None for a host with no mapping of its own)."""
    name = {**HOSTS, "kiro": "kiro", **STORE_HOSTS}.get("claude-code" if host == "auto" else host)
    return load_mapping(name).get("skill_folders") if name else None


def skill_inventory(folders, repo):
    """One entry per distinct (name, content); `scopes` says where its copies live. None when no folders are declared."""
    if not folders:
        return None
    entries = {}
    for base, scope in skill_dirs(folders, repo):
        for md in sorted(base.glob("*/SKILL.md")):
            try:
                data = md.read_bytes()
            except OSError:
                continue
            key = (md.parent.name, hashlib.sha256(data).hexdigest())
            if key in entries:
                if scope not in entries[key]["scopes"]:
                    entries[key]["scopes"] = sorted(entries[key]["scopes"] + [scope])
                continue
            m = re.search(r"^description:[ \t]*(.*)$", data.decode("utf-8", "replace"), re.M)
            desc = m.group(1).strip().strip("\"'") if m else ""
            entries[key] = {"name": md.parent.name, "description_chars": len(desc), "bytes": len(data),
                            "scopes": [scope]}
    return list(entries.values())


NO_FOLDERS = "skill folders unknown for this host"


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


CC_COMMAND_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
CC_SKILL_BODY = "Base directory for this skill"
CODEX_LINK_RE = re.compile(r"\[\$([\w:.\-]+)\]\([^)]*SKILL\.md\)")
CODEX_BLOCK_RE = re.compile(r"<skill>\s*<name>\s*([^<\s]+)\s*</name>")
CURSOR_ATTACHED_RE = re.compile(r"Skill Name:[ \t]*(\S+)")


def invoked_skills(events, host):
    """Skills the user started by name, as (name, detected_by, bytes), in order.

    Each host injects the skill body next to the user's request; the pair must be present.
    """
    main = [e for e in events if e["actor"] == "main"]
    found = []
    for k, ev in enumerate(main):
        if ev.get("slash_skill"):                      # the mapping read a typed /name from the prompt
            found.append((ev["slash_skill"], "slash command", None, ev["turn"]))
            continue
        if host == "claude-code" and ev["role"] == "user" and not ev["non_turn"]:
            m = CC_COMMAND_RE.search(ev["text"])
            if not m:
                continue
            for nxt in main[k + 1:]:
                if is_turn_start(nxt):
                    break
                if nxt["role"] == "user" and nxt["text"].lstrip().startswith(CC_SKILL_BODY):
                    found.append((m.group(1), "slash command", len(nxt["text"].encode("utf-8")), ev["turn"]))
                    break
        elif host == "codex" and ev["role"] in ("user", "meta"):
            for link in CODEX_LINK_RE.finditer(ev["text"]):
                for nxt in main[k + 1:k + 6]:
                    block = CODEX_BLOCK_RE.search(nxt["text"])
                    if block and block.group(1) == link.group(1):
                        found.append((link.group(1), "$skill link", len(nxt["text"].encode("utf-8")), ev["turn"]))
                        break
        elif host == "cursor" and ev["role"] == "user" and "<manually_attached_skills>" in ev["text"]:
            size = len(ev["text"].encode("utf-8"))
            found += [(name, "attached skill", size, ev["turn"]) for name in CURSOR_ATTACHED_RE.findall(ev["text"])]
    return found


TOP_OFFERED = 100


def offered_skills(events):
    """Skill names the host offered the main agent this session (None when it records no listing)."""
    names, seen = [], False
    for ev in events:
        if ev["actor"] == "main" and isinstance(ev.get("offered"), list):
            seen = True
            names += [n for n in ev["offered"] if isinstance(n, str) and n not in names]
    return names if seen else None


def is_offered(name, offered):
    return name in offered or any(o.endswith(":" + name) for o in offered)


# ---- overlaps: descriptions against descriptions, directive lines against always-on instructions, hooks --------

STOPWORDS = frozenset("""the and for with that this from your you are was were been being have has had not but can will would could
should may might into onto over under than then them they their there these those when where which while what who whom why how all any
each every both such own same too very just also via per use used using uses when whenever about after before again once only more most
some other out off its our""".split())
DIRECTIVE_RE = re.compile(r"\b(always|never|must|don't|do not|only|run|use|prefer|should)\b", re.I)
DESCRIPTION_TERMS = 15          # terms kept per skill description (calibrated on real descriptions)
MAX_SKILL_FILES = 2000
MAX_FILE_BYTES = 256 * 1024
MAX_SHARED_TERMS = 5
MAX_TERM_CHARS = 20
TOP_OVERLAPS = 10
MIN_DIRECTIVE_TERMS = 3
COMMON_CATEGORIES = ("shell", "read", "edit", "search")


def _stem(word):
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def term_list(text, surface, bigrams=True):
    """Unigrams (and bigrams) of the informative words: lowercase, no stopwords or short words, simple stems."""
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in STOPWORDS]
    stems = [_stem(w) for w in words]
    for st, w in zip(stems, words):
        surface.setdefault(st, w)
    if not bigrams:
        return list(stems)
    grams = list(stems) + [f"{a} {b}" for a, b in zip(stems, stems[1:])]
    for a, b, wa, wb in zip(stems, stems[1:], words, words[1:]):
        surface.setdefault(f"{a} {b}", f"{wa} {wb}")
    return grams


def tfidf(documents, bigrams=True, top=None):
    """L2-normalised TF-IDF vectors; the IDF is ln(1 + N/df) over all the documents, so words every document uses count for little.

    With `top`, each document keeps only its `top` heaviest terms, so a long description is not diluted by its length."""
    surface = {}
    counts = []
    for text in documents:
        c = {}
        for t in term_list(text, surface, bigrams):
            c[t] = c.get(t, 0) + 1
        counts.append(c)
    n, df = len(counts), {}
    for c in counts:
        for t in c:
            df[t] = df.get(t, 0) + 1
    vectors = []
    for c in counts:
        v = {t: (1 + math.log(tf)) * math.log(1 + n / df[t]) for t, tf in c.items()}
        if top:
            v = dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))[:top])
        norm = math.sqrt(sum(x * x for x in v.values()))
        vectors.append({t: x / norm for t, x in v.items()} if norm else {})
    return vectors, surface


def similar_pairs(left, right, threshold, surface, same=False, stats=None):
    """[(i, j, score, shared terms)] for vector pairs at or over the threshold, best first (ties by position).

    `stats`, when given, receives the best score of any pair, over the threshold or not."""
    index = {}
    for j, v in enumerate(right):
        for t, w in v.items():
            index.setdefault(t, []).append((j, w))
    found = []
    for i, v in enumerate(left):
        scores = {}
        for t, w in v.items():
            for j, x in index.get(t, ()):
                if not same or j > i:
                    scores[j] = scores.get(j, 0.0) + w * x
        if stats is not None and scores:
            stats["best"] = max(stats.get("best", 0.0), max(scores.values()))
        for j, score in scores.items():
            if score >= threshold - 1e-9:
                shared = sorted(((v[t] * right[j][t], t) for t in v if t in right[j]), reverse=True)
                names = []
                for _, t in shared:
                    word = surface.get(t, t)
                    if len(word) <= MAX_TERM_CHARS and word not in names:
                        names.append(word)
                found.append((i, j, score, names[:MAX_SHARED_TERMS]))
    found.sort(key=lambda f: (-f[2], f[0], f[1]))
    return found


def display_path(path, repo):
    """Relative to the repo, else ~/…, else with the home folder masked."""
    p = Path(os.path.realpath(path))
    for base, prefix in ((Path(os.path.realpath(repo)), ""), (Path(os.path.realpath(Path.home())), "~/")):
        try:
            return prefix + p.relative_to(base).as_posix()
        except ValueError:
            continue
    return mask_home(str(path))


def frontmatter(lines):
    """({key: value}, index of the first body line). Handles plain, quoted and folded/literal block values."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), None)
    if end is None:
        return {}, 0
    meta, i = {}, 1
    while i < end:
        m = re.match(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$", lines[i])
        i += 1
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        block = []
        while i < end and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
            block.append(lines[i].strip())
            i += 1
        if value in (">", "|", ">-", "|-", ">+", "|+") or not value:
            value = " ".join(b for b in block if b)
        elif block:
            value = " ".join([value, *[b for b in block if b]])
        meta[key] = value.strip().strip("\"'")
    return meta, end + 1


def skill_files(folders, plugin_folders, repo):
    """Every SKILL.md the host reads: [{name, description, path, display, lines, body_start}] and the number unreadable."""
    seen, found, unreadable = set(), [], 0
    plugin_dirs = skill_dirs(plugin_folders, repo)
    for base, _ in skill_dirs(folders, repo) + plugin_dirs:
        for md in sorted(base.glob("*/SKILL.md")):
            real = os.path.realpath(md)
            if real in seen or len(found) >= MAX_SKILL_FILES:
                continue
            seen.add(real)
            try:
                text = Path(real).read_bytes()[:MAX_FILE_BYTES].decode("utf-8", "replace")
            except OSError:
                unreadable += 1
                continue
            lines = text.split("\n")
            meta, body_start = frontmatter(lines)
            found.append({"name": meta.get("name") or md.parent.name, "description": meta.get("description", ""), "path": real,
                          "display": display_path(real, repo), "lines": lines, "body_start": body_start,
                          "plugin": any(base == d for d, _ in plugin_dirs)})
    return found, unreadable


def directive_lines(lines, start=0):
    """[(line number, text)] for lines holding a directive word, outside code blocks, with enough informative words."""
    found, fenced = [], False
    for n, line in enumerate(lines[start:], start + 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        text = stripped.replace("’", "'")
        if fenced or not text or not DIRECTIVE_RE.search(text):
            continue
        if len(term_list(text, {})) >= MIN_DIRECTIVE_TERMS:
            found.append((n, text[:400]))
    return found


def always_on_files(events, repo):
    """[{path, display, loaded, lines}]: what the host says it loaded, plus the repo's CLAUDE.md, AGENTS.md and always-on Cursor rules."""
    files = {}

    def add(path, loaded, content=None):
        real = os.path.realpath(os.path.expanduser(path))
        if real in files and content is None:
            files[real]["loaded"] |= loaded
            return
        if content is None:
            try:
                content = Path(real).read_bytes()[:MAX_FILE_BYTES].decode("utf-8", "replace")
            except OSError:
                return
        else:
            content = content[:MAX_FILE_BYTES]               # what the host recorded as loaded wins over the file on disk
        files[real] = {"display": display_path(real, repo), "loaded": loaded or files.get(real, {}).get("loaded", False),
                       "lines": content.split("\n")}

    for ev in events:                                        # the main session and each subagent
        info = ev.get("instruction")
        if info:
            contents = info.get("contents") or [None] * len(info["paths"])
            for path, content in zip(info["paths"], contents):
                add(path, True, content)
    for name in ("CLAUDE.md", "AGENTS.md"):
        if (repo / name).is_file():
            add(str(repo / name), False)
    global_md = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "CLAUDE.md"
    if global_md.is_file():
        add(str(global_md), False)
    for rule in sorted((repo / ".cursor" / "rules").glob("*")) if (repo / ".cursor" / "rules").is_dir() else []:
        if rule.is_file():
            head = rule.read_bytes()[:MAX_FILE_BYTES].decode("utf-8", "replace").split("\n")
            if str(frontmatter(head)[0].get("alwaysApply", "")).lower() == "true":
                add(str(rule), False)
    return list(files.values())


def listed_descriptions(events):
    """{name: description} from the skill lists the host printed for the main agent (first one wins)."""
    found = {}
    for ev in events:
        if ev["actor"] == "main":
            for name, desc in (ev.get("descriptions") or {}).items():
                found.setdefault(name, desc)
    return found


def named_in_files(listed_name, file_names):
    """Does a skill file carry this listed name, either exactly or as the part after a plugin prefix?"""
    return listed_name in file_names or listed_name.split(":")[-1] in file_names


def named_in(name, names):
    return name in names or any(n.endswith(":" + name) for n in names)


def hook_summary(mapping, events, repo):
    """Hooks from the files the mapping declares: event, matcher, first word of the command, and how many of the
    session's tool calls the matcher matches. Never the command itself."""
    if not mapping.get("hook_files"):
        return {"unknown": "hooks unknown for this host"}
    calls = [c for ev in events for c in ev["calls"]]
    files, hooks = 0, []
    for path, scope in skill_dirs(mapping["hook_files"], repo):
        if not path.is_file():
            continue
        files += 1
        try:
            groups = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
        except (OSError, ValueError, AttributeError):
            continue
        for event, entries in sorted(groups.items()) if isinstance(groups, dict) else []:
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                matcher = entry.get("matcher") if isinstance(entry.get("matcher"), str) else ""
                for hook in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
                    if not isinstance(hook, dict):
                        continue
                    command = hook.get("command") if isinstance(hook.get("command"), str) else ""
                    first = command.split()[0] if command.split() else str(hook.get("type", "hook"))
                    item = {"event": event, "matcher": matcher, "command": Path(first).name or first, "scope": scope,
                            "matched": None, "of_all_calls": None, "share_pct": None, "summary": None, "every_call_of": []}
                    if event in ("PreToolUse", "PostToolUse"):
                        def hits(name):
                            if matcher in ("", "*"):
                                return True
                            try:
                                return re.fullmatch(matcher, name) is not None
                            except re.error:
                                return False
                        matched = [c for c in calls if hits(c["name"])]
                        item.update(matched=len(matched), of_all_calls=len(calls),
                                    share_pct=round(100 * len(matched) / len(calls)) if calls else 0)
                        by_cat = {}
                        for c in calls:
                            by_cat.setdefault(call_cat(c), [0, 0])[0] += 1
                        for c in matched:
                            by_cat[call_cat(c)][1] += 1
                        top = max(by_cat.items(), key=lambda kv: (kv[1][1], kv[0]), default=None)
                        if top and top[1][1]:
                            item["summary"] = f"matched {top[1][1]} of {top[1][0]} {top[0]} calls"
                        names = {}
                        for c in calls:
                            names.setdefault(c["name"], []).append(c)
                        item["every_call_of"] = sorted(n for n, cs in names.items()
                                                       if hits(n) and call_cat(cs[0]) in COMMON_CATEGORIES)
                    hooks.append(item)
    hooks.sort(key=lambda h: (not h["every_call_of"], -(h["matched"] or 0), h["event"], h["matcher"], h["command"]))
    return {"files": files, "total": len(hooks), "top": hooks[:TOP_OVERLAPS], "more": max(0, len(hooks) - TOP_OVERLAPS)}


def overlaps_summary(events, mapping, repo, loaded, offered, thresholds):
    """The skills.overlaps section. The script only measures; the agent judges."""
    skill_threshold, directive_threshold = thresholds
    files, unreadable = skill_files(mapping.get("skill_folders"), mapping.get("plugin_skill_folders"), repo)
    loaded_names, offered_names = list(loaded), offered or []
    # Plugin folders hold every plugin's skills, installed or not: only the ones the host offered count.
    files = [f for f in files if not f["plugin"] or (offered is not None and named_in(f["name"], offered_names))]
    listed = listed_descriptions(events)
    file_names = {f["name"] for f in files}
    from_listing = [{"name": n, "description": d, "path": None, "display": None, "lines": [], "body_start": 0}
                    for n, d in listed.items() if not named_in_files(n, file_names)]
    corpus = files + from_listing

    def status(name):
        return "loaded this session" if named_in(name, loaded_names) else "offered" if named_in(name, offered_names) else "installed only"

    not_scanned = sum(1 for n in offered_names if not named_in_files(n, file_names) and n not in listed)
    out = {"scanned": {"skills": len(corpus), "from_files": len(files), "from_listing": len(from_listing),
                       "unreadable": unreadable, "not_scanned": not_scanned}}

    described = [f for f in corpus if f["description"]]
    vectors, surface = tfidf([f["description"] for f in described], bigrams=False, top=DESCRIPTION_TERMS)
    pairs = [(described[i], described[j], score, shared) for i, j, score, shared in similar_pairs(vectors, vectors, skill_threshold, surface, same=True)
             if not (described[i]["path"] and described[i]["path"] == described[j]["path"])]
    out["skill_pairs"] = {"threshold": skill_threshold, "total": len(pairs), "more": max(0, len(pairs) - TOP_OVERLAPS),
                          "top": [{"a": a["name"], "b": b["name"], "score_pct": round(100 * s), "shared_terms": t,
                                   "a_status": status(a["name"]), "b_status": status(b["name"])} for a, b, s, t in pairs[:TOP_OVERLAPS]]}
    by_name = {}
    for f in files:
        by_name.setdefault(f["name"], []).append(f["display"])
    dups = sorted((n, sorted(p)) for n, p in by_name.items() if len(p) > 1)
    out["duplicate_names"] = {"total": len(dups), "more": max(0, len(dups) - TOP_OVERLAPS),
                              "top": [{"name": n, "paths": p[:5]} for n, p in dups[:TOP_OVERLAPS]]}

    instructions = always_on_files(events, repo)
    skill_lines = [(f, n, t) for f in files for n, t in directive_lines(f["lines"], f["body_start"])]
    inst_lines = [(f, n, t) for f in instructions for n, t in directive_lines(f["lines"])]
    out["scanned"].update(instruction_files=len(instructions), directive_lines={"skills": len(skill_lines), "instructions": len(inst_lines)})
    dvec, dsurface = tfidf([t for _, _, t in skill_lines] + [t for _, _, t in inst_lines])
    left, right = dvec[:len(skill_lines)], dvec[len(skill_lines):]
    stats = {}
    matches = similar_pairs(left, right, directive_threshold, dsurface, stats=stats)
    top, eligible = [], 0
    for i, j, score, shared in matches:
        sf, sn, _ = skill_lines[i]
        inf, inn, _ = inst_lines[j]
        both = status(sf["name"]) == "loaded this session" and inf["loaded"]
        eligible += both
        if len(top) < TOP_OVERLAPS:
            top.append({"skill": sf["name"], "skill_line": sn, "instruction": inf["display"], "instruction_line": inn,
                        "score_pct": round(100 * score), "shared_terms": shared, "both_loaded": bool(both),
                        "judge": "eligible" if both else "static"})
    out["directive_pairs"] = {"threshold": directive_threshold, "total": len(matches), "eligible": eligible,
                              "static": len(matches) - eligible, "more": max(0, len(matches) - TOP_OVERLAPS),
                              "best_pct": round(100 * min(1.0, stats.get("best", 0.0))), "top": top}
    listed_files = sorted(({"path": f["display"], "loaded": bool(f["loaded"]), "directive_lines": len(directive_lines(f["lines"]))} for f in instructions),
                          key=lambda f: (not f["loaded"], f["path"]))
    out["instruction_files"] = {"total": len(listed_files), "loaded": sum(f["loaded"] for f in listed_files),
                                "more": max(0, len(listed_files) - TOP_OVERLAPS), "top": listed_files[:TOP_OVERLAPS]}
    out["hooks"] = hook_summary(mapping, events, repo)
    return out


def host_instruction_files(events):
    """Always-on rule files the host says it loaded (paths masked), and the bytes it reports for them."""
    files, seen, total = [], set(), 0
    for ev in events:
        info = ev.get("instruction")
        if ev["actor"] != "main" or not info:
            continue
        total = max(total, info["bytes"])
        for path in info["paths"]:
            if path not in seen:
                seen.add(path)
                files.append({"path": mask_home(path), "bytes": None, "always_on": info["always_on"]})
    return {"files": files, "bytes": total} if files else None


def skills_summary(events, host, repo, offered=None, folders=None, mapping=None, thresholds=None):
    mapping = mapping or {}
    results = {r["id"]: r for e in events for r in e["results"] if r["id"] is not None}
    loaded, reads, detections = {}, [], {}
    tool_label = mapping.get("skill_tool_label", "skill-tool")
    for name, how, size, turn in invoked_skills(events, host):
        loaded.setdefault(name, {"name": name, "detected_by": how, "bytes": size})
        detections.setdefault(name, []).append({"by": how, "turn": turn})
    for ev in events:
        for c in ev["calls"]:
            cat = call_cat(c)
            if cat == "skill" and isinstance(c["input"], dict) and c["input"].get("skill"):
                skill = c["input"]["skill"]
                loaded.setdefault(skill, {"name": skill, "detected_by": tool_label, "bytes": None})
                detections.setdefault(skill, []).append({"by": tool_label, "turn": ev["turn"]})
            if cat != "read":
                continue
            for path in call_path(c):
                parts = tuple(p for p in re.split(r"[\\/]+", path) if p)
                if "skills" not in parts or parts.index("skills") + 1 >= len(parts) - 1:
                    continue
                res = results.get(c["id"])
                reads.append((parts[parts.index("skills") + 1], parts[-1] == "SKILL.md" and parts[-2] == parts[parts.index("skills") + 1],
                              res["bytes"] if res else 0))
    for name, is_skill_md, _ in reads:         # only a read of <name>/SKILL.md means the skill was used
        if is_skill_md:
            loaded.setdefault(name, {"name": name, "detected_by": "read SKILL.md", "bytes": 0})
    for name, _, size in reads:                # other files of a used skill add to its size
        if name in loaded:
            loaded[name]["bytes"] = (loaded[name]["bytes"] or 0) + size
    if mapping.get("skill_detections") == "all":      # say every way a skill was seen, with its turn
        for name, found in detections.items():
            loaded[name]["detections"] = found
    inventory = skill_inventory(folders, repo)
    no_folders = inventory is None
    inventory = inventory or []
    sizes = {i["name"]: i["bytes"] for i in inventory}
    for entry in loaded.values():
        if entry["bytes"] is None:
            entry["bytes"] = sizes.get(entry["name"])
    shown = inventory
    if len(inventory) > TOP_ITEMS:       # keep the largest always-on descriptions
        shown = sorted(inventory, key=lambda i: (-i["description_chars"], i["name"]))[:TOP_ITEMS]
    summary = {"loaded": list(loaded.values())[:TOP_LOADED_SKILLS], "loaded_total": len(loaded),
               "used": list(loaded)[:TOP_LOADED_SKILLS]}
    if offered is None:
        summary["offered"] = {"not_measurable": "the host records no list of skills offered to the model"}
    else:
        summary["offered"] = {"total": len(offered), "names": offered[:TOP_OFFERED]}
        summary["used_not_offered"] = [n for n in loaded if not is_offered(n, offered)][:TOP_LOADED_SKILLS]
        summary["offered_note"] = "a skill used but not offered is normal for explicit-only skills"
    summary["_inventory_names"] = None if no_folders else [i["name"] for i in inventory]     # removed before output
    summary.update(inventory=shown, inventory_total=None if no_folders else len(inventory),
                   instruction_files=instruction_files(repo))
    if thresholds is not None:
        summary["overlaps"] = overlaps_summary(events, {**mapping, "skill_folders": folders}, repo, loaded, offered, thresholds)
    host_rules = host_instruction_files(events)
    if host_rules:
        known = {f["path"] for f in summary["instruction_files"]}
        summary["instruction_files"] += [f for f in host_rules["files"] if f["path"] not in known]
        if host_rules["bytes"]:
            summary["instruction_bytes_always_on"] = host_rules["bytes"]
    if no_folders:
        summary["inventory_note"] = NO_FOLDERS
    return summary


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


def degraded_report(host, repo, searched=None, folders=None):
    report = {"mode": "degraded", "confidence_cap": "medium",
              "evidence_rule": "No usable transcript. The agent's own recollection is never evidence; "
                               "only the working tree, the diff and repo records below count.",
              "git": working_tree_report(repo),
              "skills": {"inventory": skill_inventory(folders or builtin_folders(host), repo) or [],
                         "instruction_files": instruction_files(repo)}}
    if searched is not None:
        report.update(transcript_found=False, searched=searched)
    return report


def load_config(repo):
    try:
        return tomllib.loads((repo / ".workflow-review.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---- report: judgments from the agent, every number from the metrics run ----------------------------------------

EXIT_FINDINGS = 7
IMPACT, EFFORT, RISK, CONFIDENCE = ("high", "medium", "low"), ("low", "medium", "high"), ("low", "medium", "high"), ("high", "medium", "low")
VERDICTS = ("keep", "improve", "merge", "gap", "overlap", "none")
METRIC_IDS = ("calls", "output", "cache", "fresh", "context", "tools", "time", "verify", "subagents", "interruptions", "tokens")
FINDINGS_KEYS = ("schema_version", "title", "bottom_line", "actions", "skills", "closing_question", "metric_notes")
ACTION_KEYS = ("title", "impact", "effort", "risk", "confidence", "why", "what")
APPLIES_TO = ("all hosts", "claude-code", "codex", "cursor", "devin", "kiro")
LIMIT = {"title": 80, "bottom_line": 300, "action_title": 100, "text": 300, "evidence": 240, "skill_name": 60,
         "skill_evidence": 240, "closing": 300, "note": 120, "actions": 5, "evidence_items": 3, "skills": 20, "overlaps": 5}
MD_LINES = 80


BOLD_RE = re.compile(r"\*\*.+?\*\*")


def _text(errors, path, value, limit):
    if not isinstance(value, str):
        errors.append(f"{path}: must be text")
    elif not value.strip():
        errors.append(f"{path}: must not be empty")
    elif len(value) > limit:
        errors.append(f"{path}: {len(value)} characters, the limit is {limit}")
    elif BOLD_RE.search(value) and path != "bottom_line":
        errors.append(f"{path}: **emphasis** is only allowed in bottom_line")
    elif path == "bottom_line" and len(BOLD_RE.findall(value)) > 1:
        errors.append("bottom_line: at most one **emphasis** span is allowed")


def _object(errors, path, value, required, optional=()):
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return False
    errors += [f"{path}: unknown key '{k}' (allowed: {', '.join((*required, *optional))})" for k in value if k not in (*required, *optional)]
    errors += [f"{path}: missing key '{k}'" for k in required if k not in value]
    return True


def _enum(errors, path, value, allowed):
    if value not in allowed:
        errors.append(f"{path}: must be one of {', '.join(allowed)} (got {value!r})")


def validate_findings(data):
    """Every problem in the agent's findings, as readable lines; an empty list means they are valid."""
    errors = []
    if not _object(errors, "findings", data, ("schema_version", "title", "bottom_line", "actions"), FINDINGS_KEYS[4:]):
        return errors
    if data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
        errors.append(f"schema_version: must be 1 (got {data.get('schema_version')!r})")
    if "title" in data:
        _text(errors, "title", data["title"], LIMIT["title"])
    if "bottom_line" in data:
        _text(errors, "bottom_line", data["bottom_line"], LIMIT["bottom_line"])
    actions = data.get("actions", [])
    if not isinstance(actions, list):
        errors.append("actions: must be a list")
        actions = []
    elif len(actions) > LIMIT["actions"]:
        errors.append(f"actions: {len(actions)} items, the limit is {LIMIT['actions']}")
    ranks = []
    for i, action in enumerate(actions):
        at = f"actions[{i}]"
        if not _object(errors, at, action, ACTION_KEYS, ("applies_to",)):
            continue
        if "applies_to" in action and action["applies_to"] not in APPLIES_TO:
            errors.append(f"{at}.applies_to: must be one of {', '.join(APPLIES_TO)} (got {action['applies_to']!r})")
        if "title" in action:
            _text(errors, f"{at}.title", action["title"], LIMIT["action_title"])
        for key, allowed in (("impact", IMPACT), ("effort", EFFORT), ("risk", RISK), ("confidence", CONFIDENCE)):
            if key in action:
                _enum(errors, f"{at}.{key}", action[key], allowed)
        if action.get("impact") in IMPACT:
            ranks.append((i, IMPACT.index(action["impact"])))
        if "why" in action and _object(errors, f"{at}.why", action["why"], ("cause", "cost", "evidence")):
            for key in ("cause", "cost"):
                if key in action["why"]:
                    _text(errors, f"{at}.why.{key}", action["why"][key], LIMIT["text"])
            ev = action["why"].get("evidence")
            if "evidence" in action["why"]:
                if not isinstance(ev, list):
                    errors.append(f"{at}.why.evidence: must be a list")
                else:
                    if len(ev) > LIMIT["evidence_items"]:
                        errors.append(f"{at}.why.evidence: {len(ev)} items, the limit is {LIMIT['evidence_items']}")
                    for j, item in enumerate(ev):
                        _text(errors, f"{at}.why.evidence[{j}]", item, LIMIT["evidence"])
        if "what" in action and _object(errors, f"{at}.what", action["what"], ("change", "safe_because", "watch_for")):
            for key in ("change", "safe_because", "watch_for"):
                if key in action["what"]:
                    _text(errors, f"{at}.what.{key}", action["what"][key], LIMIT["text"])
    for (_, before), (i, after) in zip(ranks, ranks[1:]):
        if after < before:
            errors.append(f"actions[{i}].impact: {actions[i]['impact']} comes after a lower impact; order actions high, then medium, then low")
    skills = data.get("skills", [])
    if not isinstance(skills, list):
        errors.append("skills: must be a list")
        skills = []
    elif len(skills) > LIMIT["skills"]:
        errors.append(f"skills: {len(skills)} items, the limit is {LIMIT['skills']}")
    for i, skill in enumerate(skills):
        at = f"skills[{i}]"
        if not _object(errors, at, skill, ("name", "verdict", "evidence"), ("overlaps_with",)):
            continue
        if "name" in skill:
            _text(errors, f"{at}.name", skill["name"], LIMIT["skill_name"])
            if isinstance(skill["name"], str) and skill["name"].strip().lower() == "other installed skills":
                errors.append(f"{at}.name: the report adds the 'other installed skills' row itself; leave it out")
        if "verdict" in skill:
            _enum(errors, f"{at}.verdict", skill["verdict"], VERDICTS)
        if "evidence" in skill:
            _text(errors, f"{at}.evidence", skill["evidence"], LIMIT["skill_evidence"])
        if "overlaps_with" in skill:
            names = skill["overlaps_with"]
            if not isinstance(names, list) or len(names) > LIMIT["overlaps"]:
                errors.append(f"{at}.overlaps_with: must be a list of at most {LIMIT['overlaps']} names")
            else:
                for j, name in enumerate(names):
                    _text(errors, f"{at}.overlaps_with[{j}]", name, LIMIT["skill_name"])
    if "closing_question" in data:
        _text(errors, "closing_question", data["closing_question"], LIMIT["closing"])
        if not any(isinstance(s, dict) and s.get("verdict") == "gap" for s in skills):
            errors.append("closing_question: allowed only when at least one skill has the verdict gap")
    notes = data.get("metric_notes")
    if "metric_notes" in data:
        if not isinstance(notes, dict):
            errors.append("metric_notes: must be an object")
        else:
            for key, value in notes.items():
                if key not in METRIC_IDS:
                    errors.append(f"metric_notes: unknown metric '{key}' (allowed: {', '.join(METRIC_IDS)})")
                else:
                    _text(errors, f"metric_notes.{key}", value, LIMIT["note"])
    return errors


def read_findings(stream):
    """(findings, errors). Not JSON is an error like any other."""
    try:
        data = json.loads(stream.read())
    except ValueError as exc:
        return None, [f"findings must be one JSON object: {exc}"]
    errors = validate_findings(data)
    return (None if errors else data), errors


NOT_MEASURABLE = "not measurable"


def fmt_tokens(n):
    if n is None:                       # a missing number is never shown as 0
        return NOT_MEASURABLE
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 100_000:
        return f"{round(n / 1000)}K"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def fmt_pct(p):
    return "under 0.1%" if 0 < p < 0.1 else f"{p:.1f}%"


def fmt_secs(s):
    """Seconds below 10 minutes, minutes up to an hour, then hours with one decimal."""
    s = int(s)
    return f"{s} s" if s < 600 else f"{s / 60:.0f} min" if s < 3600 else f"about {s / 3600:.1f} h"


HOST_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "cursor": "Cursor", "devin": "Devin", "kiro": "Kiro"}


def scope_line(out, now):
    cov, win = out["coverage"], out["window"]
    host = HOST_NAMES.get(cov["host"], str(cov["host"]).title())
    lo, total = win["from_turn"], win["turns_total"]
    if total is None:
        span = f"Turns from {lo}, count not measurable,"
    else:
        span = "No turns" if not total else f"Turn {lo}" if total == 1 else f"Turns {lo}–{lo + total - 1}"
    label = f" ({cov['label']} adapter)" if cov.get("label") else ""
    return f"{span} of one {host} session{label}, reviewed on {now.day} {now:%b %Y}"


def at_least(n, lower):
    """A token count, marked "at least" when some records lacked its field (the total is a lower bound)."""
    return f"at least {fmt_tokens(n)}" if lower and n is not None else fmt_tokens(n)


BAR_KEY = {"read": "cache_read", "write": "cache_write", "output": "output", "fresh": "fresh_input"}


def token_parts(out):
    t = out["tokens"]
    if "not_measurable" in t:
        return None
    gone = set(t.get("not_recorded", []))
    parts = [(k, label, t[key]) for k, label, key in (("read", "Cache read", "cache_read"), ("write", "Cache write", "cache_write"),
                                                      ("output", "Output", "output"), ("fresh", "Fresh input", "fresh_input"))
             if key not in gone]                          # a field the host never records is left out, not a 0% slice
    if any(v is None for _, _, v in parts):            # a part is not measurable: no composition can be drawn
        return None
    total = sum(v for _, _, v in parts)
    return [(k, label, v, 100.0 * v / total) for k, label, v in parts], total


def metric_rows(out):
    """(id, label, value) rows; every value is computed here from the metrics run."""
    t, rows, gone = out["tokens"], [], not_recorded_words(out)
    lower = set(t.get("lower_bound", []))
    tok = lambda k: gone[k] if k in gone else at_least(t[k], k in lower)
    if "not_measurable" in t:
        rows.append(("tokens", "Tokens", "not measurable on this host"))
    else:
        rows += [("calls", "API calls", NOT_MEASURABLE if t["api_calls"] is None else str(t["api_calls"])),
                 ("output", "Output tokens", tok("output")),
                 ("cache", "Cache read / write", f"{tok('cache_read')} / {tok('cache_write')}"),
                 ("fresh", "Fresh input", tok("fresh_input"))]
        if t.get("context_at_invocation") is not None:
            rows.append(("context", "Context at invocation", fmt_tokens(t["context_at_invocation"])))
    tools = out["tools"]
    shell = tools["by_category"].get("shell")
    errs = tools["errors"]
    rows.append(("tools", "Tool calls", f"{tools['total']}" + (f" ({shell} shell)" if shell else "")
                 + (f", {errs} error{'' if errs == 1 else 's'}" if errs is not None else ", errors not measurable")))
    tm_ = out["time"]
    if "not_measurable" in tm_:
        value = "not measurable on this host"
    elif "tool_s" in gone:
        value = f"{fmt_secs(tm_['active_s'])} active (tools included), tool time {gone['tool_s']}, {fmt_secs(tm_['waiting_s'])} waiting on you"
    elif "active_s" in tm_:
        value = f"{fmt_secs(tm_['active_s'])} active, {fmt_secs(tm_['tool_s'])} in tools, {fmt_secs(tm_['waiting_s'])} waiting on you"
    else:
        value = coarse_time_view(tm_)["wall"]                    # minutes and their range, never a raw number of seconds
        value += ", " + tm_["precision"].rsplit("; ", 1)[-1]
    rows.append(("time", "Time", value))
    v = out["verify"]
    rows.append(("verify", "Verify runs", f"{v['total']}" + (f" ({v['before_last_edit']} before the last edit)" if v["before_last_edit"] else "")))
    subs = sum(a.get("actors", 1) for a in out["actors"] if a["actor"] != "main")
    rows.append(("subagents", "Subagents", str(subs) if subs else "none"))
    if isinstance(out["interruptions"], dict) and out["interruptions"].get("count"):
        rows.append(("interruptions", "Interruptions", str(out["interruptions"]["count"])))
    return rows


METRIC_NOUN = {"turns": "turn counts", "tool_calls": "tool call counts", "tokens": "token totals", "errors": "error counts",
               "verify": "verify run counts", "interruptions": "interruption counts", "time": "times", "skills": "skill counts",
               "subagents": "subagent counts"}


def health_phrases(row):
    """A health row that needs attention, in plain words: which number, what went wrong, the two values."""
    noun, out = METRIC_NOUN.get(row["metric"], row["metric"]), []
    for c in row.get("checks", []):
        if c.get("passed"):
            continue
        num = lambda v: NOT_MEASURABLE if v is None else f"{v:,}" if isinstance(v, int) else (f"{v:,.1f}" if isinstance(v, float) else str(v))
        if c["layer"] == "second count":
            out.append(f"{noun} couldn't be cross-checked: {num(c['first'])} vs {num(c['second'])}")
        elif c["name"] == "unseen record type":
            out.append(f"a record type this version of the skill doesn't know: {c['type']} ({num(c['first'])} record"
                       f"{'' if c['first'] == 1 else 's'}; may affect {noun})")
        elif c["name"] == "coverage":
            out.append(f"only {num(c['first'])}% of records recognised")
        elif "missing" in c["name"]:
            out.append(c["name"])
        else:
            out.append(f"{noun} missing: {c['name']}" if row["status"] == "broken" else f"{noun}: {c['name']}")
    return list(dict.fromkeys(out)) or [f"{noun}: {row['status']}"]


def coverage_items(out):
    cov, items = out["coverage"], []
    if not cov["drift"]["ok"] and cov["drift"].get("warning"):
        items.append(cov["drift"]["warning"])
    for r in cov.get("health", {}).get("rows", []):
        if r["status"] in SEVERITY:
            items += health_phrases(r)
    items += cov.get("warnings", [])
    if cov.get("version_note"):
        items.append(cov["version_note"])
    if cov.get("not_measurable"):
        host = HOST_NAMES.get(cov["host"], str(cov["host"]).title())
        items.append(f"{host} doesn't record: " + ", ".join(cov["not_measurable"]))
    return items


VERDICT_LABEL = {"keep": "Keep", "improve": "Improve", "merge": "Merge or cut", "gap": "Gap", "overlap": "Overlaps", "none": "No verdict"}


def skill_rows(findings):
    """(label, verdict, evidence, overlaps) with gaps numbered G1, G2, ... in order."""
    rows, gap = [], 0
    for s in findings.get("skills", []):
        name = s["name"]
        if s["verdict"] == "gap":
            gap += 1
            name = f"G{gap}: {name}"
        rows.append((name, s["verdict"], s["evidence"], s.get("overlaps_with", [])))
    return rows


def applies_label(action):
    value = action.get("applies_to")
    return None if value is None else "All hosts" if value == "all hosts" else HOST_NAMES[value]


def other_skill_count(names, findings):
    """Installed skills the findings do not mention (None when the inventory is unknown or all are mentioned)."""
    if names is None:
        return None
    mentioned = {s["name"] for s in findings.get("skills", [])}
    left = sum(1 for n in names if n not in mentioned)
    return left or None


def other_skill_text(count):
    return f"Not exercised in this task ({count} skill{'' if count == 1 else 's'})"


def flags(action):
    return [w for w, on in (("High risk", action["risk"] == "high"), ("Low confidence", action["confidence"] == "low"),
                            ("High effort", action["effort"] == "high")) if on]


def clean_findings(findings, do_redact):
    """Findings with every string redacted like the metrics, newlines folded to spaces."""
    def walk(v):
        if isinstance(v, str):
            v = " ".join(v.split())
            return redact(v) if do_redact else v
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    return walk(findings)


def _esc(text):
    from html import escape
    return escape(text, quote=True)


def _inline(text, bold=False):
    """Escape, then let `backticks` become <code> (and, where allowed, one **span** become <strong>)."""
    text = _esc(text)
    if bold:
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text, count=1)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


REPORT_CSS = """
:root{color-scheme:light;--page:#eef1f2;--sheet:#fcfcfb;--ink:#1d2733;--ink-2:#4a5562;--ink-3:#6b7580;--rule:#d9dee2;--code-bg:#f1f3f4;
--impact-high:#d03b3b;--impact-medium:#ec835a;--impact-low:#8a949e;--warn:#fab219;--warn-edge:#c98500;
--v-keep:#0ca30c;--v-improve:#fab219;--v-merge:#6250d6;--v-gap:#e87ba4;--v-overlap:#2a78d6;
--s-read:#2a78d6;--s-write:#eb6834;--s-output:#1baf7a;--s-fresh:#eda100;
--serif:Charter,"Bitstream Charter","Sitka Text",Cambria,Georgia,serif;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace}
@media (prefers-color-scheme: dark){:root{color-scheme:dark;--page:#121416;--sheet:#1a1a19;--ink:#eceae6;--ink-2:#c3c2b7;--ink-3:#9a9a92;
--rule:#34363a;--code-bg:#232427;--impact-low:#7d858c;--warn-edge:#fab219;--s-read:#3987e5;--s-write:#d95926;--s-output:#199e70;
--s-fresh:#c98500;--v-merge:#9085e9;--v-gap:#d55181;--v-overlap:#3987e5}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--ink);font:16px/1.55 var(--sans);font-variant-numeric:tabular-nums}
.sheet{max-width:52rem;margin:2.5rem auto;padding:2.5rem 3rem 3rem;background:var(--sheet);border-radius:6px;box-shadow:0 1px 0 var(--rule),0 0 0 1px var(--rule)}
@media (max-width:640px){.sheet{margin:0;padding:1.5rem 1.25rem 2rem;border-radius:0;box-shadow:none}}
code{font-family:var(--mono);font-size:.86em;background:var(--code-bg);padding:.08em .35em;border-radius:3px}
:focus-visible{outline:2px solid var(--s-read);outline-offset:2px;border-radius:3px}
header h1{font:600 1.5rem/1.25 var(--serif);margin:0 0 .35rem;letter-spacing:-.005em}
.scope{margin:0;color:var(--ink-2);font-size:.95rem}
.bottom-line{margin:2rem 0 1.25rem;font:400 1.42rem/1.42 var(--serif);max-width:38em}
figure.anatomy{margin:0 0 2.25rem}
.bar{display:flex;gap:2px;height:22px;width:100%}.bar span{display:block;height:100%;min-width:0}
.bar span:first-child{border-radius:4px 0 0 4px}.bar span:last-child{border-radius:0 4px 4px 0}
.seg-read{background:var(--s-read)}.seg-write{background:var(--s-write)}.seg-output{background:var(--s-output)}.seg-fresh{background:var(--s-fresh)}
.legend{display:flex;flex-wrap:wrap;gap:.4rem 1.4rem;margin:.7rem 0 0;padding:0;list-style:none;font-size:.88rem;color:var(--ink-2)}
.legend li{display:flex;align-items:baseline;gap:.45rem}.legend i{width:10px;height:10px;border-radius:2px;display:inline-block;transform:translateY(1px)}
.legend b{color:var(--ink);font-weight:600}
figcaption{margin-top:.4rem;font-size:.85rem;color:var(--ink-3)}
h2{font:600 1.12rem/1.3 var(--serif);margin:2.5rem 0 .9rem;padding-top:1.1rem;border-top:1px solid var(--rule)}
table.metrics{border-collapse:collapse;width:100%;font-size:.93rem}
table.metrics th,table.metrics td{text-align:left;padding:.42rem 0;border-bottom:1px solid var(--rule);vertical-align:top}
table.metrics th{font-weight:500;color:var(--ink-2);width:42%;padding-right:1rem}
table.metrics tr:last-child th,table.metrics tr:last-child td{border-bottom:0}
.muted{color:var(--ink-3)}
ol.actions{list-style:none;margin:0;padding:0;counter-reset:rank}
ol.actions>li{counter-increment:rank;position:relative;margin:0 0 1.25rem;padding:1.1rem 1.25rem .6rem 1.35rem;border:1px solid var(--rule);border-left:5px solid var(--impact,var(--impact-low));border-radius:5px}
ol.actions>li.impact-high{--impact:var(--impact-high)}ol.actions>li.impact-medium{--impact:var(--impact-medium)}ol.actions>li.impact-low{--impact:var(--impact-low)}
.action-head{display:flex;gap:.75rem;align-items:baseline}
.rank{font:600 1.05rem/1 var(--serif);color:var(--ink-3);min-width:1.1rem}.rank::before{content:counter(rank)}
.action-head h3{margin:0;font:600 1.08rem/1.35 var(--sans)}
.tags{display:flex;flex-wrap:wrap;gap:.35rem;margin:.6rem 0 .4rem 1.85rem;padding:0;list-style:none}
.tag{display:inline-flex;align-items:center;min-height:1.6rem;font-size:.78rem;line-height:1;padding:0 .55rem;border:1px solid var(--rule);border-radius:999px;color:var(--ink-2)}
.tag.impact{border-color:var(--impact);color:var(--ink);font-weight:600}
.tag.impact::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--impact);margin-right:.4rem}
.tag.warn{border:1.5px solid var(--warn-edge);color:var(--ink);font-weight:600}
.tag.warn::before{content:"!";display:inline-grid;place-items:center;width:.95rem;height:.95rem;margin-right:.35rem;border-radius:50%;background:var(--warn);color:#1d2733;font-size:.68rem;font-weight:700}
details{margin:.35rem 0 0 1.85rem;border-top:1px solid var(--rule)}details:first-of-type{border-top:0}
summary{cursor:pointer;list-style:none;padding:.55rem 0;font-weight:600;font-size:.95rem}
summary::-webkit-details-marker{display:none}
summary::before{content:"\\25B8";display:inline-block;width:1rem;color:var(--ink-3)}
details[open]>summary::before{content:"\\25BE"}
details ul{margin:0 0 .75rem;padding-left:1.1rem}details li{margin:.3rem 0;max-width:40em}
.label{font-weight:600}
ul.skills{list-style:none;margin:0;padding:0}
ul.skills li{display:grid;grid-template-columns:minmax(9rem,13rem) 8.5rem 1fr;gap:.25rem 1rem;padding:.55rem 0;border-bottom:1px solid var(--rule);font-size:.93rem}
ul.skills li:last-child{border-bottom:0}
.skill-name{font-family:var(--mono);font-size:.86rem;overflow-wrap:anywhere}
.verdict{display:flex;align-items:baseline;gap:.45rem;font-weight:600}
.verdict::before{content:"";width:9px;height:9px;border-radius:50%;background:var(--v);transform:translateY(1px);flex:none}
.v-keep{--v:var(--v-keep)}.v-improve{--v:var(--v-improve)}.v-merge{--v:var(--v-merge)}.v-gap{--v:var(--v-gap)}.v-overlap{--v:var(--v-overlap)}
.v-none{--v:transparent;font-weight:500;color:var(--ink-3)}.v-none::before{border:1px solid var(--ink-3)}
.skill-evidence{color:var(--ink-2)}
@media (max-width:640px){ul.skills li{grid-template-columns:1fr auto}.skill-evidence{grid-column:1/-1}}
.closing{margin:2rem 0 0;padding:1rem 1.2rem;border-radius:5px;background:var(--code-bg);font-size:.98rem}.closing p{margin:0}
@media print{body{background:#fff}.sheet{box-shadow:none;margin:0;max-width:none;padding:0}
details>summary::before{content:"";width:0}details:not([open])>*:not(summary){display:block}ol.actions>li{break-inside:avoid}}
.report-foot{margin:2.5rem 0 0;padding-top:1rem;border-top:1px solid var(--rule);font-size:.85rem}
.report-problem{color:var(--ink-3)}.report-problem.prominent{color:var(--ink);font-weight:600}
"""


def render_html(out, findings, now, inventory_names=None):
    """The report page. `findings` is already cleaned; `out` is the metrics run."""
    notes = findings.get("metric_notes", {})
    h = ["<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
         '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         f"<title>Workflow review: {_esc(findings['title'])}</title>", "<style>" + REPORT_CSS + "</style>", "</head>", "<body>",
         '<main class="sheet">', "<header>", f"<h1>Workflow review: {_inline(findings['title'])}</h1>",
         f'<p class="scope">{_esc(scope_line(out, now))}</p>', "</header>",
         f'<p class="bottom-line">{_inline(findings["bottom_line"], bold=True)}</p>']
    parts = token_parts(out)
    if parts and parts[1]:
        shown, total = parts
        lower = set(out["tokens"].get("lower_bound", []))
        h.append('<figure class="anatomy" aria-labelledby="anatomy-cap">')
        label = ", ".join(f"{name.lower()} {fmt_pct(p)}" for _, name, _, p in shown)
        h.append(f'<div class="bar" role="img" aria-label="Token composition: {_esc(label)}">')
        h += [f'<span class="seg-{k}" style="flex: {p:.4f}" title="{_esc(name)}: {fmt_tokens(v)} tokens ({fmt_pct(p)})"></span>'
              for k, name, v, p in shown if v]
        h.append("</div>")
        h.append('<ul class="legend">' + "".join(
            f'<li><i style="background: var(--s-{k})"></i>{_esc(name)} <b>{at_least(v, BAR_KEY[k] in lower)}</b> ({fmt_pct(p)})</li>'
            for k, name, v, p in shown) + "</ul>")
        small = " Output is too small to see on the bar." if next(p for k, _, _, p in shown if k == "output") < 1 else ""
        h.append(f'<figcaption id="anatomy-cap">Where this window’s {fmt_tokens(total)} tokens went.{small}</figcaption>')
        h.append("</figure>")
    h += ['<section aria-labelledby="h-metrics">', '<h2 id="h-metrics">Metrics</h2>', '<table class="metrics">', "<tbody>"]
    for rid, label, value in metric_rows(out):
        note = f' <span class="muted">({_esc(notes[rid])})</span>' if rid in notes else ""
        h.append(f'<tr><th scope="row">{_esc(label)}</th><td>{_esc(value)}{note}</td></tr>')
    cov = coverage_items(out)
    if cov:
        h.append('<tr><th scope="row">Coverage</th><td>' + "<br>".join(_esc(c) for c in cov) + "</td></tr>")
    h += ["</tbody>", "</table>", "</section>", '<section aria-labelledby="h-actions">',
          '<h2 id="h-actions">Do these, ranked by impact</h2>']
    if not findings["actions"]:
        h.append("<p>No changes recommended.</p>")
    else:
        h.append('<ol class="actions">')
        for a in findings["actions"]:
            h += [f'<li class="impact-{a["impact"]}">',
                  f'<div class="action-head"><span class="rank" aria-hidden="true"></span><h3>{_inline(a["title"])}</h3></div>',
                  '<ul class="tags" aria-label="Ratings">', f'<li class="tag impact">{a["impact"].capitalize()} impact</li>']
            for key, bad in (("effort", "high"), ("risk", "high"), ("confidence", "low")):
                cls = "tag warn" if a[key] == bad else "tag"
                h.append(f'<li class="{cls}">{a[key].capitalize()} {key}</li>')
            if applies_label(a):
                h.append(f'<li class="tag">{_esc(applies_label(a))}</li>')
            w, wh = a["what"], a["why"]
            h += ["</ul>", "<details>", "<summary>Why</summary>", "<ul>", f"<li>{_inline(wh['cause'])}</li>",
                  f'<li><span class="label">Cost:</span> {_inline(wh["cost"])}</li>']
            if wh["evidence"]:
                h.append('<li><span class="label">Evidence:</span> ' + "; ".join(_inline(e) for e in wh["evidence"]) + "</li>")
            h += ["</ul>", "</details>", "<details>", "<summary>What to do</summary>", "<ul>", f"<li>{_inline(w['change'])}</li>",
                  f'<li><span class="label">Safe because:</span> {_inline(w["safe_because"])}</li>',
                  f'<li><span class="label">Watch for:</span> {_inline(w["watch_for"])}</li>', "</ul>", "</details>", "</li>"]
        h.append("</ol>")
    h.append("</section>")
    rows = skill_rows(findings)
    other = other_skill_count(inventory_names, findings)
    if rows or other:
        h += ['<section aria-labelledby="h-skills">', '<h2 id="h-skills">Skills audit</h2>', '<ul class="skills">']
        for name, verdict, evidence, overlaps in rows:
            extra = " Overlaps with " + ", ".join(f"<code>{_esc(o)}</code>" for o in overlaps) + "." if overlaps else ""
            h += ["<li>", f'<span class="skill-name">{_esc(name)}</span>', f'<span class="verdict v-{verdict}">{VERDICT_LABEL[verdict]}</span>',
                  f'<span class="skill-evidence">{_inline(evidence)}{extra}</span>', "</li>"]
        if other:
            h += ["<li>", '<span class="skill-name muted">Other installed skills</span>', '<span class="verdict v-none">No verdict</span>',
                  f'<span class="skill-evidence">{other_skill_text(other)}</span>', "</li>"]
        h += ["</ul>", "</section>"]
    if findings.get("closing_question"):
        h.append(f'<aside class="closing" aria-label="Next step"><p>{_inline(findings["closing_question"])}</p></aside>')
    url = out["coverage"].get("issue_url")
    if url:                       # a plain link: the user opens a prefilled form, reads it, and submits it themselves
        cls = "report-problem prominent" if needs_issue(out) else "report-problem"
        h.append(f'<footer class="report-foot"><a class="{cls}" href="{_esc(url)}" target="_blank" rel="noopener noreferrer">'
                 "Report a problem with this review</a></footer>")
    h += ["</main>", "</body>", "</html>", ""]
    return "\n".join(h)


def render_report_md(out, findings, now, inventory_names=None):
    """The same report as markdown, at most MD_LINES lines."""
    lines = [f"# Workflow review: {findings['title']}", scope_line(out, now), f"**Bottom line:** {findings['bottom_line']}", "## Metrics"]
    notes = findings.get("metric_notes", {})
    lines += [f"- {label}: {value}" + (f" ({notes[rid]})" if rid in notes else "") for rid, label, value in metric_rows(out)]
    parts = token_parts(out)
    if parts and parts[1]:
        lower = set(out["tokens"].get("lower_bound", []))
        lines.append("- Token composition: " + ", ".join(f"{name.lower()} {at_least(v, BAR_KEY[k] in lower)} ({fmt_pct(p)})"
                                                         for k, name, v, p in parts[0]))
    lines += [f"- Coverage: {c}" for c in coverage_items(out)]
    if out["coverage"].get("issue_url") and needs_issue(out):
        lines.append(f"Report this problem: {out['coverage']['issue_url']}")
    lines.append("## Do these, ranked by impact")
    if not findings["actions"]:
        lines.append("No changes recommended.")
    for n, a in enumerate(findings["actions"], 1):
        warn = flags(a)
        lines.append(f"{n}. **{a['title']}**: {a['impact'].capitalize()} impact, {a['effort']} effort, {a['risk']} risk, "
                     f"{a['confidence']} confidence" + (f", {applies_label(a)}" if applies_label(a) else "")
                     + (f" (! {', '.join(warn)})" if warn else ""))
        lines += [f"   - Why: {a['why']['cause']}", f"   - Cost: {a['why']['cost']}"]
        if a["why"]["evidence"]:
            lines.append("   - Evidence: " + "; ".join(a["why"]["evidence"]))
        lines += [f"   - Change: {a['what']['change']}", f"   - Safe because: {a['what']['safe_because']}",
                  f"   - Watch for: {a['what']['watch_for']}"]
    rows = skill_rows(findings)
    tail = [f"> {findings['closing_question']}"] if findings.get("closing_question") else []
    other = other_skill_count(inventory_names, findings)
    if rows or other:
        lines.append("## Skills audit")
        skill_lines = [f"- {name}: {VERDICT_LABEL[v]}. {ev}" + (f" Overlaps with {', '.join(o)}." if o else "") for name, v, ev, o in rows]
        if other:
            skill_lines.append(f"- Other installed skills: No verdict. {other_skill_text(other)}")
        room = MD_LINES - len(lines) - len(tail)
        if len(skill_lines) > room:
            hidden = len(skill_lines) - (room - 1)
            skill_lines = skill_lines[:room - 1] + [f"- (+{hidden} more skills not shown)"]
        lines += skill_lines
    return "\n".join((lines + tail)[:MD_LINES]) + "\n"


def write_report(text):
    """<tempdir>/workflow-reviewer/review-<UTC>.html, file mode 0600 inside a 0700 folder. Returns the path."""
    base = Path(tempfile.gettempdir()) / "workflow-reviewer"
    fresh = not base.exists()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if fresh:
        os.chmod(base, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n = 0
    while True:
        path = base / f"review-{stamp}{f'-{n + 1}' if n else ''}.html"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(path, 0o600)
    return path


def can_open(env=None, platform=None):
    """Open the report only on a desktop: not over SSH, not in CI, and on Linux only with a display."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if any(env.get(k) for k in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT", "CI", "GITHUB_ACTIONS")):
        return False
    if platform.startswith("linux") and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return False
    return True


def open_file(path):
    """The platform opener, without a shell."""
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=False)


def report_main(out, findings, args, redact_on, inventory_names=None):
    """Render the report for this metrics run. Returns 0."""
    now = datetime.now(timezone.utc)
    findings = clean_findings(findings, redact_on)
    if args.report_format == "md":
        sys.stdout.write(render_report_md(out, findings, now, inventory_names))
        return 0
    path = write_report(render_html(out, findings, now, inventory_names))
    if not args.no_open and can_open():
        try:
            open_file(str(path))
        except (OSError, subprocess.SubprocessError, AttributeError):
            pass                                          # a missing opener is not an error; the path is printed
    top = findings["actions"][0]["title"] if findings["actions"] else None
    if args.format == "json":
        print(json.dumps({"bottom_line": findings["bottom_line"], "top_action": top, "report_path": str(path)}, ensure_ascii=False, indent=2))
    else:
        print(f"Bottom line: {findings['bottom_line']}\nTop action: {top or 'none'}\nReport: {path}")
    return 0


# ---- cli ------------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="transcript_metrics.py")
    p.add_argument("paths", nargs="*")
    p.add_argument("--host", default="auto", help="auto, a built-in host or a user adapter's host")
    p.add_argument("--format", default="md", choices=["md", "json"])
    p.add_argument("--verify-pattern", action="append", default=[], dest="verify_patterns")
    p.add_argument("--list-turns", action="store_true")
    p.add_argument("--all", action="store_true", help="with --list-turns: list every turn")
    p.add_argument("--from-turn", type=int)
    p.add_argument("--to-turn", type=int)
    p.add_argument("--events")
    p.add_argument("--sample", type=int)
    p.add_argument("--session-id")
    p.add_argument("--mapping")
    p.add_argument("--drift-threshold", type=float)
    p.add_argument("--no-redact", action="store_true")
    p.add_argument("--idle-threshold", type=float)
    p.add_argument("--make-replica", action="store_true", help="write a synthetic copy of this session (no content) to the temp folder")
    p.add_argument("--pr-bundle", action="store_true", help="print the adapter and replica paths, expected numbers and the commands for a pull request (runs nothing)")
    p.add_argument("--probe", metavar="PATH", help="print the structure of a file, folder or database (no content) and exit")
    p.add_argument("--save-adapter", metavar="NAME", help="read a drafted mapping from stdin, check it against this session, save it as a user adapter")
    p.add_argument("--replace", action="store_true", help="with --save-adapter: overwrite an adapter of the same name")
    p.add_argument("--health-all", action="store_true", help="show every row of coverage.health, not only those needing attention")
    p.add_argument("--overlap-threshold", type=float, default=0.15, help="skill description similarity to report (0 to 1)")
    p.add_argument("--directive-threshold", type=float, default=0.45, help="directive line similarity to report (0 to 1)")
    p.add_argument("--report", metavar="-", help="read the agent's findings JSON from stdin and render the report")
    p.add_argument("--report-format", default="html", choices=["html", "md"])
    p.add_argument("--no-open", action="store_true", help="with --report: do not open the HTML file")
    return p


def read_lines(path):
    return path.read_text(encoding="utf-8").split("\n")      # JSONL: only \n ends a record


# ---- storage readers: how a host keeps its records, apart from how the mapping reads them -----------------

CONTAINER_KINDS = ("jsonl", "sqlite", "json_document")
RESERVED_KINDS = ()


def container_kind(raw):
    return (raw.get("container") or {}).get("kind", "jsonl")


def container_error(kind):
    if kind in RESERVED_KINDS:
        return f"mapping container kind '{kind}' is reserved but not supported yet ({', '.join(CONTAINER_KINDS)})"
    if kind not in CONTAINER_KINDS:
        return f"mapping container kind '{kind}' is not supported ({', '.join(CONTAINER_KINDS)})"
    return None


class Source:
    """One stream of records: the main conversation or one subagent's."""

    def __init__(self, lines, label, main, stem=None, file=None, link=None):
        self.lines, self.label, self.main, self.stem, self.file, self.link = lines, label, main, stem, file, link


class JsonlReader:
    """Session files: a JSONL transcript, its subagent files, a session folder."""
    kind = "jsonl"

    def __init__(self, paths):
        self.paths = paths

    def first_lines(self):
        return read_lines(self.paths[0])

    def sources(self, mapping):
        seen = set()
        for path in self.paths:
            own_sub = is_subagent_file(path, read_lines(path)[0], mapping)
            for file in [path, *expand_subagents(path)]:
                if file.resolve() in seen:
                    continue                            # e.g. a subagent passed and also found beside its parent
                seen.add(file.resolve())
                main_file = file is path and not own_sub
                yield Source(read_lines(file), str(file), main_file, file.stem, file)


class ContainerProblem(Exception):
    """A mapping or database that cannot be read: reported as exit 2 (drift)."""

    def __init__(self, message, missing=None):
        super().__init__(message)
        self.missing = missing


SQLITE_TABLES = {
    "sessions": ("table", "id", "cwd", "created", "last_activity", "head"),
    "nodes": ("table", "session", "id", "parent", "payload", "created"),
    "subagent_heads": ("table", "session", "agent", "head"),          # optional
}
SQLITE_OPTIONAL = {"sessions": ("model",)}                  # columns a mapping may add: the session's model
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sqlite_spec(raw):
    """The mapping's table and column names, each checked to be a plain identifier before anything is opened."""
    cfg = raw.get("container") or {}
    spec = {}
    for name, keys in SQLITE_TABLES.items():
        part = cfg.get(name)
        if part is None:
            if name == "subagent_heads":
                continue
            raise ContainerProblem(f"mapping [container.{name}] is missing")
        for key in keys:
            value = part.get(key)
            if value is None:
                raise ContainerProblem(f"mapping [container.{name}] is missing '{key}'")
            if not isinstance(value, str) or not IDENT_RE.match(value):
                raise ContainerProblem(f"mapping [container.{name}] {key} is not a plain identifier; refusing to query")
        for key in SQLITE_OPTIONAL.get(name, ()):
            if key in part and (not isinstance(part[key], str) or not IDENT_RE.match(part[key])):
                raise ContainerProblem(f"mapping [container.{name}] {key} is not a plain identifier; refusing to query")
        spec[name] = dict(part)
    version = cfg.get("version")                              # a table that records the schema version (optional)
    if version is not None:
        for key in ("table", "column"):
            if not isinstance(version.get(key), str) or not IDENT_RE.match(version[key]):
                raise ContainerProblem(f"mapping [container.version] {key} is not a plain identifier; refusing to query")
        spec["version"] = dict(version)
    return spec


def mask_node_id(node_id):
    """A short stable stand-in for a node id, so a note can be told apart without showing the id."""
    return hashlib.sha256(str(node_id).encode()).hexdigest()[:6]


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def open_sqlite_readonly(path):
    """Read-only, not immutable: a running app's WAL data is still included."""
    try:
        return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ContainerProblem(f"cannot open the database read-only: {exc}")


def check_schema_in_db(conn, spec):
    """Every named table and column must exist (checked against sqlite_master and table_info), and no table may be virtual."""
    try:
        tables = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall())
    except sqlite3.DatabaseError as exc:
        raise ContainerProblem(f"not a readable SQLite database: {exc}")
    missing = []
    for name, part in spec.items():
        if name == "version":
            continue                                          # optional: a database without it just has no version
        table = part["table"]
        if table not in tables:
            missing.append(f"table {table}")
            continue
        if (tables[table] or "").lstrip().upper().startswith("CREATE VIRTUAL"):
            raise ContainerProblem(f"table {table} is a virtual table; virtual and FTS tables are never read")
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})")}
        wanted = [k for k in (*SQLITE_TABLES[name], *SQLITE_OPTIONAL.get(name, ())) if k != "table" and k in part]
        missing += [f"column {table}.{part[k]}" for k in wanted if part[k] not in have]
    if missing:
        raise ContainerProblem("the database does not match the mapping: missing " + ", ".join(missing), missing)


def select_rows(conn, table, columns, where=None, params=()):
    sql = f"SELECT {', '.join(quote_ident(c) for c in columns)} FROM {quote_ident(table)}"
    if where:
        sql += f" WHERE {quote_ident(where)} = ?"
    return conn.execute(sql, params).fetchall()


def read_schema_version(conn, spec):
    """The newest schema version the database records (None when it keeps none)."""
    part = spec.get("version")
    if not part:
        return None
    try:
        row = conn.execute(f"SELECT MAX({quote_ident(part['column'])}) FROM {quote_ident(part['table'])}").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and isinstance(row[0], int) else None


class SqliteReader:
    """One session of a SQLite database: the chain of nodes from the head, plus one chain per subagent head."""
    kind = "sqlite"

    def __init__(self, conn, spec, session):
        self.spec, self.session, self.notes = spec, session, []
        self.version = read_schema_version(conn, spec)
        self._build(conn)

    def _build(self, conn):
        nodes, sessions = self.spec["nodes"], self.spec["sessions"]
        cols = [nodes[k] for k in ("session", "id", "parent", "created", "payload")]
        rows = {}
        for sess, nid, parent, created, payload in select_rows(conn, nodes["table"], cols, nodes["session"], (self.session["raw_id"],)):
            rows[nid] = {"parent": parent, "payload": payload,
                         "row": {nodes["session"]: sess, nodes["id"]: nid, nodes["parent"]: parent, nodes["created"]: created}}
        extra = {"$session": {"model": self.session["model"]}} if self.session.get("model") is not None else {}
        self.main_lines, seen = self._chain(rows, self.session["head"], "", extra)
        main_ids = set(seen)
        self.sub_chains, self.shared_with_main = [], 0
        heads = self.spec.get("subagent_heads")
        if heads:
            for _, agent, head in select_rows(conn, heads["table"], [heads["session"], heads["agent"], heads["head"]],
                                              heads["session"], (self.session["raw_id"],)):
                lines, ids = self._chain(rows, head, " (subagent)")
                self.sub_chains.append((str(agent), lines))
                self.shared_with_main += len(ids & main_ids)
                seen |= ids
        self.off_chain = len(set(rows) - seen)
        if self.off_chain:
            self.notes.append(f"off-chain nodes: {self.off_chain}")

    def _chain(self, rows, head, tag, extra=None):
        """Records from the root to `head`, and the node ids on it. A parent that is missing, or a loop,
        ends the chain and is reported in the notes (ids masked)."""
        chain, ids, nid = [], set(), head
        while nid in rows and nid not in ids:
            ids.add(nid)
            chain.append(rows[nid])
            nid = rows[nid]["parent"]
        if nid is not None and nid in ids:
            self.notes.append(f"chain loop at node {mask_node_id(nid)}{tag}")
        elif nid is not None:
            self.notes.append(f"chain broken: 1 earlier nodes unreachable{tag}")
        lines = []
        for node in reversed(chain):
            payload = node["payload"]
            try:
                record = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
            except ValueError:
                record = {"payload": payload if isinstance(payload, str) else repr(payload)}
            record = dict(record) if isinstance(record, dict) else {"payload": record}
            record["$row"] = node["row"]
            record.update(extra or {})
            lines.append(json.dumps(record, ensure_ascii=False, default=str))
        return lines, ids

    def storage_info(self):
        info = {"kind": "sqlite", "off_chain_nodes": self.off_chain, "subagent_chains": len(self.sub_chains),
                "nodes_shared_with_main": self.shared_with_main}
        if self.version is not None:
            info.update(schema_version=self.version, schema_version_source=self.spec["version"]["table"])
        return info

    def first_lines(self):
        return self.main_lines

    def sources(self, mapping):
        label = f"sqlite:{self.session['id']}"
        yield Source(self.main_lines, label, True)
        for agent, lines in self.sub_chains:
            yield Source(lines, f"{label}:{agent}", False, agent, None, "subagent_heads")


def list_sqlite_sessions(conn, spec):
    part = spec["sessions"]
    cols = [part[k] for k in ("id", "cwd", "created", "last_activity", "head")]
    if part.get("model"):
        cols.append(part["model"])
    return [{"raw_id": r[0], "id": str(r[0]), "cwd": r[1] or "", "created": parse_ts(r[2]), "last": parse_ts(r[3]),
             "head": r[4], "model": r[5] if len(r) > 5 else None} for r in select_rows(conn, part["table"], cols)]


def choose_sqlite_session(sessions, args, repo):
    """(session, ambiguous_output). Same rules as jsonl: --session-id, else working-directory match and the most recent."""
    if args.session_id:
        return next((s for s in sessions if s["id"] == args.session_id), None), None
    here = {str(repo.resolve()), str(repo)}
    mine = sorted((s for s in sessions if s["cwd"] in here or str(Path(s["cwd"]).resolve()) in here),
                  key=lambda s: s["last"] or 0, reverse=True)
    recent = [s for s in mine if s["last"] is not None and time.time() - s["last"] < RECENT_SECONDS]
    if len(recent) >= 2:
        return None, {"ambiguous": [{"session": s["id"], "working_directory": mask_home(s["cwd"]),
                                     "created": iso(s["created"]), "last_activity": iso(s["last"])} for s in recent],
                      "note": "Several recent sessions; ask the user which one, then pass --session-id."}
    return (mine[0] if mine else None), None



def open_sqlite_session(args, repo, raw):
    """(reader, None), or (None, (output, exit code)) when the database cannot be used."""
    host = raw.get("host", "unknown")
    conn = None
    try:
        spec = sqlite_spec(raw)                       # identifiers are vetted before the file is even looked at
        default = (raw.get("container") or {}).get("default_path")
        db = Path(args.paths[0]) if args.paths else (Path(os.path.expanduser(default)) if default else None)
        if db is None or not db.is_file():
            return None, (degraded_report(host, repo, [mask_home(str(db))] if db else [], raw.get("skill_folders")), EXIT_NO_TRANSCRIPT)
        conn = open_sqlite_readonly(db)
        check_schema_in_db(conn, spec)
        session, ambiguous = choose_sqlite_session(list_sqlite_sessions(conn, spec), args, repo)
        if ambiguous:
            return None, (ambiguous, 3)
        if session is None:
            return None, (degraded_report(host, repo, [mask_home(str(db))], raw.get("skill_folders")), EXIT_NO_TRANSCRIPT)
        return SqliteReader(conn, spec, session), None
    except ContainerProblem as exc:
        out = {"error": str(exc)}
        if exc.missing:
            out["missing"] = exc.missing
        return None, (out, 2)
    except sqlite3.Error as exc:
        return None, ({"error": f"database error: {exc}"}, 2)
    finally:
        if conn is not None:
            conn.close()


# ---- user adapters, labels, saving ---------------------------------------------------------------------------------

TOP_KEYS = frozenset("""call_categories call_name_categories capabilities checks container detect fields health_hints hook_files hooks_format
host interrupt_on_repeated_prompt interruption_prefixes made_by replica made_from_version not_a_turn_prefixes plugin_skill_folders records requires schema_version
skill_detections skill_folders skill_tool_label subagent_marker subagents tested_versions token_method token_method_fallback
coverage_notes null_means_zero stability tool_time_source turnless_activity type_path variant variants versions""".split())
RULE_KEYS = frozenset("""call_command_name call_command_regex call_id call_input call_kind call_name call_raw calls event_id
instruction_always_on instruction_bytes instruction_items instruction_paths interrupt match model msg_id non_turn_reason offered_block
offered_desc_from offered_desc_item offered_desc_pairs offered_each offered_from offered_item offered_section offered_skills prompt_time
call_split call_via result_error result_exit_code result_exit_code_regex result_finished result_id result_started result_text results role skill_command
spawned_agent spawned_model text timestamp turn turn_error turn_regex turn_start usage_cache_read usage_cache_write usage_context usage_dedupe_key
usage_fallback usage_input usage_input_includes_cache usage_output usage_present""".split())
FIELD_KEYS = frozenset("version msg_id model timestamp event_id".split())
CAPABILITY_KEYS = frozenset("has_tokens has_timestamps has_tool_results has_ids".split())
CHECK_KEYS = frozenset("name metric first second relation tolerance_pct slack_first skip_if_second_zero severity scope".split())
NOTE_KEYS = frozenset("match each where split text".split())
SUBAGENT_KEYS = frozenset("""agent_path brief_key call_id file_prefix forked_from history_start link_regex meta_file model ordinal
parent_id self_id spawn_key""".split())
REGEX_KEYS = frozenset("call_command_regex result_exit_code_regex turn_regex offered_block offered_item offered_section offered_desc_item link_regex".split())
HOST_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
BUILTIN_NAMES = {**HOSTS, **STORE_HOSTS, "kiro": "kiro"}


def user_adapter_dir(env=None, platform=None, home=None):
    """$XDG_CONFIG_HOME/workflow-reviewer/hosts (default ~/.config/...); %APPDATA%\\workflow-reviewer\\hosts on Windows."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = Path(home) if home is not None else Path.home()
    if platform == "win32":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "workflow-reviewer" / "hosts"
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "workflow-reviewer" / "hosts"


def mapping_problems(raw):
    """Why a drafted or saved mapping cannot be used: unknown keys (declarative keys only), bad schema, container, regexes."""
    errors = []
    error, _ = schema_check(raw)
    if error:
        errors.append(error)
    for v in resolve_variants(raw):
        bad = [f for f in v.get("null_means_zero", []) if f not in FIELD_OF_TOKEN_KEY]
        if not isinstance(v.get("null_means_zero", []), list) or bad:
            errors.append(f"null_means_zero must list token fields ({', '.join(FIELD_OF_TOKEN_KEY)}); got {bad or v.get('null_means_zero')!r}")
    host = raw.get("host")
    if not isinstance(host, str) or not HOST_NAME_RE.match(host):
        errors.append("host name must be lowercase letters, digits, - or _ (up to 41 characters)")
    errors += [f"unknown key '{k}' (declarative keys only)" for k in raw if k not in TOP_KEYS]

    def keys(where, table, allowed):
        if isinstance(table, dict):
            errors.extend(f"unknown key '{k}' in {where} (declarative keys only)" for k in table if k not in allowed)

    def rules(records, where):
        for n, rule in enumerate(records or []):
            keys(f"{where}records[{n}]", rule, RULE_KEYS)
            for k, v in (rule.items() if isinstance(rule, dict) else []):
                if k in REGEX_KEYS and isinstance(v, (str, list)):
                    for pattern in ([v] if isinstance(v, str) else v):
                        try:
                            re.compile(pattern)
                        except (re.error, TypeError):
                            errors.append(f"{where}records[{n}].{k} is not a valid regular expression")

    detect = raw.get("detect")
    if detect is not None:
        keys("detect", detect, {"env"})
        names = detect.get("env") if isinstance(detect, dict) else None
        if not isinstance(names, list) or not all(isinstance(n, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,60}", n) for n in names):
            errors.append("detect.env must be a list of environment variable names (names only, never values)")
    keys("fields", raw.get("fields"), FIELD_KEYS)
    keys("capabilities", raw.get("capabilities"), CAPABILITY_KEYS)
    keys("subagents", raw.get("subagents"), SUBAGENT_KEYS)
    for n, check in enumerate(raw.get("checks", [])):
        keys(f"checks[{n}]", check, CHECK_KEYS)
    for n, note in enumerate(raw.get("coverage_notes", [])):
        keys(f"coverage_notes[{n}]", note, NOTE_KEYS)
        if isinstance(note, dict):
            try:
                str(note.get("text", "")).format(count=0, **dict.fromkeys(note.get("split", {}), 0))
            except (KeyError, IndexError, ValueError):
                errors.append(f"coverage_notes[{n}].text may use only {{count}} and its split names")
    rules(raw.get("records"), "")
    for n, variant in enumerate(raw.get("variants", [])):
        keys(f"variants[{n}]", variant, TOP_KEYS | {"name"})
        rules(variant.get("records"), f"variants[{n}].")
    if "subagents" in raw and isinstance(raw["subagents"].get("link_regex"), str):
        try:
            re.compile(raw["subagents"]["link_regex"])
        except re.error:
            errors.append("subagents.link_regex is not a valid regular expression")
    kind = container_kind(raw)
    errors += [e for e in [container_error(kind)] if e]
    if kind == "sqlite":
        try:
            sqlite_spec(raw)
        except ContainerProblem as exc:
            errors.append(str(exc))
    if kind == "json_document":
        try:
            json_document_spec(raw)
        except ContainerProblem as exc:
            errors.append(str(exc))
    return errors


def load_user_adapters(env=None):
    """([(file name, raw mapping)], [notes]). A file that cannot be used is skipped with a note, never fatal."""
    folder = user_adapter_dir(env)
    found, notes = [], []
    for path in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            notes.append(f"user adapter {path.name} ignored: {str(exc).splitlines()[0][:80]}")
            continue
        error, _ = schema_check(raw)
        if error or not isinstance(raw.get("host"), str):
            notes.append(f"user adapter {path.name} ignored: {error or 'no host name'}")
            continue
        found.append((path.name, raw))
    return found, notes


def registry():
    """host name -> {"raw": the mapping (built-in variants plus user variants, or the user's own), "builtin": bool}, plus notes."""
    adapters, notes = load_user_adapters()
    hosts = {}
    for name, key in BUILTIN_NAMES.items():
        raw = load_mapping(key)
        raw.setdefault("capabilities", {})
        for variant in (raw.get("variants") or []):
            variant.setdefault("label", "verified")
        hosts[name] = {"raw": raw, "builtin": True}
    for file, raw in adapters:
        host = raw["host"]
        problems = mapping_problems(raw)
        if problems:
            notes.append(f"user adapter {file} ignored: {problems[0]}")
            continue
        raw = {**raw, "capabilities": raw.get("capabilities", {})}
        if host in hosts and hosts[host]["builtin"]:                       # extra variants for a built-in host; never a replacement
            base = hosts[host]["raw"]
            variants = list(base.get("variants") or [{"name": base.get("variant") or "default", "label": "verified"}])
            allowed = set(base.get("null_means_zero", []))
            for v in (raw.get("variants") or [raw]):
                variants.append({**{k: x for k, x in v.items() if k not in ("host", "schema_version", "made_by", "made_from_version")},
                                 "name": v.get("name") or v.get("variant") or Path(file).stem, "label": "unproven",
                                 "checks": v.get("checks", []),
                                 **({"null_means_zero": [f for f in v["null_means_zero"] if f in allowed]} if "null_means_zero" in v else {})})
            base["variants"] = variants
        elif host not in hosts:
            hosts[host] = {"raw": restrict_null_zero(raw, None), "builtin": False}      # no built-in check vouches for a null
    return hosts, notes


WEAKEN_ERROR = "an adapter may add mappings, not remove or loosen checks"
SEVERITY_RANK = {"warn": 1, "broken": 2}


def weakened_checks(raw, builtin):
    """How a user mapping for a built-in host removes or loosens one of the host's built-in checks (empty when it does not).

    Every variant of the user's mapping must keep each built-in check, by name, counting the same things the same way,
    with the same or a smaller tolerance and slack, the same or a higher severity, and no new way to skip it."""
    problems = []
    for variant in resolve_variants(raw):
        mine = {c.get("name"): c for c in variant.get("checks", []) if isinstance(c, dict)}
        where = f"variant '{variant['variant']}': " if raw.get("variants") else ""
        for b in builtin.get("checks", []):
            name, c = b["name"], mine.get(b["name"])
            if c is None:
                problems.append(f"{where}removes check '{name}'")
                continue
            if any(c.get(k) != b.get(k) for k in ("metric", "first", "second")) or c.get("relation", "equal") != b.get("relation", "equal"):
                problems.append(f"{where}changes what check '{name}' compares")
            if float(c.get("tolerance_pct", 0)) > float(b.get("tolerance_pct", 0)):
                problems.append(f"{where}loosens the tolerance of check '{name}'")
            if int(c.get("slack_first", 0)) > int(b.get("slack_first", 0)):
                problems.append(f"{where}loosens the slack of check '{name}'")
            if SEVERITY_RANK.get(c.get("severity", "broken"), 0) < SEVERITY_RANK.get(b.get("severity", "broken"), 2):
                problems.append(f"{where}lowers the severity of check '{name}'")
            if c.get("skip_if_second_zero") and not b.get("skip_if_second_zero"):
                problems.append(f"{where}lets check '{name}' be skipped")
            if c.get("scope", "window") != b.get("scope", "window"):
                problems.append(f"{where}changes the scope of check '{name}'")
    return problems


def widened_null_zero(raw, builtin):
    """How a user mapping adds or widens null_means_zero: every variant may list only fields the built-in mapping of the
    same host lists (`builtin` is None for a host with no built-in mapping, which may list none)."""
    allowed = set((builtin or {}).get("null_means_zero", []))
    problems = []
    for variant in resolve_variants(raw):
        where = f"variant '{variant['variant']}': " if raw.get("variants") else ""
        extra = [f for f in variant.get("null_means_zero", []) if f not in allowed]
        if extra:
            problems.append(f"{where}null_means_zero adds {', '.join(extra)}, which no built-in check vouches for")
    return problems


def restrict_null_zero(raw, builtin):
    """A mapping that is not built in keeps only the null_means_zero fields its host's built-in mapping lists."""
    allowed = set((builtin or {}).get("null_means_zero", []))
    raw = dict(raw)
    if "null_means_zero" in raw:
        raw["null_means_zero"] = [f for f in raw["null_means_zero"] if f in allowed]
    if raw.get("variants"):
        raw["variants"] = [{**v, "null_means_zero": [f for f in v["null_means_zero"] if f in allowed]} if "null_means_zero" in v else v
                           for v in raw["variants"]]
    return raw


def mapping_label(mapping, source):
    """verified: a built-in mapping with tested versions. experimental: a built-in one that says so. unproven: anything a user made."""
    if source == "builtin" and mapping.get("label", "verified") == "verified" and mapping.get("tested_versions"):
        return "experimental" if mapping.get("stability") == "experimental" else "verified"
    return "unproven"


def adapter_text(text, host_version):
    """The drafted mapping with the provenance keys placed among its top-level keys."""
    lines = text.split("\n")
    at = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines))
    extra = ['made_by = "workflow-reviewer adapter flow"', f"made_from_version = {json.dumps(str(host_version or 'unknown'))}"]
    return "\n".join(lines[:at] + extra + lines[at:]).rstrip("\n") + "\n"


def save_adapter(name, text, host_version, replace):
    """Write <adapter folder>/<name>.toml (file 0600, folder 0700). Returns (path, None) or (None, error)."""
    folder = user_adapter_dir()
    target = folder / f"{name}.toml"
    if target.exists() and not replace:
        return None, f"adapter {name} exists; pass --replace to overwrite it"
    fresh = not folder.exists()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if fresh:
        os.chmod(folder, 0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(adapter_text(text, host_version))
    os.chmod(target, 0o600)
    return target, None


# ---- json_document storage: a session is a folder of JSON documents ----------------------------------------------------------

JSON_DOC_CAP = 200 * 1024 * 1024
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,100}$")
PATH_RE = re.compile(r"^[A-Za-z0-9_.\[\]=\-/$]*$")


class DocumentTooLarge(ContainerProblem):
    """A document over the size cap: exit 4."""


def json_document_spec(raw):
    """The mapping's roots, documents and merge rule, checked before any file is touched."""
    cfg = raw.get("container") or {}
    roots = cfg.get("roots")
    if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and r for r in roots):
        raise ContainerProblem("mapping [container] roots must be a list of folders")
    docs = cfg.get("documents")
    if not isinstance(docs, list) or not docs:
        raise ContainerProblem("mapping [[container.documents]] is missing")
    parsed = []
    for n, doc in enumerate(docs):
        file = doc.get("file") if isinstance(doc, dict) else None
        plain = file.replace("{id}", "x") if isinstance(file, str) else None        # {id} stands for the session folder's name
        if plain is None or not FILE_NAME_RE.match(plain) or ".." in plain:
            raise ContainerProblem(f"mapping container.documents[{n}].file is not a plain file name; refusing to read")
        path = doc.get("records", ".")
        if not isinstance(path, str) or not PATH_RE.match(path):
            raise ContainerProblem(f"mapping container.documents[{n}].records is not a plain path")
        name = doc.get("name") or file.rsplit(".", 1)[0].replace("{id}", "session").replace(".", "_")
        if not isinstance(name, str) or not IDENT_RE.match(name):
            raise ContainerProblem(f"mapping container.documents[{n}].name is not a plain identifier")
        parsed.append({"file": file, "records": path, "name": name, "as_record": bool(doc.get("as_record"))})
    merge = (cfg.get("merge") or {}).get("by", "order")
    if not isinstance(merge, str) or not PATH_RE.match(merge):
        raise ContainerProblem("mapping container.merge.by must be 'order' or a timestamp path")
    session = cfg.get("session") or {}
    for key in ("cwd_document", "cwd_path"):
        if key in session and (not isinstance(session[key], str) or not PATH_RE.match(session[key].replace("{id}", "x")) or ".." in session[key]):
            raise ContainerProblem(f"mapping container.session.{key} is not a plain name")
    subagents = cfg.get("subagents")                     # child sessions listed in a database: parent id column, optional flag column
    if subagents is not None:
        if not isinstance(subagents.get("db"), str) or not subagents["db"]:
            raise ContainerProblem("mapping container.subagents.db must be a path")
        for key in ("table", "id", "parent"):
            if not isinstance(subagents.get(key), str) or not IDENT_RE.match(subagents[key]):
                raise ContainerProblem(f"mapping container.subagents.{key} is not a plain identifier; refusing to query")
        if "flag" in subagents and (not isinstance(subagents["flag"], str) or not IDENT_RE.match(subagents["flag"])):
            raise ContainerProblem("mapping container.subagents.flag is not a plain identifier; refusing to query")
    return {"roots": roots, "documents": parsed, "merge": merge, "session": session, "subagents": subagents}


def read_document(path):
    """One JSON document, read whole (checked against the size cap first). Replaceable in tests."""
    size = path.stat().st_size
    if size > JSON_DOC_CAP:
        raise DocumentTooLarge(f"document {path.name} is {size // (1024 * 1024)} MB, over the {JSON_DOC_CAP // (1024 * 1024)} MB limit; not read")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ContainerProblem(f"document {path.name} is not valid JSON")


def document_items(doc, path, name, as_record=False):
    if as_record:                                        # the whole document is one record (a session's metadata)
        if not isinstance(doc, dict):
            raise ContainerProblem(f"document {name} is not an object")
        return [doc]
    if path in ("", "."):
        items = doc if isinstance(doc, list) else None
    else:
        found = get_all(doc, path)
        items = found[0] if len(found) == 1 and isinstance(found[0], list) else (found or None)
    if items is None:
        raise ContainerProblem(f"document {name} has no record list at '{path}'")
    return [i if isinstance(i, dict) else {"value": i} for i in items]


class JsonDocumentReader:
    """One session folder: its documents merged into one record stream."""
    kind = "json_document"

    def __init__(self, folder, spec, session_id, with_children=True):
        self.folder, self.spec, self.session_id, self.notes, self.version = folder, spec, session_id, [], None
        records, loaded = [], 0
        for order, doc in enumerate(spec["documents"]):
            file = folder / doc["file"].replace("{id}", session_id)
            if not file.is_file():
                self.notes.append(f"document {doc['file'].replace('{id}', '<id>')} not found")
                continue
            loaded += 1
            for index, item in enumerate(document_items(read_document(file), doc["records"], file.name, doc["as_record"])):
                records.append((order, index, {**item, "$doc": doc["name"], "$index": index}))
        if not loaded:
            raise ContainerProblem("none of the declared documents exist in the session folder")
        by = spec["merge"]
        if by != "order":
            last, stamped = 0.0, []
            for order, index, rec in records:
                ts = parse_ts(get_first(rec, by))
                last = ts if ts is not None else last          # a record with no time stays beside its neighbour
                stamped.append((last, order, index, rec))
            records = [(o, i, r) for _, o, i, r in sorted(stamped, key=lambda t: t[:3])]
        self.records = [r for _, _, r in records]
        self.documents = loaded
        self.children = self._children(spec) if with_children else []

    def _children(self, spec):
        """Subagent sessions the mapping's database lists under this session, read like the session itself."""
        cfg = spec.get("subagents")
        if not cfg:
            return []
        db = Path(os.path.expanduser(cfg["db"]))
        if not db.is_file():
            return []
        found = []
        try:
            conn = open_sqlite_readonly(db)
            try:
                sql = f"SELECT {quote_ident(cfg['id'])} FROM {quote_ident(cfg['table'])} WHERE {quote_ident(cfg['parent'])} = ?"
                if cfg.get("flag"):
                    sql += f" AND {quote_ident(cfg['flag'])} = 1"
                ids = [row[0] for row in conn.execute(sql + " ORDER BY 1", (self.session_id,))]
            finally:
                conn.close()
        except (ContainerProblem, sqlite3.Error):
            self.notes.append("subagent sessions could not be listed from the database")
            return []
        for child in ids:
            if not isinstance(child, str) or not FILE_NAME_RE.match(child):
                continue
            for root in spec["roots"]:
                folder = Path(os.path.expanduser(root)) / child
                if folder.is_dir():
                    found.append(JsonDocumentReader(folder, spec, child, with_children=False))
                    break
        return found

    def first_lines(self):
        return [json.dumps(r, ensure_ascii=False, default=str) for r in self.records]

    def sources(self, mapping):
        yield Source(self.first_lines(), f"json_document:{self.session_id}", True)
        for child in self.children:
            yield Source(child.first_lines(), f"json_document:{self.session_id}:{child.session_id}", False, child.session_id, None,
                         "parent_session_id")

    def storage_info(self):
        return {"kind": "json_document", "documents": self.documents, "records": len(self.records),
                **({"subagent_sessions": len(self.children)} if self.spec.get("subagents") else {})}


def document_sessions(spec):
    """[{id, folder, last}]: every folder under the roots that holds at least one declared document."""
    found = []
    for root in spec["roots"]:
        base = Path(os.path.expanduser(root))
        for folder in sorted(base.iterdir()) if base.is_dir() else []:
            files = [folder / d["file"].replace("{id}", folder.name) for d in spec["documents"] if (folder / d["file"].replace("{id}", folder.name)).is_file()]
            if folder.is_dir() and files:
                found.append({"id": folder.name, "folder": folder, "last": max(f.stat().st_mtime for f in files),
                              "created": min(f.stat().st_mtime for f in files)})
    return found


def session_cwd(session, spec):
    cfg = spec["session"]
    if not (cfg.get("cwd_document") and cfg.get("cwd_path")):
        return None
    try:
        doc = read_document(session["folder"] / cfg["cwd_document"].replace("{id}", session["id"]))
    except (ContainerProblem, OSError):
        return ""
    value = get_first(doc, cfg["cwd_path"])
    return value if isinstance(value, str) else ""


def open_json_session(args, repo, raw):
    """(reader, None), or (None, (output, exit code))."""
    host = raw.get("host", "unknown")
    try:
        spec = json_document_spec(raw)
        if args.paths:                                        # a session folder, or a document inside one
            target = Path(args.paths[0])
            folder = target.parent if target.is_file() else target
            if not folder.is_dir():
                return None, (degraded_report(host, repo, [mask_home(str(folder))], raw.get("skill_folders")), EXIT_NO_TRANSCRIPT)
            return JsonDocumentReader(folder, spec, folder.name), None
        sessions = document_sessions(spec)
        if args.session_id:
            chosen = next((s for s in sessions if s["id"] == args.session_id), None)
        else:
            here = {str(repo.resolve()), str(repo)}
            mine = []
            for s in sessions:
                cwd = session_cwd(s, spec)
                if cwd is None or cwd in here or (cwd and str(Path(cwd).resolve()) in here):
                    s["cwd"] = cwd or ""
                    mine.append(s)
            mine.sort(key=lambda s: s["last"], reverse=True)
            recent = [s for s in mine if time.time() - s["last"] < RECENT_SECONDS]
            if len(recent) >= 2:
                return None, ({"ambiguous": [{"session": s["id"], "working_directory": mask_home(s["cwd"]), "created": iso(s["created"]),
                                              "last_activity": iso(s["last"])} for s in recent],
                               "note": "Several recent sessions; ask the user which one, then pass --session-id."}, 3)
            chosen = mine[0] if mine else None
        if chosen is None:
            return None, (degraded_report(host, repo, [mask_home(r) for r in spec["roots"]], raw.get("skill_folders")), EXIT_NO_TRANSCRIPT)
        return JsonDocumentReader(chosen["folder"], spec, chosen["id"]), None
    except DocumentTooLarge as exc:
        return None, ({"error": str(exc)}, 4)
    except ContainerProblem as exc:
        return None, ({"error": str(exc)}, 2)


# ---- probe: the structure of a file, folder or database, and nothing else ----------------------------------------------------

PROBE_NAME_FIELDS = ("type", "role", "kind", "status", "say", "ask", "event", "subtype")
PROBE_MAX_PATHS = 200
PROBE_MAX_VALUES = 50
PROBE_MAX_RECORDS = 5000
PROBE_MAX_ELEMENTS = 200
PROBE_MAX_KEYS = 30            # a dict with more distinct keys than this holds data (ids), not structure
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,40}$")
NAMESPACED_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,30}(/[A-Za-z_][A-Za-z0-9_-]{0,30}){1,2}$")      # no dots: a file name is not an extension key
TYPE_VALUE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
TIME_KEY_RE = re.compile(r"(^|_|-)(ts|time|timestamp|date|at|created|updated|started|finished|completed|expires)($|_|-)|(time|stamp|date)$", re.I)
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def plain_key(key):
    """The fixed part of the probe's <key> rule: a plain identifier or a namespaced extension key, not an id, nothing redact would
    change. The probe adds rules that need many records; a single name (an unseen record type) gets this part only."""
    return bool(KEY_RE.match(key) or NAMESPACED_KEY_RE.match(key)) and not id_like(key) and redact(key) == key


def id_like(text):
    """Does this look like an id, token or number rather than a name? Such strings are never printed."""
    return bool(re.search(r"[0-9a-fA-F]{8,}", text) or re.search(r"\d{5,}", text) or re.search(r"\d.*\d.*\d.*\d", text)
                or "@" in text or re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", text, re.I))


def safe_ident(name):
    """A table or column name that is a plain identifier and does not look like an id or a secret."""
    return bool(IDENT_RE.match(name)) and len(name) <= 64 and not id_like(name) and redact(name) == name


def safe_value(text):
    return bool(TYPE_VALUE_RE.match(text)) and not id_like(text) and redact(text) == text


def scalar_type(v):
    return "null" if v is None else "bool" if isinstance(v, bool) else "int" if isinstance(v, int) else "float" if isinstance(v, float) \
        else "str" if isinstance(v, str) else "list" if isinstance(v, list) else "dict"


class Structure:
    """Counts of JSON paths, types, type-field values, timestamp shapes and array lengths over many records."""

    def __init__(self):
        self.paths = {}          # raw path tuple -> [records containing it, {type: n}]
        self.values = {}         # (raw path tuple of the field, ) -> {value: n}
        self.times = {}          # raw path tuple -> {shape: n}
        self.lengths = {}        # raw path tuple -> [lengths]
        self.keysets = {}        # raw path tuple of a dict -> {frozenset of its keys: how often}
        self.records = 0

    def add(self, record):
        self.records += 1
        seen = set()
        self._walk(record, (), seen)
        for path in seen:
            self.paths[path][0] += 1

    def _walk(self, value, path, seen):
        kind = scalar_type(value)
        if path:
            entry = self.paths.setdefault(path, [0, {}])
            entry[1][kind] = entry[1].get(kind, 0) + 1
            seen.add(path)
        if isinstance(value, dict):
            shapes = self.keysets.setdefault(path, {})
            shape = frozenset(str(k) for k in value)
            shapes[shape] = shapes.get(shape, 0) + 1
            for key, child in value.items():
                self._walk(child, path + (str(key),), seen)
                self._field(str(key), child, path + (str(key),))
        elif isinstance(value, list):
            self.lengths.setdefault(path, []).append(len(value))
            for child in value[:PROBE_MAX_ELEMENTS]:
                self._walk(child, path + ("[]",), seen)

    def _field(self, key, child, path):
        if key in PROBE_NAME_FIELDS and isinstance(child, str):
            group = self.values.setdefault(path, {})
            group[child] = group.get(child, 0) + 1
        shape = None
        if isinstance(child, str) and ISO_RE.match(child):
            shape = "iso8601"
        elif isinstance(child, (int, float)) and not isinstance(child, bool) and TIME_KEY_RE.search(key):
            shape = "epoch_ms" if 1e12 <= child < 1e14 else "epoch_s" if 1e9 <= child < 1e11 else None
        if shape:
            slot = self.times.setdefault(path, {})
            slot[shape] = slot.get(shape, 0) + 1

    # -- naming: only structural keys are ever printed -----------------------------------------------------------------------
    def map_like(self, path):
        """Is this object a map from data (file names, ids) to values? Its key set then differs from record to record:
        many distinct key sets, relative to how often the object occurs. The records themselves (the root) are exempt."""
        shapes = self.keysets.get(path)
        if not path or not shapes:
            return False
        distinct, occurrences = len(shapes), sum(shapes.values())
        return distinct >= 3 and distinct > 0.25 * occurrences

    def namer(self):
        """Names for printing. A key is shown only when it is plain structure: a plain identifier (or a namespaced extension key
        of plain identifiers joined by /, seen in at least 3 records) that is not an id, not inside a map-like object, and, once
        there are enough records to tell, seen in at least 3 of them. Everything else prints as <key>."""
        def key_name(path, i):
            key, parent = path[i], path[:i]
            count = self.paths[path[:i + 1]][0]
            if key == "[]":
                return "[]"
            if not plain_key(key):
                return "<key>"
            if NAMESPACED_KEY_RE.match(key) and count >= 3:        # an extension key (chisel/tool_call_content) stays even in a varying object
                return key
            if self.map_like(parent) or (self.records >= 6 and count < 3):
                return "<key>"
            return key if KEY_RE.match(key) else "<key>"

        def name_of(path):
            return ".".join(key_name(path, i) for i in range(len(path))).replace(".[]", "[]")
        return name_of

    def report(self):
        name_of = self.namer()
        merged = {}
        for path, (count, types) in self.paths.items():
            slot = merged.setdefault(name_of(path), [0, {}])
            slot[0] += count
            for t, n in types.items():
                slot[1][t] = slot[1].get(t, 0) + n
        ranked = sorted(merged.items(), key=lambda kv: (-kv[1][0], kv[0]))
        paths = [{"path": p, "count": c, "types": dict(sorted(t.items()))} for p, (c, t) in ranked[:PROBE_MAX_PATHS]]
        groups = {}
        for path, counts in self.values.items():
            slot = groups.setdefault(name_of(path), {})
            for v, n in counts.items():
                slot[v] = slot.get(v, 0) + n
        type_groups = {}
        for field, counts in sorted(groups.items()):
            kept = sorted(((v, n) for v, n in counts.items() if n >= 2 and safe_value(v)), key=lambda vn: (-vn[1], vn[0]))
            if kept:                                   # a field whose values are all hidden is not worth a group
                type_groups[field] = {"values": dict(kept[:PROBE_MAX_VALUES]), "more": len(counts) - min(len(kept), PROBE_MAX_VALUES)}
        times = {}
        for path, shapes in self.times.items():
            slot = times.setdefault(name_of(path), {})
            for s, n in shapes.items():
                slot[s] = slot.get(s, 0) + n
        arrays = {}
        for path, lens in self.lengths.items():
            lens = sorted(lens)
            arrays[name_of(path)] = {"count": len(lens), "min": lens[0], "median": lens[len(lens) // 2], "max": lens[-1]}
        return {"records": self.records, "paths": paths, "paths_more": max(0, len(ranked) - PROBE_MAX_PATHS),
                "type_groups": type_groups, "timestamps": dict(sorted(times.items())),
                "array_lengths": dict(sorted(arrays.items())[:PROBE_MAX_PATHS])}


def probe_records(records):
    s = Structure()
    for r in records[:PROBE_MAX_RECORDS]:
        s.add(r)
    return s.report()


def check_size(path):
    if path.stat().st_size > JSON_DOC_CAP:
        raise DocumentTooLarge(f"{path.suffix or 'file'} file is over the {JSON_DOC_CAP // (1024 * 1024)} MB limit; not read")


def probe_file(path):
    check_size(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".jsonl" or (not path.suffix and "\n{" in text[:2000]):
        records, bad = [], 0
        for line in text.split("\n"):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    bad += 1
        return {"kind": "jsonl", "unparsable_lines": bad, **probe_records([r for r in records if isinstance(r, (dict, list))] or [])}
    try:
        doc = json.loads(text)
    except ValueError:
        raise ContainerProblem("the file is neither JSON nor JSON lines")
    return {"kind": "json", **probe_records(doc if isinstance(doc, list) and all(isinstance(d, dict) for d in doc) else [doc])}


def probe_folder(path, depth=3):
    kinds, dirs = {}, 0

    def walk(folder, level):
        nonlocal dirs
        try:
            entries = list(folder.iterdir())
        except OSError:
            return
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    dirs += 1
                    if level < depth:
                        walk(e, level + 1)
                elif e.is_file():
                    ext = e.suffix.lower() if re.fullmatch(r"\.[a-z0-9]{1,8}", e.suffix.lower()) and safe_value(e.suffix.lower()[1:]) else "(other)"
                    slot = kinds.setdefault(ext or "(none)", {"count": 0, "bytes": 0})
                    slot["count"] += 1
                    slot["bytes"] += e.stat().st_size
            except OSError:
                continue
    walk(path, 1)
    return {"kind": "folder", "levels_scanned": depth, "folders": dirs, "file_kinds": dict(sorted(kinds.items()))}


def probe_sqlite(path):
    check_size(path)
    conn = open_sqlite_readonly(path)
    try:
        master = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
        virtual = [n for n, sql in master if (sql or "").lstrip().upper().startswith("CREATE VIRTUAL")]
        tables, other = [], 0
        for name, sql in sorted(master):
            shadow = any(name.startswith(v + "_") for v in virtual)
            if name in virtual or shadow or name.startswith("sqlite_") or not safe_ident(name):
                other += 1
                continue
            cols = [(c[1], c[2]) for c in conn.execute(f"PRAGMA table_info({quote_ident(name)})")]
            entry = {"name": name, "columns": [{"name": c if safe_ident(c) else "<col>",
                                                "type": re.sub(r"[^A-Za-z]", "", t or "")[:12].upper()} for c, t in cols],
                     "rows": conn.execute(f"SELECT COUNT(*) FROM {quote_ident(name)}").fetchone()[0], "json_columns": {}}
            for c, t in cols:
                if not safe_ident(c):
                    continue
                samples = []
                for (v,) in conn.execute(f"SELECT {quote_ident(c)} FROM {quote_ident(name)} WHERE {quote_ident(c)} IS NOT NULL LIMIT {PROBE_MAX_ELEMENTS}"):
                    if isinstance(v, str) and v[:1] in "{[":
                        try:
                            samples.append(json.loads(v))
                        except ValueError:
                            pass
                if samples:
                    entry["json_columns"][c] = probe_records([s for s in samples if isinstance(s, (dict, list))])
            tables.append(entry)
        return {"kind": "sqlite", "tables": tables, "other_tables": other}
    finally:
        conn.close()


def run_probe(target):
    """(output, exit code) for --probe. Reads only; prints structure only."""
    path = Path(os.path.expanduser(target))
    try:
        if path.is_dir():
            result = probe_folder(path)
        elif not path.is_file():
            return {"error": "nothing to probe at that path"}, EXIT_NO_TRANSCRIPT
        else:
            with open(path, "rb") as f:
                magic = f.read(16)
            result = probe_sqlite(path) if magic.startswith(b"SQLite format 3") else probe_file(path)
    except DocumentTooLarge as exc:
        return {"error": str(exc)}, 4
    except (ContainerProblem, sqlite3.Error, OSError) as exc:
        return {"error": f"cannot probe: {type(exc).__name__}"}, 2
    return {"probe": result, "note": "structure only: names of fields, types, counts and short identifier values; no content"}, 0


# ---- replica: a synthetic copy of the current session, and the pull-request bundle ------------------------------------------------

REPLICA_FILL = "lorem ipsum dolor sit amet consectetur "
REPLICA_MAX_TEXT = 400
REPLICA_BASE_MS = int(datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
PATHLIKE_RE = re.compile(r"^(?:~?/|\./|[A-Za-z]:[\\/])[^\s]*$|^[^\s/\\]+(?:/[^\s/\\]+){1,}$")
CWD_KEYS = frozenset({"cwd", "workspace_root", "workspaceRoot", "root", "rootPath", "folder"})
PLAIN_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class Leak(Exception):
    """The replica still carries something from the source."""


def filler_text(n):
    n = max(0, min(n, REPLICA_MAX_TEXT))
    return (REPLICA_FILL * (n // len(REPLICA_FILL) + 1))[:n]


class Anonymizer:
    """Rewrite JSON structure-preservingly: types, order, nesting and counts stay; text, ids, paths and names do not.

    The mapping's [replica] table declares what is safe to keep (keep_paths, keep_patterns) and which paths hold
    ids (id_paths) or shell commands (command_paths), all as normalised paths like messages[].content[].name."""

    def __init__(self, cfg, session_id):
        self.keep_paths = set(cfg.get("keep_paths", []))
        self.id_paths = set(cfg.get("id_paths", []))
        self.command_paths = set(cfg.get("command_paths", []))
        patterns = list(cfg.get("keep_patterns", []))
        self.keep_re = re.compile("|".join(f"(?:{p})" for p in patterns)) if patterns else None
        self.ids = {session_id: "replica_session"} if session_id else {}
        self.keys, self.paths, self.exempt, self.shift = {}, {}, set(), 0

    def learn_time(self, docs):
        """The earliest timestamp anywhere decides one shift for all, so every interval stays the same."""
        found = []

        def walk(v, key=""):
            if isinstance(v, dict):
                for k, x in v.items():
                    walk(x, str(k))
            elif isinstance(v, list):
                for x in v:
                    walk(x, key)
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and TIME_KEY_RE.search(key) and (1e9 <= v < 1e14):
                found.append(v * 1000 if v < 1e11 else v)
            elif isinstance(v, str) and ISO_RE.match(v):
                ts = parse_ts(v)
                if ts is not None:
                    found.append(ts * 1000)
        for d in docs:
            walk(d)
        self.shift = int(REPLICA_BASE_MS - min(found)) if found else 0

    def new_id(self, real):
        if real not in self.ids:
            self.ids[real] = f"id{len(self.ids):07d}"
        return self.ids[real]

    def key(self, real):
        if real in self.keys:
            return self.keys[real]
        keep = PLAIN_IDENT_RE.match(real) and len(real) <= 41 and not id_like(real) and redact(real) == real
        self.keys[real] = real if keep else f"key{len(self.keys):04d}"
        return self.keys[real]

    def text(self, s):
        if not s:
            return s
        if self.keep_re is None:
            return filler_text(len(s))
        out, pos = [], 0
        for m in self.keep_re.finditer(s):
            out += [filler_text(m.start() - pos), m.group(0)]
            pos = m.end()
        out.append(filler_text(len(s) - pos))
        return "".join(out)

    def string(self, s, path, key):
        if path in self.keep_paths:
            self.exempt.add(s)
            return s
        if path in self.id_paths:
            return self.new_id(s)
        if path in self.command_paths:                      # a command becomes its class: a test run or a plain command
            cls = "python3 -m pytest -q" if any(verify_kind(w) for w in split_commands(s)) else "ls"
            self.exempt.add(cls)
            return cls
        if key in PROBE_NAME_FIELDS and safe_value(s):
            return s
        if ISO_RE.match(s):
            ts = parse_ts(s)
            if ts is not None:
                return datetime.fromtimestamp(ts + self.shift / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if " " not in s and "\n" not in s and len(s) <= 300 and PATHLIKE_RE.match(s):
            if key in CWD_KEYS:
                return "~/project"
            if s not in self.paths:
                self.paths[s] = f"~/project/file{len(self.paths) + 1}{os.path.splitext(s)[1][:6] if PLAIN_IDENT_RE.match(os.path.splitext(s)[1][1:] or 'x') else ''}"
            return self.paths[s]
        if " " not in s and id_like(s) and len(s) <= 120:
            return self.new_id(s)
        return self.text(s)

    def walk(self, v, path="", key=""):
        if isinstance(v, dict):
            return {self.key(str(k)): self.walk(x, f"{path}.{k}" if path else str(k), str(k)) for k, x in v.items()}
        if isinstance(v, list):
            return [self.walk(x, f"{path}[]", key) for x in v]
        if isinstance(v, str):
            return self.string(v, path, key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and TIME_KEY_RE.search(key) and 1e9 <= v < 1e14:
            return v + self.shift // 1000 if v < 1e11 else v + self.shift
        return v


def strings_in(value, acc):
    if isinstance(value, dict):
        for k, v in value.items():
            acc.add(str(k))
            strings_in(v, acc)
    elif isinstance(value, list):
        for v in value:
            strings_in(v, acc)
    elif isinstance(value, str):
        acc.add(value)
    return acc


def leak_check(source_docs, replica_docs, anon):
    """No string of 6 or more characters from the source may appear in the replica, unless it is a plain identifier
    (a type or field name) or a value the mapping declared safe to keep. Returns how many source strings leaked."""
    source = {s for s in strings_in(source_docs, set()) if len(s) >= 6 and not PLAIN_IDENT_RE.match(s)}
    leaked = set()
    for r in strings_in(replica_docs, set()):
        if r in anon.exempt:
            continue
        probe = anon.keep_re.sub("", r) if anon.keep_re else r
        for s in source:
            if s in probe:
                leaked.add(s)
    return len(leaked)


def replica_inputs(reader, mapping):
    """[(file name inside the replica, [documents or records])] for a reader the replica can copy, else None."""
    if reader.kind == "json_document":
        out = []
        for doc in reader.spec["documents"]:
            file = reader.folder / doc["file"].replace("{id}", reader.session_id)
            if file.is_file():
                out.append((doc["file"], read_document(file)))
        return out
    if reader.kind == "jsonl":
        files = []
        for n, src in enumerate(reader.sources(mapping)):
            records = [json.loads(ln) for ln in src.lines if ln.strip()]
            files.append(("{id}.jsonl" if src.main else f"{{id}}/subagents/agent-{n:04d}.jsonl", records))
        return files
    return None


def make_replica(reader, mapping, host):
    """Write the replica; returns (path, summary) or raises Leak/ContainerProblem. Nothing is written when the check fails."""
    items = replica_inputs(reader, mapping)
    if items is None:
        raise ContainerProblem("a replica cannot be made from this kind of storage yet")
    session_id = getattr(reader, "session_id", None) or "session"
    anon = Anonymizer(mapping.get("replica") or {}, session_id)
    docs = [d for _, d in items]
    anon.learn_time(docs)
    rewritten = [(name, [anon.walk(r) for r in d] if name.endswith(".jsonl") else anon.walk(d)) for name, d in items]
    leaked = leak_check(docs, [d for _, d in rewritten], anon)
    if leaked:
        raise Leak(f"{leaked} source strings would survive in the replica")
    base = Path(tempfile.gettempdir()) / "workflow-reviewer"
    fresh = not base.exists()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if fresh:
        os.chmod(base, 0o700)
    target = base / f"replica-{host}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    n = 1
    while target.exists():
        n += 1
        target = base / f"replica-{host}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{n}"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    folder = target / "replica_session" if reader.kind == "json_document" else target
    if folder != target:
        folder.mkdir(mode=0o700)
        os.chmod(folder, 0o700)
    for name, data in rewritten:
        dest = folder / name.replace("{id}", "replica_session")
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = "\n".join(json.dumps(r, ensure_ascii=False) for r in data) + "\n" if name.endswith(".jsonl") else json.dumps(data, ensure_ascii=False, indent=1)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return folder, {"files": len(rewritten), "source_strings_checked": len(strings_in(docs, set())), "leaked": 0}


def home_relative(path):
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):].replace("\\", "/") if text.startswith(home) else text


def newest_replica(host):
    base = Path(tempfile.gettempdir()) / "workflow-reviewer"
    found = sorted(base.glob(f"replica-{host}-*"), key=lambda p: p.stat().st_mtime) if base.is_dir() else []
    return found[-1] if found else None


def pr_bundle(out, host, repo_url="justin-nicky/workflow-reviewer-skill"):
    """What the person needs to open a pull request, as text. Nothing here runs a command."""
    adapter = user_adapter_dir() / f"{host}.toml"
    replica = newest_replica(host)
    t, tools, cov = out["tokens"], out["tools"], out["coverage"]
    expected = {"turns": out["window"]["turns_total"], "interruptions": (out["interruptions"] or {}).get("count"),
                "tool_calls": tools["total"], "tool_calls_by_category": tools["by_category"], "tool_errors": tools["errors"],
                "verify_runs": out["verify"]["total"], "subagents": sum(a.get("actors", 1) for a in out["actors"] if a["actor"] != "main"),
                "models": [m for a in out["actors"] for m in a["models"]][:3], "version": cov.get("transcript_version")}
    if "not_measurable" not in t:
        expected["tokens"] = {k: t[k] for k in ("api_calls", "fresh_input", "cache_read", "cache_write", "output")}
    branch = f"add-{host}-adapter"
    replica_text = home_relative(replica) if replica else "(none yet: run --make-replica first)"
    in_shell = lambda text: "$HOME" + text[1:] if text.startswith("~") else text      # a ~ inside quotes is not expanded by the shell
    commands = [
        f"gh repo fork {repo_url} --clone --remote",
        "cd workflow-reviewer-skill",
        f"git checkout -b {branch}",
        f"cp \"{in_shell(home_relative(adapter))}\" skills/workflow-reviewer/scripts/mappings/{host}.toml",
        f"cp -R \"{in_shell(replica_text)}\" tests/fixtures/golden/{host}",
        f"git add skills/workflow-reviewer/scripts/mappings/{host}.toml tests/fixtures/golden/{host}",
        f"git commit -m \"feat: {host} adapter\"",
        f"git push -u origin {branch}",
        f"gh pr create --repo {repo_url} --title \"feat: {host} adapter\" --body \"Adapter made with the adapter flow (version {expected['version']}). Golden replica and expected numbers are in the PR.\"",
    ]
    return {"adapter_path": home_relative(adapter), "adapter_exists": adapter.is_file(), "replica_path": replica_text,
            "expected_for_golden_test": expected, "commands": commands,
            "note": "Nothing was run. Review the files first (the adapter is a mapping, the replica is synthetic), then run the commands yourself."}


def store_activity(raw, repo):
    """Most recent activity of a sqlite or json_document host's session for this working directory (None when there is none)."""
    kind = container_kind(raw)
    try:
        if kind == "json_document":
            spec = json_document_spec(raw)
            here = {str(repo.resolve()), str(repo)}
            times = [s["last"] for s in document_sessions(spec) if (lambda c: c is None or c in here or (c and str(Path(c).resolve()) in here))(session_cwd(s, spec))]
            return max(times, default=None)
        if kind == "sqlite":
            spec = sqlite_spec(raw)
            default = (raw.get("container") or {}).get("default_path")
            db = Path(os.path.expanduser(default)) if default else None
            if db is None or not db.is_file():
                return None
            conn = open_sqlite_readonly(db)
            try:
                check_schema_in_db(conn, spec)
                here = {str(repo.resolve()), str(repo)}
                times = [s["last"] for s in list_sqlite_sessions(conn, spec)
                         if s["last"] is not None and (s["cwd"] in here or str(Path(s["cwd"]).resolve()) in here)]
                return max(times, default=None)
            finally:
                conn.close()
        newest = [p.stat().st_mtime for p in host_candidates_for(raw)]
        return max(newest, default=None)
    except (ContainerProblem, sqlite3.Error, OSError):
        return None


def host_candidates_for(raw):
    out = []
    for pattern in ((raw.get("container") or {}).get("globs") or []):
        out += [Path(m) for m in glob.glob(os.path.expanduser(pattern), recursive=True) if Path(m).is_file()]
    return out


def choose_host(args, repo, hosts):
    """Which host's session to review: environment marker, else the host with the most recent session here, else exit 6.

    Returns {"host", "how", "paths"?} or {"exit", "output"}."""
    candidates = [h for h in hosts if h != "kiro"]
    marked = [h for h in candidates if any(os.environ.get(v) for v in ((hosts[h]["raw"].get("detect") or {}).get("env") or []))]
    scope, how = (marked, "env marker") if marked else (candidates, "data presence")
    jsonl_hosts = [h for h in scope if h in HOSTS]
    host, paths, ambiguous = discover(args, repo, jsonl_hosts, with_host=True) if jsonl_hosts else (None, [], None)
    if ambiguous:
        return {"exit": 3, "output": ambiguous}
    found = [(paths[0].stat().st_mtime, host, paths)] if paths else []
    for h in scope:
        if h not in HOSTS:
            last = store_activity(hosts[h]["raw"], repo)
            if last is not None:
                found.append((last, h, []))
    if found:
        _, best, best_paths = max(found, key=lambda f: f[0])
        return {"host": best, "how": how, "paths": best_paths}
    if marked:                                        # the marker names the host; it has no session here, so the run reports that
        return {"host": marked[0], "how": "env marker", "paths": []}
    output = degraded_report("auto", repo, searched_locations("auto", repo))
    output["hosts_checked"] = sorted(scope)
    return {"exit": EXIT_NO_TRANSCRIPT, "output": output}


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
    findings = None
    if (args.make_replica or args.pr_bundle) and (args.report or args.save_adapter or args.events or args.sample is not None or args.list_turns):
        emit({"error": "--make-replica and --pr-bundle cannot be combined with --report, --save-adapter, --events, --sample or --list-turns"}, args.format, redact_on)
        return 2
    if args.save_adapter and (args.mapping or args.report):
        emit({"error": "--save-adapter reads the drafted mapping from stdin: do not combine it with --mapping or --report"}, args.format, redact_on)
        return 2
    if args.report:
        problem = None
        if args.report != "-":
            problem = "--report reads the findings from stdin: use --report -"
        elif args.mapping == "-":
            problem = "--report and --mapping cannot both read stdin; pass the mapping as a file"
        elif args.events or args.sample is not None or args.list_turns:
            problem = "--report cannot be combined with --events, --sample or --list-turns"
        if problem:
            emit({"error": problem}, args.format, redact_on)
            return 2
        findings, errors = read_findings(sys.stdin)
        if errors:
            emit({"error": "findings are not valid; fix these and send them again", "errors": errors}, args.format, redact_on)
            return EXIT_FINDINGS
    if args.probe:                                    # structure only; reads, never writes
        out, code = run_probe(args.probe)
        emit(out, args.format, redact_on)
        return code
    hosts, adapter_notes = registry()
    ACTIVE_HOSTS.clear()
    ACTIVE_HOSTS.update(hosts)
    if args.host != "auto" and args.host not in hosts:
        emit({"error": f"unknown host '{args.host}'", "hosts": sorted(hosts)}, args.format, redact_on)
        return 2
    save_text = None
    if args.save_adapter:
        problem = None
        if not HOST_NAME_RE.match(args.save_adapter):
            problem = "adapter name must be lowercase letters, digits, - or _"
        elif args.mapping or args.report:
            problem = "--save-adapter reads the drafted mapping from stdin: do not combine it with --mapping or --report"
        elif args.events or args.sample is not None or args.list_turns:
            problem = "--save-adapter cannot be combined with --events, --sample or --list-turns"
        if problem:
            emit({"error": problem}, args.format, redact_on)
            return 2
        save_text = sys.stdin.read()
    if args.host == "kiro":
        emit(degraded_report("kiro", repo), args.format, redact_on)
        return 0
    override = None
    if args.mapping or save_text is not None:         # an override decides how records are stored, so read it first
        text = save_text if save_text is not None else (sys.stdin.read() if args.mapping == "-" else Path(args.mapping).read_text(encoding="utf-8"))
        try:
            override = load_mapping(text=text)
        except ValueError as exc:
            emit({"error": f"the mapping is not valid TOML: {str(exc).splitlines()[0][:100]}"}, args.format, redact_on)
            return 2
        override.setdefault("capabilities", {})
        problems = mapping_problems(override) if save_text is not None else \
            [e for e in [schema_check(override)[0] or container_error(container_kind(override))] if e]
        if problems:
            emit({"error": ("the mapping cannot be used: " + problems[0]) if save_text is not None else problems[0], "errors": problems}
                 if save_text is not None else {"error": problems[0]}, args.format, redact_on)
            return 2
        if save_text is not None:
            builtin = load_mapping(BUILTIN_NAMES[override["host"]]) if override.get("host") in BUILTIN_NAMES else None
            weaker = (weakened_checks(override, builtin) if builtin is not None and override.get("host") != "kiro" else []) \
                + widened_null_zero(override, builtin)
            if weaker:
                emit({"error": WEAKEN_ERROR, "problems": weaker}, args.format, redact_on)
                return 2
    chosen_by = "--mapping" if override is not None else "--host" if args.host != "auto" else None
    if override is None and args.host == "auto" and not args.paths:
        decision = choose_host(args, repo, hosts)
        if decision.get("exit"):
            emit(decision["output"], args.format, redact_on)
            return decision["exit"]
        args.host, chosen_by = decision["host"], decision["how"]
        if decision.get("paths"):
            args.paths = [str(p) for p in decision["paths"]]
    store_raw = override if override is not None and container_kind(override) in ("sqlite", "json_document") else None
    if store_raw is None and override is None and args.host in hosts and container_kind(hosts[args.host]["raw"]) in ("sqlite", "json_document"):
        store_raw = hosts[args.host]["raw"]
        store_raw.setdefault("capabilities", {})
    if store_raw is not None:
        opener = open_json_session if container_kind(store_raw) == "json_document" else open_sqlite_session
        reader, early = opener(args, repo, store_raw)
        if early:
            emit(early[0], args.format, redact_on)
            return early[1]
        host = store_raw.get("host", "unknown")
        first_lines = reader.first_lines()
    else:
        paths = [Path(p) for p in args.paths]
        resolved = []
        for p in paths:
            if p.is_dir():
                main_file = main_transcript(p.resolve())
                if main_file is None:
                    emit({"error": f"no main transcript found for session folder {p}",
                          "note": "pass the .jsonl transcript, or a folder that holds or sits beside one"},
                         args.format, redact_on)
                    return EXIT_NO_TRANSCRIPT
                p = main_file
            resolved.append(p)
        paths = resolved
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
            emit(degraded_report(args.host, repo, searched_locations(args.host, repo),
                              override.get("skill_folders") if override else None), args.format, redact_on)
            return EXIT_NO_TRANSCRIPT
        reader = JsonlReader(paths)
        first_lines = reader.first_lines()
        host = override.get("host", args.host) if override is not None and args.host == "auto" else args.host
        if override is None and args.host == "auto":
            host = detect_host(first_lines, args.host)
            chosen_by = "transcript shape"
        elif override is not None:
            host = override.get("host", "unknown")
    if override is not None:
        raw_mapping, source = restrict_null_zero(override, load_mapping(BUILTIN_NAMES[host]) if host in BUILTIN_NAMES else None), "override"
    elif host in hosts:
        raw_mapping, source = hosts[host]["raw"], "builtin" if hosts[host]["builtin"] else "user"
    else:
        raw_mapping, source = load_mapping(HOSTS[host]), "builtin"
    schema_note = schema_check(raw_mapping)[1]
    notes = ([schema_note] if schema_note else []) + list(getattr(reader, "notes", [])) + adapter_notes
    mapping, variant_info = choose_variant(raw_mapping, first_lines, version=getattr(reader, "version", None))
    events, raw = [], []
    stats = {"lines": 0, "classified": 0, "unseen_types": {}, "ignored_types": {},
             "truncated_last_line": False, "version": None, "skill_list_unparsed": 0}
    if getattr(reader, "version", None) is not None:
        stats["version"] = str(reader.version)
    metas, main_ids, records, parsed = {}, set(), [], []
    sub_cfg = mapping.get("subagents") or {}
    for src in reader.sources(mapping):
        actor = "main" if src.main else f"subagent:{src.stem}"
        lines = src.lines
        if not src.main:
            metas[actor] = read_subagent_meta(src.file, mapping, lines) if src.file else {}
            if src.link:
                metas[actor]["fixed_link"] = src.link
            lines, history = skip_copied_history(lines, mapping)
            if history:
                metas[actor]["history"] = history
        else:
            raw += lines
            if sub_cfg.get("self_id"):
                main_ids.add(get_first(first_record(lines) or {}, sub_cfg["self_id"]))
        for ln in lines:                                  # the raw records, for the second counts
            if ln.strip():
                try:
                    records.append((actor, json.loads(ln)))
                except ValueError:
                    pass
        parsed.append((actor, src.label, [ln.strip() for ln in lines if ln.strip()]))
        ev, st = parse_lines(lines, mapping, actor)
        if not src.main and metas[actor].get("history") and st["version"] is None:
            st["version"] = metas[actor]["history"]["version"]
        for e in ev:
            e["file"] = src.label
        events += ev
        stats["lines"] += st["lines"]
        stats["classified"] += st["classified"]
        stats["truncated_last_line"] |= st["truncated_last_line"]
        stats["skill_list_unparsed"] += st["skill_list_unparsed"]
        stats["version"] = stats["version"] or st["version"]
        for bucket in ("unseen_types", "ignored_types"):
            for k, v in st[bucket].items():
                stats[bucket][k] = stats[bucket].get(k, 0) + v
    events = dedupe(events)
    stats["duplicate_calls"] = dedupe_calls(events)
    placed = set()
    if events and all(e["ts"] is not None for e in events):
        events.sort(key=lambda e: e["ts"])
    else:
        events, placed = place_subagents(events)
    for i, ev in enumerate(events):
        ev["i"] = i
    assign_turns(events)
    if mapping.get("interrupt_on_repeated_prompt"):
        mark_repeated_prompts(events)
    links = link_subagents(events, metas, mapping, placed, main_ids - {None})
    cut = reviewer_cut(events)
    reviewer_turns = sorted({e["turn"] for e in events if is_turn_start(e) and is_invocation(e)})
    unlinked = [a for a, v in links.items() if v["link"] == "none"]
    whole_session = cut is None and args.from_turn is None and args.to_turn is None
    if args.events:
        lo, _, hi = args.events.partition(":")
        picked = [e for e in events if int(lo) <= e["i"] <= int(hi or lo)]
        out = {"events": [event_view(e) for e in picked[:MAX_EVENTS]]}
        if len(picked) > MAX_EVENTS:
            out["truncated"] = f"showing {MAX_EVENTS} of {len(picked)} events; narrow the range"
    elif args.sample is not None:
        out = {"sample": [ln[:300] for ln in raw if ln.strip()][:min(args.sample, MAX_SAMPLE)]}
    else:
        # a subagent no spawn explains belongs to no window; only a whole-session run lists it
        window = [e for e in events if whole_session or not e.get("unplaced")]
        main_turns = sorted({e["turn"] for e in window if is_turn_start(e)})
        d_from, d_to, d_note = default_window(reviewer_turns, main_turns)
        if args.list_turns:
            rows = turn_rows(window, mapping, args.verify_patterns)
            firsts = {e["turn"]: e for e in window if is_turn_start(e)}
            listed = [{"index": r["turn"], "time": iso(firsts[r["turn"]]["ts"]) if r["turn"] in firsts else None,
                       "preview": r["preview"], **({"reviewer": True} if r["turn"] in reviewer_turns else {})} for r in rows]
            if args.all or len(listed) <= LIST_TURNS_SHOWN:
                header = f"showing all {len(listed)} turns"
            else:
                header = f"showing {LIST_TURNS_SHOWN} of {len(listed)} turns (use --all for every turn)"
                listed = listed[-LIST_TURNS_SHOWN:]
            out = {"header": header, "turns": listed,
                   "default_window": {"from_turn": d_from, "to_turn": d_to if d_to is not None else (main_turns[-1] if main_turns else 0)}}
        else:
            explicit = args.from_turn is not None or args.to_turn is not None
            if explicit:                  # a bound the user left out is the plain one: session start, just before the latest invocation
                lo = args.from_turn or 1
                hi = args.to_turn if args.to_turn is not None else (reviewer_turns[-1] - 1 if reviewer_turns else None)
                window_note = None
            else:
                lo, hi, window_note = d_from, d_to, d_note
            window = [e for e in window if e["turn"] >= lo and (hi is None or e["turn"] <= hi)]
            # the window is frozen: nothing after its last record is read, for any number or check
            stats, records, prior, first_kept = frozen_window(parsed, events, mapping, stats, lo, hi, whole_session)
            if first_kept is not None and len(first_kept) < len(first_lines):     # the variant scores, from the window's lines too
                rescored = choose_variant(raw_mapping, first_kept, version=getattr(reader, "version", None))[1]
                if rescored["variant"] == variant_info["variant"]:
                    variant_info = rescored
            out = analyse(window, stats, mapping, args.verify_patterns)
            last_ts = max((e["ts"] for e in events if e["ts"] is not None and (hi is None or e["turn"] <= hi)
                           and (whole_session or not e.get("unplaced"))), default=None)
            lag = None
            if last_ts is not None and datetime.now(timezone.utc).timestamp() - last_ts < LAG_SECONDS:
                lag = "latest turn may be incomplete: last record is under %ds old" % LAG_SECONDS
            drift = drift_report(mapping, stats, window, args.drift_threshold)
            whole = lo <= 1 and (hi is None or hi >= max(main_turns, default=0))
            health = health_report(mapping, stats, window, records, drift, args.drift_threshold, prior, whole)
            drift = settle_drift(drift, health)
            out["coverage"] = {"host": host, "tested_versions": mapping.get("tested_versions"),
                               "mapping_source": source, "variant": variant_info["variant"], "drift": drift,
                               "label": mapping_label(mapping, source), "host_chosen_by": chosen_by or "--host",
                               "health": health_view(health, mapping, stats["version"], args.health_all),
                               "ignored_types": with_other(stats["ignored_types"], TOP_TYPES), "transcript_version": stats["version"],
                               "version_note": version_note(stats["version"], mapping.get("tested_versions"), drift),
                               "not_measurable": not_measurable(mapping),
                               "capabilities": mapping["capabilities"], "last_record_ts": iso(last_ts),
                               "lag_note": lag, "window_note": window_note,
                               "truncated_last_line": stats["truncated_last_line"]}
            notes = notes + coverage_notes(mapping, records)
            if notes:
                out["coverage"]["notes"] = notes
            if reader.kind != "jsonl":
                out["coverage"]["container"] = reader.kind
                out["coverage"]["storage"] = reader.storage_info()
            checked = out["tokens"].get("context_check") if isinstance(out.get("tokens"), dict) else None
            if checked and checked["mismatches"]:
                out["coverage"].setdefault("warnings", []).append(
                    f"token cross-check: {checked['mismatches']} of {checked['calls']} calls where input + cache_read "
                    "differs from context_input_tokens")
            if unlinked:
                out["coverage"]["unlinked_subagents"] = len(unlinked)
                out["coverage"].setdefault("warnings", []).append(
                    f"unlinked subagents: {len(unlinked)} (not placed in any window)")
            if stats["skill_list_unparsed"]:
                out["coverage"].setdefault("warnings", []).append("skill list found but not parsed; offered list may be incomplete")
            if health["unattributed_types"]:                    # unseen record types no metric was hinted at: listed, as a warning
                out["coverage"].setdefault("warnings", []).append(
                    "unseen record types not tied to any metric: " + ", ".join(health["unattributed_types"]))
            if variant_info["scores"]:
                out["coverage"]["variant_choice"] = {"scores": variant_info["scores"],
                                                     "reason": variant_info["reason"]}
            non_turn = {}
            for e in window:
                if e["actor"] == "main" and e["role"] == "user" and e["non_turn"] and e["text"].strip():
                    non_turn[e["non_turn"]] = non_turn.get(e["non_turn"], 0) + 1
            out["window"] = {"reviewer_invocation_found": cut is not None, "events": len(window),
                             "non_turn_user_records": non_turn,
                             "from_turn": lo, "to_turn": args.to_turn, "reviewer_turns": reviewer_turns}
            out["interruptions"] = interruption_summary(window, mapping)
            turn_errors = turn_error_summary(window, mapping)
            if turn_errors is not None:
                out["turn_errors"] = turn_errors
            if mapping["capabilities"].get("has_timestamps"):
                out["time"] = time_summary(window, args.idle_threshold, mapping.get("tool_time_source") == "result_spans")
            elif has_prompt_clock(mapping):
                last = max((e["turn"] for e in window if e["actor"] == "main"), default=None)
                after = [e for e in events if e["actor"] == "main" and e["prompt_ts"] is not None
                         and last is not None and e["turn"] > last]
                out["time"] = coarse_time_summary(window, min(after, key=lambda e: e["turn"])["prompt_ts"] if after else None)
            else:
                out["time"] = {"not_measurable": "host records no timestamps"}
            all_turns = turn_rows(window, mapping, args.verify_patterns)
            out["window"]["turns_total"] = len(all_turns)
            out["turns"] = cap_rows(all_turns, TOP_ROWS, "turn", "turns", "other")
            out["actors"] = cap_rows(actor_rows(window, mapping, args.verify_patterns, links, metas), TOP_ROWS,
                                     "actor", "actors", "other", keep="main")
            null_unrecorded_numbers(out, mapping)
            out["skills"] = skills_summary(window, host, repo, offered_skills(events), raw_mapping.get("skill_folders"), mapping,
                                      (args.overlap_threshold, args.directive_threshold))
            if out["skills"].get("inventory_note"):
                out["coverage"].setdefault("notes", []).append(out["skills"]["inventory_note"])
    if save_text is not None and "coverage" in out:
        broken = out["coverage"]["health"]["rows"] and [r["metric"] for r in out["coverage"]["health"]["rows"] if r["core"] and r["status"] == "broken"]
        if broken or not out["coverage"]["drift"]["ok"]:
            emit({"error": "a core metric is broken with this mapping; the adapter was not saved", "broken": broken or [],
                  "drift": out["coverage"]["drift"].get("warning")}, args.format, redact_on)
            return 2
        target, problem = save_adapter(args.save_adapter, save_text, stats["version"], args.replace)
        if problem:
            emit({"error": problem}, args.format, redact_on)
            return 2
        emit({"saved": home_relative(target), "host": override.get("host"), "label": "unproven", "made_from_version": stats["version"],
              "health": out["coverage"]["health"]["summary"]}, args.format, False)             # the path is printed as it is
        return 0
    if (args.make_replica or args.pr_bundle) and "coverage" in out:
        if args.pr_bundle:
            emit(pr_bundle(out, host), args.format, False)
            return 0
        try:
            folder, summary = make_replica(reader, mapping, host)
        except Leak as exc:
            emit({"error": f"replica not written: {exc}"}, args.format, redact_on)
            return 2
        except ContainerProblem as exc:
            emit({"error": str(exc)}, args.format, redact_on)
            return 4
        emit({"replica": home_relative(folder), **summary, "note": "synthetic: text, ids, paths and names replaced; numbers, order and nesting kept"}, args.format, False)
        return 0
    if "coverage" in out and "window" in out:
        null_missing_numbers(out)
    code = 2 if out.get("coverage", {}).get("drift", {}).get("ok") is False else 0
    if "coverage" in out and "window" in out:
        out["coverage"]["issue_url"] = issue_url(out, code)
    inventory_names = out["skills"].pop("_inventory_names", None) if isinstance(out.get("skills"), dict) else None
    if findings is not None and "coverage" in out:
        report_main(out, findings, args, redact_on, inventory_names)
        return code
    emit(out, args.format, redact_on)
    return code


if __name__ == "__main__":
    sys.exit(main())
