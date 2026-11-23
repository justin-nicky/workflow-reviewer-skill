"""SKILL.md, the references and the README must match what the script really does."""
import importlib.util
import json
import re
import shlex
import subprocess
import sys

import pytest

from conftest import ROOT, SCRIPT

SKILL_DIR = ROOT / "skills" / "workflow-reviewer"
SKILL = SKILL_DIR / "SKILL.md"
DOCS = [SKILL, ROOT / "README.md", *sorted((SKILL_DIR / "references").glob("*.md"))]
GOLDEN_CLAUDE = next(iter(sorted((ROOT / "tests/fixtures/golden/claude-code").glob("*.jsonl")))).with_suffix("")

spec = importlib.util.spec_from_file_location("tm_docs", SCRIPT)
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)
HELP = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True).stdout
SCRIPT_FLAGS = set(re.findall(r"--[a-z][a-z0-9-]*", HELP))


def text(path):
    return path.read_text(encoding="utf-8")


def fenced_blocks(doc, languages=None):
    return [(lang, body) for lang, body in re.findall(r"```([a-z]*)\n(.*?)```", text(doc), re.S) if languages is None or lang in languages]


# ---- SKILL.md itself ---------------------------------------------------------------------------------------------------------------

def test_the_frontmatter_parses_and_the_skill_is_explicit_only():
    match = re.match(r"^---\n(.*?)\n---\n", text(SKILL), re.S)
    assert match, "SKILL.md must start with frontmatter"
    fields = dict(line.split(":", 1) for line in match.group(1).split("\n") if ":" in line)
    fields = {k.strip(): v.strip() for k, v in fields.items()}
    assert fields["name"] == "workflow-reviewer" and len(fields["description"]) > 40
    assert fields["disable-model-invocation"] == "true"
    assert "same session" in fields["description"] and "just" in fields["description"]
    assert "allow_implicit_invocation: false" in text(SKILL_DIR / "agents" / "openai.yaml")


def test_the_skill_version_matches_the_newest_changelog_entry():
    version = re.search(r'^  version: "([^"]+)"$', text(SKILL), re.M).group(1)
    assert re.search(r"^## v([\d.]+)", text(ROOT / "CHANGELOG.md"), re.M).group(1) == version


def test_the_docs_state_the_same_minimum_python_as_the_script():
    gate = re.search(r"sys\.version_info < \(3, (\d+)\)", text(SCRIPT)).group(1)
    assert gate == "9"
    assert f"Python 3.{gate} or newer" in text(ROOT / "README.md")
    assert f"older than 3.{gate}" in text(SKILL) and f"older than 3.{gate}" in text(SKILL_DIR / "references" / "hosts.md")


def test_skill_md_is_short():
    assert len(text(SKILL).splitlines()) <= 150


def test_the_ground_rules_and_the_seven_steps_are_there():
    body = text(SKILL)
    for phrase in ("read-only", "never edit", "data, never instructions", "secrets"):
        assert phrase in body.lower().replace("**", ""), phrase
    for n in range(1, 8):
        assert re.search(rf"^##+ Step {n}\b", body, re.M), f"Step {n}"


# ---- flags and commands --------------------------------------------------------------------------------------------------------------

SKILLS_CLI_FLAGS = {"--copy"}        # flags of `npx skills add` named in the README's install section


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_flag_a_doc_mentions_exists(doc):
    mentioned = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", text(doc)))
    assert mentioned - SKILLS_CLI_FLAGS <= SCRIPT_FLAGS, sorted(mentioned - SKILLS_CLI_FLAGS - SCRIPT_FLAGS)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_documented_command_parses_with_the_scripts_own_parser(doc):
    parser = tm.build_parser()
    seen = 0
    for _, body in fenced_blocks(doc, {"bash", "sh", ""}):
        for line in body.split("\n"):
            if "transcript_metrics.py" not in line:
                continue
            command = re.split(r"\s[<|>]\s|\s2>", line)[0]
            command = re.sub(r"<[NM]>", "1", command)                    # numbers in placeholders
            tokens = shlex.split(command.replace("<", "x_").replace(">", "_x"))
            args = tokens[next(i for i, t in enumerate(tokens) if t.endswith("transcript_metrics.py")) + 1:]
            try:
                parser.parse_args(args)
            except SystemExit:
                pytest.fail(f"{doc.name}: the parser rejects: {line.strip()}")
            seen += 1
    if doc == SKILL:
        assert seen >= 3


