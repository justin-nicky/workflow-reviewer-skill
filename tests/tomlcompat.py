"""tomllib for the tests: the standard library one, or the vendored tomli on Python older than 3.11 (as the script does)."""
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "skills" / "workflow-reviewer" / "scripts" / "_vendor"

try:
    import tomllib
except ImportError:                      # Python 3.9 and 3.10
    sys.path.insert(0, str(VENDOR))
    import tomli as tomllib
