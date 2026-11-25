"""User adapters: new hosts and extra variants for built-in hosts, labelled, never replacing built-ins; --save-adapter."""
import json
import os
import stat

import pytest
from fixtures.builders import cc_assistant, cc_user, usage, write_jsonl
from test_health import MAPPING, N, assistant, marker, result, session, user

from conftest import ROOT


def adapter_dir(cli):
    folder = cli.home / "xdg" / "workflow-reviewer" / "hosts"
    cli.env_extra["XDG_CONFIG_HOME"] = cli.env_extra["APPDATA"] = str(cli.home / "xdg")
    return folder


def install(cli, name, text):
    folder = adapter_dir(cli)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.toml").write_text(text, encoding="utf-8")


def hx(extra=""):
    return MAPPING + extra


def claude_golden():
    base = ROOT / "tests" / "fixtures" / "golden" / "claude-code"
    return next(iter(sorted(base.glob("*.jsonl")))).with_suffix("")


# ---- loading -----------------------------------------------------------------------------------------------------------------

def test_a_user_adapter_adds_a_new_host_and_is_labelled_unproven(cli, tmp_path):
    install(cli, "hx", hx())
    path = write_jsonl(tmp_path / "s.jsonl", session())
    code, out = cli.json(path, "--host", "hx")
    assert code == 0 and out["coverage"]["host"] == "hx" and out["coverage"]["label"] == "unproven"
    assert out["coverage"]["host_chosen_by"] == "--host"


def test_built_in_hosts_are_labelled_verified(cli):
    code, out = cli.json(claude_golden(), "--host", "claude-code")
    assert code == 0 and out["coverage"]["label"] == "verified" and out["coverage"]["host_chosen_by"] == "--host"


def test_an_override_mapping_is_unproven(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", session())
    (tmp_path / "m.toml").write_text(MAPPING)
    _, out = cli.json(path, "--mapping", tmp_path / "m.toml")
    assert out["coverage"]["label"] == "unproven" and out["coverage"]["host_chosen_by"] == "--mapping"


CLAUDE_VARIANT = '''
host = "claude-code"
schema_version = 1
variant = "claude-2099"
tested_versions = "2099.1.0"
type_path = "kind"
[capabilities]
has_tokens = false
has_timestamps = false
has_tool_results = false
has_ids = false
[[records]]
match = { kind = "prompt" }
role = "user"
text = ["text"]
[[records]]
match = { kind = "reply" }
role = "assistant"
text = ["text"]
'''


def test_a_user_variant_for_a_built_in_host_is_chosen_by_shape(cli, tmp_path):
    install(cli, "claude-2099", CLAUDE_VARIANT)
    shaped = write_jsonl(tmp_path / "s.jsonl", [{"kind": "prompt", "text": "one"}, {"kind": "reply", "text": "ok"}] * 6)
    code, out = cli.json(shaped, "--host", "claude-code")
    assert out["coverage"]["variant"] == "claude-2099" and out["coverage"]["label"] == "unproven"
    assert out["window"]["turns_total"] == 6


def test_the_built_in_variant_still_wins_on_a_built_in_shape(cli):
    install(cli, "claude-2099", CLAUDE_VARIANT)
    code, out = cli.json(claude_golden(), "--host", "claude-code")
    assert code == 0 and out["coverage"]["variant"] == "default" and out["coverage"]["label"] == "verified"
    assert out["coverage"]["variant_choice"]["scores"]["claude-2099"] < out["coverage"]["variant_choice"]["scores"]["default"]


def test_a_user_adapter_never_replaces_a_built_in_variant_on_a_tie(cli, tmp_path):
    same = (ROOT / "skills/workflow-reviewer/scripts/mappings/codex.toml").read_text(encoding="utf-8")
    install(cli, "codex-copy", same.replace('variant = "desktop"', 'variant = "mine"') if False else same)
    desktop = ROOT / "tests" / "fixtures" / "golden" / "codex-desktop"
    files = sorted(desktop.glob("rollout-*.jsonl"))
    code, out = cli.json(*files, "--host", "codex")
    assert out["coverage"]["variant"] == "desktop" and out["coverage"]["label"] == "verified"


def test_a_broken_user_adapter_is_skipped_with_a_note(cli, tmp_path):
    install(cli, "bad", "this is = not [valid toml")
    install(cli, "newer", 'host = "zz"\nschema_version = 99\n')
    code, out = cli.json(claude_golden(), "--host", "claude-code")
    notes = out["coverage"]["notes"]
    assert code == 0 and any("bad.toml" in n and "ignored" in n for n in notes) and any("newer.toml" in n for n in notes)


def test_the_adapter_folder_follows_xdg_and_appdata():
    import importlib.util
    from conftest import SCRIPT
    spec = importlib.util.spec_from_file_location("tm_adapters", SCRIPT)
    tm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tm)
    assert tm.user_adapter_dir({"XDG_CONFIG_HOME": "/x/cfg"}, "linux", "/home/u").as_posix() == "/x/cfg/workflow-reviewer/hosts"
    assert tm.user_adapter_dir({}, "linux", "/home/u").as_posix() == "/home/u/.config/workflow-reviewer/hosts"
    assert str(tm.user_adapter_dir({"APPDATA": "C:/Users/u/AppData/Roaming"}, "win32", "C:/Users/u")).replace("\\", "/") == \
        "C:/Users/u/AppData/Roaming/workflow-reviewer/hosts"