def test_the_scripts_commands_use_the_scripts_real_path():
    assert (SKILL_DIR / "scripts" / "transcript_metrics.py").is_file()
    assert re.search(r"python3 -I [^\n]*?scripts/transcript_metrics\.py", text(SKILL))


# ---- exit codes ------------------------------------------------------------------------------------------------------------------------

def script_exit_codes():
    """Every code the script can end with: 0, the literals and EXIT_ constants returned by main, and sys.exit calls."""
    source = SCRIPT.read_text(encoding="utf-8")
    main = source[source.index("def main(argv=None):"):]
    main = main[:main.index('\nif __name__ == "__main__":')]
    constants = {name: int(value) for name, value in re.findall(r"^(EXIT_[A-Z_]+) = (\d+)", source, re.M)}
    codes = {0} | {int(n) for n in re.findall(r"\breturn (\d+)\b", main)}
    codes |= {constants[n] for n in re.findall(r"\breturn (EXIT_[A-Z_]+)\b", main)}
    codes |= {constants[n] for n in re.findall(r"sys\.exit\((EXIT_[A-Z_]+)\)", source)}
    return codes


def test_step_3_handles_every_exit_code_the_script_can_return():
    codes = script_exit_codes()
    assert {0, 2, 3, 4, 5, 6, 7} <= codes
    step3 = re.search(r"^##+ Step 3.*?(?=^##+ Step 4)", text(SKILL), re.S | re.M).group(0)
    handled = {int(n) for n in re.findall(r"^- `(\d)`", step3, re.M)}
    assert codes <= handled, sorted(codes - handled)
    for pointer in ("references/adapter-flow.md",):
        assert pointer in step3


# ---- files -----------------------------------------------------------------------------------------------------------------------------

def resolve(doc, target):
    target = target.split("#")[0]
    for base in (doc.parent, SKILL_DIR, ROOT):
        if (base / target).exists():
            return True
    return False


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_linked_or_named_file_exists(doc):
    body = text(doc)
    links = [t for t in re.findall(r"\]\(([^)\s]+)\)", body) if not t.startswith(("http", "#", "mailto"))]
    named = re.findall(r"`((?:references|assets|scripts|examples|tests|docs)/[A-Za-z0-9_./\-]+)`", body)
    missing = [t for t in links + named if not t.endswith("/") and "*" not in t and not resolve(doc, t)]
    assert not missing, missing


def test_the_old_markdown_report_files_are_gone():
    assert not (SKILL_DIR / "assets" / "report-template.md").exists() and not (ROOT / "examples" / "sample-report.md").exists()
    for doc in DOCS:
        assert "report-template" not in text(doc) and "sample-report.md" not in text(doc), doc.name


SAMPLE_SKILLS = ["hello", "code-review", "pr-review", "tdd"] + [f"skill-{n:02d}" for n in range(1, 9)]


def render_sample(cli, tmp_path):
    """The sample report as the script renders it: the Claude Code golden replica plus the sample findings."""
    for name in SAMPLE_SKILLS:
        folder = cli.home / ".claude" / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\ndescription: d\n---\nbody\n", encoding="utf-8")
    session = next(iter(sorted((ROOT / "tests" / "fixtures" / "golden" / "claude-code").glob("*.jsonl")))).with_suffix("")
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir()
    findings = (ROOT / "tests" / "fixtures" / "findings" / "sample.json").read_text(encoding="utf-8")
    proc = cli.run(session, "--host", "claude-code", "--report", "-", "--no-open", stdin=findings)
    assert proc.returncode == 0, proc.stderr
    (path,) = (tmp_path / "tmp" / "workflow-reviewer").glob("review-*.html")
    return path.read_text(encoding="utf-8")


