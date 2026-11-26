"""A usage field present in some records and missing from others: the total sums the records that have it, is marked as
a lower bound ("at least"), and a warning says how many records lack it. Never a pause."""
import json
import re
import shutil
import sqlite3

from conftest import SCRIPT
from test_golden import GOLDEN
from test_health_mutations import edit_messages
from test_missing_fields import run, rows


def first_n(n, match, drop):
    """A mutation that drops a field from the first n matching records (across every file), noting what each held."""
    dropped = []

    def mutate(rs):
        for r in rs:
            if len(dropped) < n and match(r):
                dropped.append(drop(r))
        return rs
    return mutate, dropped


def failed(out, metric):
    return [c for c in rows(out)[metric]["checks"] if not c["passed"]]


def assert_lower_bound(code, out, md, html, field, key, label, missing, of):
    assert code == 0
    t = out["tokens"]
    assert t["lower_bound"] == [key] and isinstance(t[key], int)
    row = rows(out)["tokens"]
    assert row["status"] == "warn" and not out["coverage"]["health"].get("core_broken")
    names = [c["name"] for c in failed(out, "tokens")]
    assert any(re.fullmatch(rf"{field} token counts missing from {missing} of {of} usage records", n) for n in names), names
    assert re.search(rf"{label}: at least [\d.,]+K?\b", md), md
    assert re.search(rf"{label}</th><td>at least [\d.,]+K?<", html)
    gaps = [r for r in out["turns"] + out["actors"] if key in r["tokens"].get("lower_bound", [])]
    assert gaps and all(isinstance(r["tokens"][key], int) for r in gaps)


def test_codex_output_missing_from_two_records_is_a_lower_bound(cli, tmp_path):
    (tmp_path / "clean").mkdir()
    (tmp_path / "cut").mkdir()
    _, clean, _, _ = run(cli, tmp_path / "clean", "codex", lambda rs: rs)
    mutate, dropped = first_n(2, lambda r: r.get("type") == "token_usage_record",
                              lambda r: r["payload"]["usage"].pop("output_tokens"))
    code, out, md, html = run(cli, tmp_path / "cut", "codex", mutate)
    assert len(dropped) == 2
    assert out["tokens"]["output"] == clean["tokens"]["output"] - sum(dropped)          # the other 12, summed
    assert_lower_bound(code, out, md, html, "output", "output", "Output tokens", 2, 14)


def test_devin_fresh_input_missing_from_two_records_is_a_lower_bound(cli, tmp_path):
    (tmp_path / "clean").mkdir()
    (tmp_path / "cut").mkdir()
    _, clean, _, _ = run(cli, tmp_path / "clean", "devin", lambda conn: None)
    dropped = []

    def mutate(conn):                     # two calls lose the field in every record (Devin writes each message twice)
        picked = {}

        def change(m):
            mid = m.get("message_id")
            if m["metadata"].get("metrics") and (mid in picked or len(picked) < 2):
                picked[mid] = m["metadata"]["metrics"].pop("input_tokens")
        edit_messages(conn, change, lambda m: m["role"] == "assistant")
        dropped.extend(picked.values())
    code, out, md, html = run(cli, tmp_path / "cut", "devin", mutate)
    assert len(dropped) == 2
    assert out["tokens"]["fresh_input"] == clean["tokens"]["fresh_input"] - sum(dropped)
    total = clean["tokens"]["api_calls"]
    assert "lower_bound" not in clean["tokens"]          # Devin's own nulls are vouched for by the context cross-check
    assert_lower_bound(code, out, md, html, "input", "fresh_input", "Fresh input", 2, total)


def test_a_field_every_record_has_is_not_a_lower_bound(cli, tmp_path):
    code, out, md, _ = run(cli, tmp_path, "codex", lambda rs: rs)
    assert code == 0 and "lower_bound" not in out["tokens"]
    assert all("lower_bound" not in r["tokens"] for r in out["turns"] + out["actors"])
    assert "at least" not in md