# ---- --save-adapter ----------------------------------------------------------------------------------------------------------

def save(cli, tmp_path, text, *args, name="hx", records=None):
    cli.env_extra["XDG_CONFIG_HOME"] = cli.env_extra["APPDATA"] = str(cli.home / "xdg")
    path = write_jsonl(tmp_path / "s.jsonl", records if records is not None else session())
    return cli.run(path, "--save-adapter", name, *args, stdin=text)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_save_adapter_writes_a_private_file_when_no_core_metric_is_broken(cli, tmp_path):
    proc = save(cli, tmp_path, MAPPING)
    out = json.loads(proc.stdout)
    target = cli.home / "xdg" / "workflow-reviewer" / "hosts" / "hx.toml"
    assert proc.returncode == 0 and out["saved"] == "~" + str(target)[len(str(cli.home)):] and out["label"] == "unproven"        # printed home-relative
    assert stat.S_IMODE(target.stat().st_mode) == 0o600 and stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    text = target.read_text(encoding="utf-8")
    assert 'made_by = "workflow-reviewer adapter flow"' in text and 'made_from_version = "1.5.0"' in text
    from tomlcompat import tomllib
    assert tomllib.loads(text)["host"] == "hx"
    # and the saved adapter now works as a host
    _, again = cli.json(write_jsonl(tmp_path / "t.jsonl", session()), "--host", "hx")
    assert again["coverage"]["host"] == "hx" and again["coverage"]["label"] == "unproven"


def test_save_adapter_refuses_when_a_core_metric_is_broken_and_writes_nothing(cli, tmp_path):
    proc = save(cli, tmp_path, MAPPING, records=[user(), assistant("m", 3, ["c1", "c2", "c3"])])         # calls, no results
    out = json.loads(proc.stdout)
    assert proc.returncode == 2 and "broken" in out["error"] and out["broken"] == ["tool_calls"]
    assert not (cli.home / "xdg").exists()


def test_save_adapter_refuses_to_overwrite_without_replace(cli, tmp_path):
    assert save(cli, tmp_path, MAPPING).returncode == 0
    proc = save(cli, tmp_path, MAPPING)
    assert proc.returncode == 2 and "exists" in json.loads(proc.stdout)["error"]
    assert save(cli, tmp_path, MAPPING, "--replace").returncode == 0


@pytest.mark.parametrize("bad, reason", [
    (MAPPING.replace("schema_version = 1", "schema_version = 7"), "newer"),
    (MAPPING.replace('type_path = "t"', 'type_path = "t"\nrun = "rm -rf /"'), "unknown key 'run'"),
    (MAPPING.replace('role = "user"', 'role = "user"\nexec = "x"', 1), "unknown key 'exec'"),
    (MAPPING.replace('[fields]', '[fields]\nbogus = "x"'), "unknown key 'bogus'"),
    (MAPPING + '\n[[checks]]\nname = "x"\nmetric = "turns"\nfirst = "turns"\nsecond = "turns"\nevil = 1\n', "unknown key 'evil'"),
    (MAPPING.replace("[capabilities]", '[container]\nkind = "carrier-pigeon"\n[capabilities]'), "not supported"),
    ('host = "hx"\nschema_version = 1\n[[records]]\nmatch = { t = "u" }\nrole = "user"\ncall_command_regex = "("\n', "regular expression"),
    (MAPPING.replace('host = "hx"', 'host = "../escape"'), "host name"),
])
def test_save_adapter_validates_the_mapping_first(cli, tmp_path, bad, reason):
    proc = save(cli, tmp_path, bad)
    out = json.loads(proc.stdout)
    assert proc.returncode == 2 and reason in out["error"] + " ".join(out.get("errors", [])), out
    assert not (cli.home / "xdg").exists()