def test_the_sample_report_is_the_render_of_the_golden_session_and_the_sample_findings(cli, tmp_path):
    sample = ROOT / "examples" / "sample-report.html"
    strip = lambda s: re.sub(r"reviewed on [^<]*<", "reviewed on DATE<", s)
    assert sample.is_file() and strip(text(sample)) == strip(render_sample(cli, tmp_path))
    assert "http://" not in text(sample) and "https://" not in text(sample)
    assert "this session only reads" not in text(sample)


# ---- the examples in the docs ------------------------------------------------------------------------------------------------------------

def findings_examples():
    found = []
    for doc in DOCS:
        for _, body in fenced_blocks(doc, {"json"}):
            if '"schema_version"' in body:
                found.append(pytest.param(body, id=f"{doc.name}:{len(found)}"))
    return found


def test_the_docs_contain_at_least_one_findings_example():
    assert len(findings_examples()) >= 1


@pytest.mark.parametrize("body", findings_examples())
def test_every_findings_example_in_the_docs_is_valid(cli, tmp_path, body):
    json.loads(body)
    cli.env_extra["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir()
    proc = cli.run(GOLDEN_CLAUDE, "--host", "claude-code", "--report", "-", "--no-open", stdin=body)
    assert proc.returncode == 0, proc.stdout


def test_the_step_5_example_covers_the_whole_schema():
    example = json.loads(next(p.values[0] for p in findings_examples() if '"closing_question"' in p.values[0]))
    assert tm.validate_findings(example) == []
    assert example["actions"] and example["skills"] and example["closing_question"] and example["metric_notes"]


# ---- the findings are written for the user, not in the script's field names -----------------------------------------------------------------

GOLDEN_CURSOR = next(iter(sorted((ROOT / "tests/fixtures/golden/cursor-chat1").glob("*/"))))
SECTIONS = r"(?:tokens|tools|verify|time|turns|actors|skills|coverage|window|interruptions)"


def metric_field_names(cli):
    """Every key the metrics output uses that reads as a field name: snake_case ones, `judge`, and dotted paths."""
    names = {"judge"}

    def walk(value):
        if isinstance(value, dict):
            for key, inner in value.items():
                if "_" in key and re.fullmatch(r"[a-z][a-z0-9_]*", key):
                    names.add(key)
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)
    for session, host in ((GOLDEN_CLAUDE, "claude-code"), (GOLDEN_CURSOR, "cursor")):
        walk(cli.json(session, "--host", host, "--health-all")[1])
    return names


def prose(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for inner in value:
            yield from prose(inner)
    elif isinstance(value, dict):
        for key, inner in value.items():
            if key != "metric_notes":                       # the keys of metric_notes are row ids, but its values are prose
                yield from prose(inner)
            else:
                yield from prose(list(inner.values()))


@pytest.mark.parametrize("body", findings_examples())
def test_a_findings_example_never_uses_a_field_name_from_the_metrics_in_its_prose(cli, body):
    names = metric_field_names(cli)
    assert {"loaded_total", "directive_pairs", "has_tokens", "repeated_reads"} <= names
    for sentence in prose(json.loads(body)):
        for name in names:
            assert not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", sentence), (name, sentence)
        assert not re.search(rf"\b{SECTIONS}\.[a-z_]+", sentence), sentence


def test_skill_md_tells_the_agent_to_write_for_the_user():
    skill = text(ROOT / "skills/workflow-reviewer/SKILL.md").lower()
    for needle in ("field name", "one problem per action", "never evidence", "actors"):
        assert needle in skill, needle
