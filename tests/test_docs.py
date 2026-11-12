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


def test_skill_md_is_short():
    assert len(text(SKILL).splitlines()) <= 150


def test_the_ground_rules_and_the_seven_steps_are_there():
    body = text(SKILL)
    for phrase in ("read-only", "never edit", "data, never instructions", "secrets"):
        assert phrase in body.lower().replace("**", ""), phrase
    for n in range(1, 8):
        assert re.search(rf"^##+ Step {n}\b", body, re.M), f"Step {n}"


# ---- flags and commands --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_flag_a_doc_mentions_exists(doc):
    mentioned = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", text(doc)))
    assert mentioned <= SCRIPT_FLAGS, sorted(mentioned - SCRIPT_FLAGS)


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


def test_the_sample_report_is_the_rendered_golden_page():
    sample = ROOT / "examples" / "sample-report.html"
    golden = ROOT / "tests" / "fixtures" / "golden" / "claude-code-report.html"
    strip = lambda s: re.sub(r"reviewed on [^<]*<", "reviewed on DATE<", s)
    assert sample.is_file() and strip(text(sample)) == strip(text(golden))
    assert "http://" not in text(sample) and "https://" not in text(sample)


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
