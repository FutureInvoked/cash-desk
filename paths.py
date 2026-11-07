"""Where Cash Desk keeps its state. Shared by the Hermes half and the Desktop backend.

Everything lives under Hermes's per-profile plugin data folder,
``<hermes home>/plugin-data/cash-desk/``, so two profiles never share a ledger.
"""

from __future__ import annotations

from pathlib import Path

PLUGIN_NAME = "cash-desk"
DB_FILENAME = "cashdesk.sqlite"


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir  # Hermes-provided, follows the active profile

    return Path(plugin_data_dir(PLUGIN_NAME))


def store_path() -> Path:
    return data_dir() / DB_FILENAME


DEMO_DIR = Path(__file__).resolve().parent / "demo"


def import_roots(configured: str = "") -> list[Path]:
    """Folders cashdesk_import may read: the plugin's data folder, its bundled demo/ folder and
    the comma-separated ``import_dirs`` setting. Every entry is resolved (symlinks, ``..``)."""
    roots = [data_dir().resolve(), DEMO_DIR]
    for part in (configured or "").split(","):
        text = part.strip()
        if text:
            roots.append(Path(text).expanduser().resolve())
    return roots


def allowed_import_path(path: Path, configured: str = "") -> Path | None:
    """The resolved file when it lies inside one of :func:`import_roots`, else None.
    A relative path is taken from the plugin folder, so ``demo/customers.csv`` names the bundled demo."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = DEMO_DIR.parent / candidate
    resolved = candidate.resolve()
    for root in import_roots(configured):
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    return None


DEMO_DAY = __import__("datetime").date(2026, 11, 10)  # the day the bundled demo files are written for


def today():
    """Cash Desk's "today". CASHDESK_TODAY (an ISO date) pins it, for tests and for recording a
    demo on a chosen day; otherwise the real date."""
    import datetime as _dt
    import os

    text = os.environ.get("CASHDESK_TODAY", "").strip()
    try:
        return _dt.date.fromisoformat(text) if text else _dt.date.today()
    except ValueError:
        return _dt.date.today()


def _with_demo_records(result, records):
    from dataclasses import replace
    by_id = {record.id: record for record in records if hasattr(record, "id")}
    observations = []
    for observation in getattr(result, "bank_observations", ()):
        payment = by_id.get(observation.payment.id, observation.payment) if observation.payment else None
        evidence = observation.evidence
        if payment and payment.currency != observation.payment.currency and evidence and evidence.get("currency_basis"):
            basis = evidence["currency_basis"]
            evidence = {**evidence,"currency_basis":{**basis,"resolved":payment.currency,"basis":"demo_label",
                                                      "original_currency":basis["resolved"]}}
        observations.append(replace(observation,payment=payment,evidence=evidence))
    invoices = {record.number: record for record in records if hasattr(record, "number")}
    surpluses = tuple(replace(o, issue_date=invoices[o.number].issue_date, currency=invoices[o.number].currency)
                      if o.number in invoices else o for o in getattr(result, "invoice_surpluses", ()))
    return replace(result, records=records, bank_observations=tuple(observations), invoice_surpluses=surpluses)


def on_today(result, day=None):
    """The bundled demo always happens today: move every invoice and payment date by the gap
    between today and the demo's own day, so "7 days overdue" is true whenever it is loaded.
    Only for files from demo/; real files are never touched."""
    from dataclasses import fields, replace
    import datetime as _dt

    shift = (day or today()) - DEMO_DAY
    if not shift or not getattr(result, "records", None):
        return result
    moved = []
    for record in result.records:
        changes = {f.name: getattr(record, f.name) + shift for f in fields(record)
                   if isinstance(getattr(record, f.name), _dt.date) and not isinstance(getattr(record, f.name), _dt.datetime)}
        moved.append(replace(record, **changes) if changes else record)
    return _with_demo_records(result, tuple(moved))


DEFAULT_CURRENCY = "EUR"


def chosen_currency(configured: str | None = "") -> str:
    """The ``currency`` setting as a three-letter code; EUR when it is empty or not a code."""
    text = (configured or "").strip().upper()
    return text if len(text) == 3 and text.isalpha() else DEFAULT_CURRENCY


def is_demo_file(path: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(DEMO_DIR)
    except OSError:
        return False


def in_currency(result, currency: str):
    """The bundled demo is written in EUR; show it in the currency the person chose. Only the
    label changes (the demo's numbers stay), and only for files from demo/."""
    from dataclasses import replace

    if currency == DEFAULT_CURRENCY or not getattr(result, "records", None):
        return result
    records = tuple(replace(r, currency=currency) if getattr(r, "currency", None) == DEFAULT_CURRENCY else r for r in result.records)
    return _with_demo_records(result, records)


def outbox_dir(configured: str = "") -> Path:
    """The .eml outbox: the configured folder, else <data dir>/outbox."""
    text = (configured or "").strip()
    return Path(text).expanduser() if text else data_dir() / "outbox"