def test_the_metrics_markdown_marks_the_lower_bound(cli, tmp_path):
    mutate, dropped = first_n(2, lambda r: r.get("type") == "token_usage_record",
                              lambda r: r["payload"]["usage"].pop("output_tokens"))
    run(cli, tmp_path, "codex", mutate)
    files = sorted(tmp_path.glob("g/rollout-*.jsonl"))
    md = cli.run(*files, "--host", "codex", fmt="md").stdout
    assert re.search(r"^- output: at least [\d.,]+K?$", md, re.M), md
    assert "- lower_bound: " in md and '"output":"at least ' in md


# ---- null_means_zero: Devin writes some counts as null; a null counts as 0 only when the context cross-check vouches for it ----


DEVIN_TOML = SCRIPT.parent / "mappings" / "devin.toml"
NULL_ZERO_PATHS = {"fresh_input": "input_tokens", "cache_read": "cache_read_tokens"}


def devin(cli, name, *args):
    return cli.json(GOLDEN / f"devin-{name}" / "sessions.db", "--host", "devin", "--session-id", f"golden-devin-{name}", "--health-all", *args)


def all_checks(out, metric="tokens"):
    return rows(out)[metric]["checks"]


def test_only_devin_declares_null_means_zero_and_only_for_input_and_cache_read():
    from tomlcompat import tomllib
    for f in sorted((SCRIPT.parent / "mappings").glob("*.toml")):
        raw = tomllib.loads(f.read_text(encoding="utf-8"))
        assert raw.get("null_means_zero", []) == (["fresh_input", "cache_read"] if f.name == "devin.toml" else []), f.name


def test_every_null_devin_lists_passes_the_context_cross_check_with_the_null_as_zero():
    """The evidence for devin.toml's null_means_zero: on every golden, a record with a null input or cache_read and a context
    size adds up with the null counted as 0. A record with no context at all is an interrupted call, not vouched for."""
    vouched = unverified = 0
    for db in sorted(GOLDEN.glob("devin-*/sessions.db")):
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        for (text,) in conn.execute("SELECT chat_message FROM message_nodes"):
            m = json.loads(text)
            metrics = (m.get("metadata") or {}).get("metrics")
            if m.get("role") != "assistant" or not metrics:
                continue
            nulls = [p for p in NULL_ZERO_PATHS.values() if p in metrics and metrics[p] is None]
            if not nulls:
                continue
            if metrics.get("context_input_tokens") is None:
                unverified += 1
                continue
            assert (metrics["input_tokens"] or 0) + (metrics["cache_read_tokens"] or 0) == metrics["context_input_tokens"]
            vouched += 1
        conn.close()
    assert vouched >= 4 and unverified >= 2


def test_devin_goldens_without_interruptions_have_no_gap(cli):
    for name in ("subagent", "explicit-only"):
        code, out = devin(cli, name)
        assert code == 0 and "lower_bound" not in out["tokens"], name
        assert not [c for c in all_checks(out) if c["layer"] == "partial"], name


def test_an_interrupted_call_is_named_by_its_cause_and_skipped_by_the_cross_check(cli):
    code, out = devin(cli, "error-interrupted")
    assert code == 0 and sorted(out["tokens"]["lower_bound"]) == ["cache_read", "fresh_input"]
    partial = [c for c in all_checks(out) if c["layer"] == "partial"]
    assert [c["name"] for c in partial] == ["1 of 5 calls was interrupted; its token usage wasn't recorded"]
    assert rows(out)["tokens"]["status"] == "warn"
    ctx = next(c for c in all_checks(out) if c["name"] == "tokens: input + cache_read vs context_input_tokens")
    assert ctx["passed"] and ctx["skipped"] == "skipped for 1 interrupted call"


