"""--make-replica (a synthetic copy of the session, leak-checked) and --pr-bundle (commands printed, never run)."""
import importlib.util
import json
import os
import stat

import pytest
from fixtures import devin as dv
from fixtures.cline import assistant, failed_command, ok_command, tool_result, tool_use, user_input, write_session

from conftest import SCRIPT

spec = importlib.util.spec_from_file_location("tm_replica", SCRIPT)
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)

SECRET = "sk_" "live_abcdefghijklmnop0123456789"
EMAIL = "person@example.com"
HOME = "/Users/alex/private/project"
UUID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
PLANTED = (SECRET, EMAIL, HOME, UUID, "alex", "stripe_keys", "acme-corp", "Quarterly revenue", "the-real-session-id")
SID = "the-real-session-id"


def planted_session(cli, tmp_path):
    uses = [tool_use("run_commands", {"commands": ["python3 -m pytest -q", f"curl -H 'Authorization: {SECRET}' https://{EMAIL}"]}, f"call-{UUID}"),
            tool_use("editor", {"path": f"{HOME}/src/billing/stripe_keys.py", "old_text": "Quarterly revenue", "new_text": f"token = '{SECRET}'"}, "call-edit"),
            tool_use("read_files", {"files": [{"path": "clients/acme-corp/contract.md"}], "by_name": {"clients/acme-corp/contract.md": 1, f"{EMAIL}": 2}}, "call-read"),
            tool_use("skills", {"skill": "hello"}, "call-skill")]
    msgs = [user_input(f"mail {EMAIL} about Quarterly revenue, key {SECRET}, folder {HOME}", 1, 0), assistant(1, 1, uses, text=f"I will use {SECRET} for acme-corp")]
    results = [tool_result(f"call-{UUID}", "run_commands", [ok_command(), failed_command()], i=2, t=2),
               tool_result("call-edit", "editor", text=f"edited {HOME}/src/billing/stripe_keys.py", i=3, t=2),
               tool_result("call-read", "read_files", text="Quarterly revenue is up", i=4, t=2), tool_result("call-skill", "skills", text="loaded", i=5, t=2)]
    msgs += results + [assistant(6, 5, text="done"), user_input("again", 7, 8), user_input("really", 8, 9), assistant(9, 12, text="ok")]
    folder = write_session(tmp_path / "sessions", SID, cli.repo, msgs)
    meta = json.loads((folder / f"{SID}.json").read_text(encoding="utf-8"))
    meta.update(prompt=f"secret {SECRET}", cwd=HOME, workspace_root=HOME)
    meta["metadata"]["title"] = f"acme-corp {EMAIL}"
    (folder / f"{SID}.json").write_text(json.dumps(meta))
    return folder


def replica(cli, tmp_path, folder, *flags):
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir(exist_ok=True)
    proc = cli.run(folder, "--host", "cline", "--make-replica", *flags)
    return proc, json.loads(proc.stdout)


def files_of(tmp_path):
    return sorted((tmp_path / "tmp" / "workflow-reviewer").glob("replica-cline-*/**/*.json"))


def key_numbers(out):
    t = out["tokens"]
    return {"turns": out["window"]["turns_total"], "interruptions": out["interruptions"], "tools": out["tools"]["by_category"],
            "errors": out["tools"]["errors"], "verify": out["verify"]["total"], "skills": out["skills"]["used"],
            "tokens": {k: t[k] for k in ("api_calls", "fresh_input", "cache_read", "cache_write", "output")},
            "wall": out["time"]["wall_s"], "models": out["actors"][0]["models"]}


def test_a_replica_gives_the_same_numbers_as_the_session(cli, tmp_path):
    folder = planted_session(cli, tmp_path)
    proc, out = replica(cli, tmp_path, folder)
    assert proc.returncode == 0 and out["leaked"] == 0 and out["files"] == 2
    new = next(iter(sorted((tmp_path / "tmp" / "workflow-reviewer").glob("replica-cline-*/replica_session"))))
    _, before = cli.json(folder, "--host", "cline")
    _, after = cli.json(new, "--host", "cline")
    assert key_numbers(before) == key_numbers(after)
    assert key_numbers(after)["errors"] == 1 and key_numbers(after)["interruptions"]["count"] == 1 and key_numbers(after)["verify"] == 1


