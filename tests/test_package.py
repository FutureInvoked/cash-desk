"""Package hygiene: the engine stands alone, imports nothing from Hermes, prints
nothing, opens no network connection and stays Python 3.11 compatible."""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import cashdesk
from helpers import OLD_BRAND

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "cashdesk"
SOURCES = sorted(PACKAGE.rglob("*.py"))
FORBIDDEN_MODULES = {"hermes", "urllib", "socket", "http", "smtplib", "requests", "imaplib", "ftplib"}


def _imports(tree: ast.Module) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def test_sources_exist():
    assert SOURCES, "cashdesk/ has no Python sources"


def test_every_module_parses_as_python_311():
    for path in SOURCES:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))


def test_no_hermes_or_network_imports():
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, level in _imports(tree):
            top = name.split(".")[0]
            assert level > 0 or top not in FORBIDDEN_MODULES, f"{path.name} imports {name}"


def test_sibling_imports_are_relative():
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, level in _imports(tree):
            assert not (level == 0 and name.split(".")[0] == "cashdesk"), f"{path.name}: use a relative import, not {name}"


def test_no_print_calls():
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "print", f"{path.name}:{node.lineno} uses print()"


def test_no_float_money():
    """``float(`` never appears in the engine: money is Decimal end to end."""
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bfloat\(", text), f"{path.name} calls float()"


def test_no_forbidden_words():
    """The public repo carries no old brand, owner name or price."""
    for path in list(SOURCES) + sorted(PACKAGE.rglob("*.txt")) + sorted(PACKAGE.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert OLD_BRAND not in text, path
        assert not re.search(r"\b\d+\s?(?:%|per cent|percent)\b", text), f"{path.name} quotes a rate"


def test_public_names_resolve():
    for name in cashdesk.__all__:
        assert hasattr(cashdesk, name), name


SPEC_API = [
    "Customer", "Invoice", "Payment", "Match", "Draft", "Figure",
    "parse_amount", "format_money", "parse_date", "format_date",
    "read_invoices", "read_bank_csv", "read_customers", "ImportResult",
    "match_payments", "MatchResult", "plan_followups", "classify_reply", "ChaseState",
    "render_draft", "extract_figures", "check", "revise", "ledger_figures",
    "draft_hash", "idempotency_key", "verify_hash", "morning_brief", "Store", "ENGINE_VERSION",
]


def test_package_exports_the_spec_api():
    for name in SPEC_API:
        assert name in cashdesk.__all__ and hasattr(cashdesk, name), name
    assert not hasattr(cashdesk.money, "__all__") or callable(cashdesk.money.parse_amount), "cashdesk.money must still be the module"


def test_readme_documents_every_public_name():
    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
    for name in cashdesk.__all__:
        assert f"`{name}" in readme or f"{name}(" in readme, f"README does not document {name}"
    for required in ("Figure Lock", "standard library", "Decimal", "python -m pytest -q"):
        assert required in readme


def test_every_module_imports():
    for path in SOURCES:
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        importlib.import_module(".".join(parts))