@pytest.mark.parametrize("name", ["../x", "a/b", "", "UPPER", "x" * 60, ".hidden"])
def test_save_adapter_checks_the_name(cli, tmp_path, name):
    proc = save(cli, tmp_path, MAPPING, name=name)
    assert proc.returncode == 2 and not (cli.home / "xdg").exists()


def test_save_adapter_cannot_share_stdin_with_report_or_mapping(cli, tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", session())
    assert cli.run(path, "--save-adapter", "hx", "--mapping", "-", stdin=MAPPING).returncode == 2
    assert cli.run(path, "--save-adapter", "hx", "--report", "-", stdin=MAPPING).returncode == 2


# ---- an adapter may add mappings, never remove or loosen checks -----------------------------------------------------------------

from conftest import ROOT  # noqa: E402
from fixtures.builders import cc_assistant, cc_bash_call, cc_user, usage  # noqa: E402

BUILTIN_CODEX = (ROOT / "skills" / "workflow-reviewer" / "scripts" / "mappings" / "codex.toml").read_text(encoding="utf-8")
BUILTIN_CLAUDE = (ROOT / "skills" / "workflow-reviewer" / "scripts" / "mappings" / "claude_code.toml").read_text(encoding="utf-8")
TOKEN_CHECK_NAME = 'name = "tokens: summed per-response usage vs the final token_count total (output)"'


def claude_records():
    return [cc_user("go", 0), *cc_bash_call("m1", "c1", "ls", 2), cc_assistant("m2", 4, usage=usage(5, 20))]


def without_check(text, name_line):
    start = text.index(name_line)
    block = text.rindex("[[checks]]", 0, start)
    end = text.find("\n[[", start)
    return text[:block] + text[end + 1:]


def test_a_variant_that_drops_a_built_in_check_is_refused(cli, tmp_path):
    proc = save(cli, tmp_path, without_check(BUILTIN_CODEX, TOKEN_CHECK_NAME), name="codex-mine")
    out = json.loads(proc.stdout)
    assert proc.returncode == 2 and out["error"] == "an adapter may add mappings, not remove or loosen checks"
    assert any("tokens: summed per-response usage" in p for p in out["problems"])
    assert not (cli.home / "xdg").exists()


def test_a_variant_with_a_looser_tolerance_is_refused(cli, tmp_path):
    looser = BUILTIN_CODEX.replace("tolerance_pct = 2\n", "tolerance_pct = 50\n", 1)
    assert looser != BUILTIN_CODEX
    proc = save(cli, tmp_path, looser, name="codex-mine")
    out = json.loads(proc.stdout)
    assert proc.returncode == 2 and any("tolerance" in p for p in out["problems"])


def test_a_variant_that_skips_a_check_or_lowers_its_severity_is_refused(cli, tmp_path):
    skipped = BUILTIN_CLAUDE.replace('relation = "at_least"\n', 'relation = "at_least"\nskip_if_second_zero = true\n', 1)
    assert save(cli, tmp_path / "a", skipped, name="cc-a", records=claude_records()).returncode == 2
    weaker = BUILTIN_CLAUDE.replace('second = { const = 95 }\nrelation = "at_least"\n', 'second = { const = 95 }\nrelation = "at_least"\nseverity = "warn"\n', 1)
    assert weaker != BUILTIN_CLAUDE
    assert save(cli, tmp_path / "b", weaker, name="cc-b", records=claude_records()).returncode == 2


def test_a_variant_that_only_adds_record_mappings_is_allowed(cli, tmp_path):
    added = BUILTIN_CLAUDE + '\n[[records]]\nmatch = { type = "brand_new_record" }\nrole = "ignore"\n'
    proc = save(cli, tmp_path, added, name="cc-more", records=claude_records())
    assert proc.returncode == 0, proc.stdout
    assert (cli.home / "xdg" / "workflow-reviewer" / "hosts" / "cc-more.toml").is_file()