def test_a_null_the_cross_check_does_not_vouch_for_is_a_gap(cli, tmp_path):
    def mutate(conn):                     # one call with a null cache_read: its context no longer adds up
        picked = []

        def change(m):
            mt = m["metadata"].get("metrics")
            if mt and mt.get("cache_read_tokens") is None and mt.get("context_input_tokens") is not None \
                    and (not picked or picked[0] == m.get("message_id")):
                picked.append(m.get("message_id"))
                mt["context_input_tokens"] += 7
        edit_messages(conn, change, lambda m: m["role"] == "assistant")
    code, out, md, _ = run(cli, tmp_path, "devin", mutate)
    assert code == 0 and out["tokens"]["lower_bound"] == ["cache_read"]
    names = [c["name"] for c in all_checks(out) if c["layer"] == "partial"]
    assert names == ["cache-read token counts missing from 1 of 12 usage records"], names


def test_other_hosts_treat_an_explicit_null_as_a_gap(cli, tmp_path):
    mutate, dropped = first_n(2, lambda r: r.get("type") == "token_usage_record",
                              lambda r: r["payload"]["usage"].update(output_tokens=None))
    code, out, md, html = run(cli, tmp_path, "codex", mutate)
    assert code == 0 and out["tokens"]["lower_bound"] == ["output"]
    assert any(c["name"] == "output token counts missing from 2 of 14 usage records" for c in all_checks(out))


def test_a_mapping_given_on_the_command_line_cannot_widen_null_means_zero(cli, tmp_path):
    db = tmp_path / "sessions.db"
    shutil.copy(GOLDEN / "devin-subagent" / "sessions.db", db)
    conn = sqlite3.connect(db)
    picked = []

    def change(m):                         # one call (both of its records) writes output as null
        mt = m["metadata"].get("metrics")
        if mt and (not picked or picked[0] == m.get("message_id")):
            picked.append(m.get("message_id"))
            mt["output_tokens"] = None
    edit_messages(conn, change, lambda m: m["role"] == "assistant")
    conn.commit()
    conn.close()
    wider = DEVIN_TOML.read_text(encoding="utf-8").replace('null_means_zero = ["fresh_input", "cache_read"]',
                                                           'null_means_zero = ["fresh_input", "cache_read", "output"]')
    assert '"output"]' in wider
    code, out = cli.json(db, "--session-id", "golden-devin-subagent", "--mapping", "-", stdin=wider)
    assert out["tokens"]["lower_bound"] == ["output"], out["tokens"].get("lower_bound")


def test_save_adapter_refuses_to_add_or_widen_null_means_zero(cli, tmp_path):
    from test_user_adapters import MAPPING, save
    wider = DEVIN_TOML.read_text(encoding="utf-8").replace('null_means_zero = ["fresh_input", "cache_read"]',
                                                           'null_means_zero = ["fresh_input", "cache_read", "output"]')
    for text, name in ((wider, "devin-mine"), ('null_means_zero = ["output"]\n' + MAPPING, "hx")):
        (tmp_path / name).mkdir()
        proc = save(cli, tmp_path / name, text, name=name)
        out = json.loads(proc.stdout)
        assert proc.returncode == 2 and out["error"] == "an adapter may add mappings, not remove or loosen checks", out
        assert any("null_means_zero" in p for p in out["problems"]), out


def test_the_built_in_list_or_a_narrower_one_is_not_a_widening():
    import importlib.util
    spec = importlib.util.spec_from_file_location("tm_nz", SCRIPT)
    tm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tm)
    builtin = tm.load_mapping(text=DEVIN_TOML.read_text(encoding="utf-8"))
    same = dict(builtin)
    narrower = {**builtin, "null_means_zero": ["cache_read"]}
    assert tm.widened_null_zero(same, builtin) == [] and tm.widened_null_zero(narrower, builtin) == []
    assert tm.widened_null_zero({**builtin, "null_means_zero": ["output"]}, builtin)
    assert tm.widened_null_zero({"null_means_zero": ["cache_read"]}, None)
