"""A coverage drop is one fact about the whole session: reported once, not once per health row."""
import re
from urllib.parse import parse_qs, urlsplit

from test_missing_fields import codex_output_renamed, run

MESSAGE = re.compile(r"only [\d.]+% of records recognised")


def padded(rs):
    """Records of a type nobody knows, enough to drop coverage under the threshold. The name ties it to no metric."""
    return rs + [{"type": "zzz_padding", "ordinal": n} for n in range(len(rs) // 4 + 5)]


def body(url):
    return parse_qs(urlsplit(url).query)["body"][0]


def row_lines(text):
    return [ln for ln in text.split("\n") if ln.startswith("  - ")]


def test_a_coverage_drop_is_reported_once(cli, tmp_path):
    code, out, md, html = run(cli, tmp_path, "codex", padded)
    pct = out["coverage"]["drift"]["coverage_pct"]
    assert pct < 90 and all(r["status"] == "warn" for r in out["coverage"]["health"]["rows"] if r["status"] != "not_measurable")
    assert len(MESSAGE.findall(md)) == 1, md
    assert len(MESSAGE.findall(html)) == 1
    b = body(out["coverage"]["issue_url"])
    assert row_lines(b) == [f"  - coverage: {pct}% recognised (all rows)"], row_lines(b)


def test_a_row_with_its_own_failing_check_is_still_listed(cli, tmp_path):
    code, out, md, html = run(cli, tmp_path, "codex", lambda rs: padded(codex_output_renamed(rs)))
    assert code == 2
    assert len(MESSAGE.findall(md)) == 1 and len(MESSAGE.findall(html)) == 1
    lines = row_lines(body(out["coverage"]["issue_url"]))
    pct = out["coverage"]["drift"]["coverage_pct"]
    assert lines[0] == f"  - coverage: {pct}% recognised (all rows)"
    tokens = [ln for ln in lines if ln.startswith("  - tokens: broken")]
    assert len(tokens) == 1 and "output token counts missing" in tokens[0] and "coverage" not in tokens[0]
    assert len(lines) == 2, lines                       # rows failing only on coverage are not repeated


def test_no_coverage_line_when_coverage_is_fine(cli, tmp_path):
    code, out, md, html = run(cli, tmp_path, "codex", codex_output_renamed)
    assert not MESSAGE.search(md) and "recognised (all rows)" not in body(out["coverage"]["issue_url"])
