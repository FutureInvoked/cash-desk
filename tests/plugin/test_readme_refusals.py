"""The refusal table (docs/refusals.md, linked from the README) cannot drift from the code.

Every row names a message fragment and a test. The fragment must appear in the plugin's
source (so the README quotes what Cash Desk really says) and inside that test's own body (so
the test really checks that refusal), and the test must exist.
"""

from __future__ import annotations

import ast
import re

from tests.plugin.support import ROOT

START, END = "<!-- refusals:start -->", "<!-- refusals:end -->"
SOURCES = [ROOT / "__init__.py", ROOT / "dashboard" / "plugin_api.py", *sorted((ROOT / "cashdesk").glob("*.py"))]
ROW = re.compile(r"^\|\s*(?P<situation>[^|]+?)\s*\|\s*`(?P<fragment>[^`]+)`\s*\|\s*`(?P<test>test_[a-z0-9_]+)`\s*\|$")
REQUIRED = ("scheduled", "subagent", "approvals", "approved", "duplicate", "already sent", "from address")


def table_rows() -> list[dict[str, str]]:
    text = (ROOT / "docs" / "refusals.md").read_text(encoding="utf-8")
    assert START in text and END in text, "docs/refusals.md has no refusal table between the markers"
    block = text.split(START, 1)[1].split(END, 1)[0]
    lines = [line.strip() for line in block.splitlines() if line.strip().startswith("|")]
    rows = [m.groupdict() for m in map(ROW.match, lines[2:])]  # skip header and separator
    assert all(rows), f"a table row does not have the shape | situation | `fragment` | `test_name` |: {lines}"
    return rows


def collect_test_functions() -> dict[str, str]:
    found = {}
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                found[node.name] = ast.get_source_segment(source, node) or ""
    return found


def test_every_refusal_row_is_said_by_the_code_and_checked_by_its_test():
    rows = table_rows()
    assert len(rows) >= 10
    code = "\n".join(p.read_text(encoding="utf-8") for p in SOURCES)
    tests = collect_test_functions()
    for row in rows:
        fragment, name = row["fragment"], row["test"]
        assert fragment in code, f"README quotes {fragment!r} but no plugin source says it"
        assert name in tests, f"README names {name}, which does not exist"
        assert fragment in tests[name], f"{name} does not check {fragment!r}"
    situations = " ".join(r["situation"].lower() for r in rows)
    for topic in REQUIRED:
        assert topic in situations, f"the refusal table has no row about {topic!r}"