def test_nothing_from_the_source_survives_in_any_position(cli, tmp_path):
    proc, out = replica(cli, tmp_path, planted_session(cli, tmp_path))
    assert proc.returncode == 0
    text = "".join(p.read_text(encoding="utf-8") for p in files_of(tmp_path))
    for planted in PLANTED:
        assert planted not in text and planted not in proc.stdout, planted
    assert "/Users/" not in text and "@" not in text and "person" not in text


def test_the_replica_carries_structure_and_numbers_but_regenerated_ids_and_shifted_times(cli, tmp_path):
    folder = planted_session(cli, tmp_path)
    replica(cli, tmp_path, folder)
    new = next(iter(sorted((tmp_path / "tmp" / "workflow-reviewer").glob("replica-cline-*/replica_session"))))
    old_doc = json.loads((folder / f"{SID}.messages.json").read_text(encoding="utf-8"))
    new_doc = json.loads((new / "replica_session.messages.json").read_text(encoding="utf-8"))
    assert [m["role"] for m in new_doc["messages"]] == [m["role"] for m in old_doc["messages"]]
    assert [m["metrics"] for m in new_doc["messages"] if "metrics" in m] == [m["metrics"] for m in old_doc["messages"] if "metrics" in m]
    assert {m["id"] for m in new_doc["messages"]}.isdisjoint({m["id"] for m in old_doc["messages"]})
    assert len({m["id"] for m in new_doc["messages"]}) == len(new_doc["messages"])
    old_ts, new_ts = [m["ts"] for m in old_doc["messages"]], [m["ts"] for m in new_doc["messages"]]
    assert all(isinstance(t, int) for t in new_ts) and [b - a for a, b in zip(new_ts, new_ts[1:])] == [b - a for a, b in zip(old_ts, old_ts[1:])]
    assert new_ts[0] != old_ts[0] and json.loads((new / "replica_session.json").read_text(encoding="utf-8"))["cwd"] == "~/project"


def test_the_exit_code_wrapper_and_the_skill_name_are_kept_because_the_mapping_declares_them(cli, tmp_path):
    folder = planted_session(cli, tmp_path)
    replica(cli, tmp_path, folder)
    text = "".join(p.read_text(encoding="utf-8") for p in files_of(tmp_path))
    assert "with code 2" in text and "<user_input" in text and '"hello"' in text and "python3 -m pytest -q" in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_replica_is_private(cli, tmp_path):
    replica(cli, tmp_path, planted_session(cli, tmp_path))
    top = next(iter(sorted((tmp_path / "tmp" / "workflow-reviewer").glob("replica-cline-*"))))
    assert stat.S_IMODE(top.stat().st_mode) == 0o700 and stat.S_IMODE((top / "replica_session").stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files_of(tmp_path))


def test_two_replicas_never_overwrite_each_other(cli, tmp_path):
    folder = planted_session(cli, tmp_path)
    replica(cli, tmp_path, folder)
    replica(cli, tmp_path, folder)
    assert len(list((tmp_path / "tmp" / "workflow-reviewer").glob("replica-cline-*"))) == 2


