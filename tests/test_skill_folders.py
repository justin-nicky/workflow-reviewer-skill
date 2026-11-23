"""Skill folders are declared by each host's mapping; a host that declares none gets no inventory, never another host's folders."""
from fixtures.builders import write_jsonl

BARE = '''
host = "hostx"
schema_version = 1
[capabilities]
has_tokens = false
has_timestamps = false
has_tool_results = false
has_ids = false
[[records]]
match = { kind = "user" }
role = "user"
text = ["text"]
'''

DECLARED = BARE + '''
[[skill_folders]]
path = ".hostx/skills"
scope = "project"
[[skill_folders]]
path = "~/.hostx/skills"
scope = "user"
[[skill_folders]]
path = "skills"
base = "~/.hostx-config"
base_env = "HOSTX_HOME"
scope = "user"
[[skill_folders]]
path = ".only-here/skills"
scope = "project"
walk = false
'''


def skill(root, name, body="---\ndescription: a placeholder\n---\nsteps\n"):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(body)


def session(cli, tmp_path):
    return write_jsonl(tmp_path / "s.jsonl", [{"kind": "user", "text": "hello"}])


def test_a_host_without_skill_folders_gets_no_inventory_and_a_note(cli, tmp_path):
    skill(cli.repo / ".kiro" / "skills", "from-kiro")             # another host's folders must never be searched
    skill(cli.home / ".kiro" / "skills", "from-kiro-home")
    skill(cli.repo / ".claude" / "skills", "from-claude")
    code, out = cli.json(session(cli, tmp_path), "--mapping", "-", stdin=BARE)
    assert code == 0
    assert out["skills"]["inventory"] == [] and out["skills"]["inventory_total"] is None
    assert out["skills"]["inventory_note"] == "skill folders unknown for this host"
    assert "skill folders unknown for this host" in out["coverage"]["notes"]


def test_declared_folders_are_searched_with_their_scopes(cli, tmp_path):
    skill(cli.repo / ".hostx" / "skills", "proj")
    skill(cli.home / ".hostx" / "skills", "usr")
    skill(cli.home / ".hostx-config" / "skills", "cfg")
    skill(cli.repo.parent / ".hostx" / "skills", "parent")          # project folders are also searched in the repo's parents
    skill(cli.repo.parent / ".only-here" / "skills", "not-walked")  # walk = false: the repo itself only
    skill(cli.repo / ".only-here" / "skills", "here")
    code, out = cli.json(session(cli, tmp_path), "--mapping", "-", stdin=DECLARED)
    got = {e["name"]: e["scopes"] for e in out["skills"]["inventory"]}
    assert got == {"proj": ["project"], "usr": ["user"], "cfg": ["user"], "parent": ["project"], "here": ["project"]}
    assert "inventory_note" not in out["skills"]


def test_an_env_var_replaces_the_declared_base(cli, tmp_path):
    skill(cli.home / ".hostx-config" / "skills", "default-base")
    other = tmp_path / "elsewhere"
    skill(other / "skills", "from-env")
    cli.env_extra["HOSTX_HOME"] = str(other)
    _, out = cli.json(session(cli, tmp_path), "--mapping", "-", stdin=DECLARED)
    assert [e["name"] for e in out["skills"]["inventory"]] == ["from-env"]


def test_every_builtin_mapping_with_a_transcript_declares_its_folders():
    from conftest import ROOT
    import tomllib
    for name in ("claude_code", "codex", "cursor", "kiro"):
        raw = tomllib.loads((ROOT / "skills/workflow-reviewer/scripts/mappings" / f"{name}.toml").read_text(encoding="utf-8"))
        assert raw.get("skill_folders"), name


def test_kiro_degraded_report_lists_its_own_skills_from_its_mapping(cli):
    skill(cli.repo / ".kiro" / "skills", "kiro-skill")
    _, out = cli.json("--host", "kiro")
    assert [e["name"] for e in out["skills"]["inventory"]] == ["kiro-skill"]
