"""The vendored tomli (the parser behind Python 3.11's tomllib) is unchanged and behaves like tomllib."""
import ast
import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

from conftest import ROOT, SCRIPT
from tomlcompat import VENDOR

PACKAGE = VENDOR / "tomli"
SUMS = ROOT / "tests" / "fixtures" / "vendored_tomli.sha256"


def listed():
    rows = [line.split(None, 1) for line in SUMS.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {name.strip(): digest for digest, name in rows}


def on_disk():
    return {p.relative_to(VENDOR).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(VENDOR.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def test_the_vendored_files_match_the_recorded_hashes():
    assert on_disk() == listed(), "a vendored file changed, or the list of hashes is out of date"


def test_the_vendored_package_carries_its_licence_and_a_note_on_its_source():
    assert (PACKAGE / "LICENSE").is_file() and "MIT" in (PACKAGE / "LICENSE").read_text(encoding="utf-8")
    note = (VENDOR / "README.md").read_text(encoding="utf-8")
    assert "tomli" in note and "2.5.0" in note and "pypi.org/project/tomli" in note and "SHA-256" in note


def test_the_script_falls_back_to_the_vendored_copy_when_tomllib_is_missing():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "import tomllib" in source and "_vendor" in source and "import tomli as tomllib" in source


# ---- differential: tomllib (3.11+) and the vendored tomli agree ---------------------------------------------------------------------

@pytest.fixture
def both():
    tomllib = pytest.importorskip("tomllib")
    sys.path.insert(0, str(VENDOR))
    for name in [n for n in sys.modules if n == "tomli" or n.startswith("tomli.")]:
        del sys.modules[name]
    try:
        vendored = importlib.import_module("tomli")
        assert Path(vendored.__file__).resolve().is_relative_to(VENDOR.resolve())
        yield tomllib, vendored
    finally:
        sys.path.remove(str(VENDOR))
        for name in [n for n in sys.modules if n == "tomli" or n.startswith("tomli.")]:
            del sys.modules[name]


def outcome(module, text):
    try:
        return "ok", json.dumps(module.loads(text), sort_keys=True, default=repr)
    except Exception as err:                                  # the error type and message must agree too
        return "error", f"{type(err).__name__}: {err}"


def test_every_builtin_mapping_parses_the_same(both):
    tomllib, vendored = both
    files = sorted((ROOT / "skills" / "workflow-reviewer" / "scripts" / "mappings").glob("*.toml"))
    assert len(files) >= 6
    for file in files:
        text = file.read_text(encoding="utf-8")
        assert outcome(tomllib, text) == outcome(vendored, text), file.name
        assert outcome(tomllib, text)[0] == "ok", file.name


def test_every_string_in_the_tests_parses_the_same(both):
    tomllib, vendored = both
    texts = set()
    for test_file in sorted((ROOT / "tests").rglob("*.py")):
        for node in ast.walk(ast.parse(test_file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and ("=" in node.value or "[" in node.value):
                texts.add(node.value)
    assert len(texts) > 100
    parsed = 0
    for text in sorted(texts):
        a, b = outcome(tomllib, text), outcome(vendored, text)
        assert a == b, text[:200]
        parsed += a[0] == "ok"
    assert parsed > 20                                          # a good share of them really are TOML