def test_a_failed_leak_check_writes_nothing(cli, tmp_path, monkeypatch, capsys):
    folder = planted_session(cli, tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(tm.tempfile, "tempdir", None)
    (tmp_path / "tmp").mkdir()
    monkeypatch.chdir(cli.repo)
    monkeypatch.setattr(tm.Anonymizer, "text", lambda self, s: s)                 # a broken anonymizer that keeps the text
    assert tm.main([str(folder), "--host", "cline", "--make-replica", "--format", "json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert "replica not written" in out["error"] and not (tmp_path / "tmp" / "workflow-reviewer").exists()
    assert SECRET not in json.dumps(out)


def test_a_database_host_cannot_be_replicated_yet(cli, tmp_path):
    db = dv.DevinDb(tmp_path / "sessions.db")
    chain = db.session("s1", str(cli.repo.resolve()))
    chain.add(dv.user("one"), dv.assistant("ok", tokens=dv.metrics(1, 1, 1)))
    chain.done()
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    proc = cli.run(tmp_path / "sessions.db", "--host", "devin", "--make-replica")
    assert proc.returncode == 4 and "cannot be made" in json.loads(proc.stdout)["error"]


def test_a_replica_is_only_written_when_asked(cli, tmp_path):
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir()
    cli.json(planted_session(cli, tmp_path), "--host", "cline")
    assert list((tmp_path / "tmp").iterdir()) == []


def test_make_replica_cannot_be_combined_with_report_or_events(cli, tmp_path):
    folder = planted_session(cli, tmp_path)
    assert cli.run(folder, "--host", "cline", "--make-replica", "--events", "0:1").returncode == 2


# ---- --pr-bundle ---------------------------------------------------------------------------------------------------------------------

def bundle(cli, tmp_path, folder):
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    cli.env_extra["XDG_CONFIG_HOME"] = cli.env_extra["APPDATA"] = str(cli.home / "xdg")
    proc = cli.run(folder, "--host", "cline", "--pr-bundle")
    return proc, json.loads(proc.stdout)


def test_the_bundle_gives_paths_expected_numbers_and_the_commands(cli, tmp_path):
    folder = planted_session(cli, tmp_path)
    replica(cli, tmp_path, folder)
    proc, out = bundle(cli, tmp_path, folder)
    assert proc.returncode == 0 and out["adapter_path"] == "~/xdg/workflow-reviewer/hosts/cline.toml"
    assert "replica-cline-" in out["replica_path"]
    expected = out["expected_for_golden_test"]
    assert expected["turns"] == 3 and expected["tool_errors"] == 1 and expected["verify_runs"] == 1 and expected["interruptions"] == 1
    assert expected["tokens"]["api_calls"] == 3 and expected["version"] == "4.1.26"
    commands = "\n".join(out["commands"])
    for needle in ("gh repo fork justin-nicky/workflow-reviewer-skill", "git checkout -b add-cline-adapter", "git add skills/workflow-reviewer/scripts/mappings/cline.toml",
                   "tests/fixtures/golden/cline", "git push -u origin add-cline-adapter", "gh pr create --repo justin-nicky/workflow-reviewer-skill"):
        assert needle in commands, needle
    assert "Nothing was run" in out["note"]
    assert 'cp "$HOME/xdg/workflow-reviewer/hosts/cline.toml"' in commands and '"~' not in commands                  # a ~ in quotes would not expand


def test_the_bundle_runs_nothing_and_writes_nothing(cli, tmp_path, monkeypatch, capsys):
    folder = planted_session(cli, tmp_path)
    called = []
    for target in ("run", "Popen", "call", "check_output"):
        monkeypatch.setattr(tm.subprocess, target, lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("ran a command")))
    monkeypatch.setattr(tm.os, "system", lambda *a: called.append(a))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cli.home / "xdg"))
    monkeypatch.setenv("APPDATA", str(cli.home / "xdg"))
    monkeypatch.setattr(tm.tempfile, "tempdir", None)
    monkeypatch.chdir(cli.repo)
    assert tm.main([str(folder), "--host", "cline", "--pr-bundle", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert called == [] and out["replica_path"].startswith("(none yet") and not (tmp_path / "tmp").exists() and not (cli.home / "xdg").exists()


def test_the_bundle_shows_no_username_path_or_session_id(cli, tmp_path):
    folder = planted_session(cli, tmp_path)
    replica(cli, tmp_path, folder)
    proc, _ = bundle(cli, tmp_path, folder)
    for token in ("alex/private", "/Users/alex", SID, SECRET, EMAIL):
        assert token not in proc.stdout
