"""Cash Desk engine: pure functions over Decimal money, no Hermes imports.

The Hermes plugin in the parent package wires these functions to tools, the
approval gate and the Desktop page. Nothing here reads files outside the
paths it is given, opens a network connection or touches secrets. See
``README.md`` in this folder for the public API.
"""

from .approval import canonical_text, draft_hash, idempotency_key, verify_hash, with_hash
from .brief import brief_window, morning_brief
from .chasing import (
    ESCALATE_AFTER,
    STAGES,
    ChaseState,
    FollowupPlan,
    HeldItem,
    Reply,
    classify_reply,
    plan_followups,
    stage_for,
)
from .dates import format_date, parse_date
from .figure_lock import (
    figure_status,
    Figures,
    LockResult,
    RevisionResult,
    broken_amounts,
    check,
    describe,
    extract_figures,
    unknown_figures,
    iban_valid,
    ledger_figures,
    revise,
    spelled_number,
)
from .importers import (
    SOURCES,
    ImportResult,
    RowError,
    clean_reference,
    fingerprint,
    read_bank_csv,
    read_customers,
    read_invoices,
    slugify,
)
from .matching import (
    NAME_MIN,
    REVIEW_KINDS,
    Candidate,
    MatchResult,
    ReviewItem,
    invoices_named,
    is_refund,
    match_payments,
    payer_fits,
    name_similarity,
    normalise_name,
)
from .model import (
    DRAFT_KINDS,
    DRAFT_STATUSES,
    FIGURE_KINDS,
    INVOICE_STATUSES,
    LANGUAGES,
    Customer,
    Draft,
    Figure,
    Invoice,
    Match,
    Payment,
    to_money,
)
from .money import Amount, cs_days, format_money, format_totals, parse_amount, quantize, sum_by_currency
from .render import (
    KINDS,
    DraftLine,
    RenderContext,
    TemplateError,
    fill_template,
    format_iban,
    invoice_line,
    load_template,
    payment_details,
    render_draft,
    stage_label,
)
from .store import (
    AlreadySentError,
    Approval,
    BriefSnapshot,
    ConflictError,
    DuplicateImportError,
    Event,
    HashMismatchError,
    ImportRecord,
    NotApprovedError,
    NotFoundError,
    PaymentView,
    SendRecord,
    StatusSnapshot,
    Store,
    StoreError,
    bucket_for,
)

ENGINE_VERSION = "1.2.0"

__all__ = [
    "ENGINE_VERSION",
    # model
    "LANGUAGES", "INVOICE_STATUSES", "DRAFT_KINDS", "DRAFT_STATUSES", "FIGURE_KINDS",
    "to_money", "Customer", "Invoice", "Payment", "Match", "Figure", "Draft",
    # money and dates
    "Amount", "quantize", "parse_amount", "format_money", "sum_by_currency", "format_totals", "cs_days",
    "parse_date", "format_date",
    # importers
    "SOURCES", "RowError", "ImportResult", "fingerprint", "slugify", "clean_reference",
    "read_invoices", "read_bank_csv", "read_customers",
    # matching
    "NAME_MIN", "REVIEW_KINDS", "Candidate", "ReviewItem", "MatchResult",
    "normalise_name", "name_similarity", "is_refund", "invoices_named", "match_payments", "payer_fits",
    # chasing
    "STAGES", "ESCALATE_AFTER", "stage_for", "ChaseState", "Reply", "classify_reply", "HeldItem", "FollowupPlan", "plan_followups",
    # render
    "KINDS", "TemplateError", "DraftLine", "RenderContext", "load_template", "fill_template", "format_iban",
    "invoice_line", "payment_details", "stage_label", "render_draft",
    # figure lock
    "Figures", "LockResult", "RevisionResult", "spelled_number", "iban_valid", "figure_status", "extract_figures", "unknown_figures", "broken_amounts",
    "ledger_figures", "describe", "check", "revise",
    # approval
    "canonical_text", "draft_hash", "idempotency_key", "verify_hash", "with_hash",
    # brief
    "brief_window", "morning_brief",
    # store
    "Store", "StoreError", "DuplicateImportError", "HashMismatchError", "NotApprovedError", "AlreadySentError",
    "ConflictError", "NotFoundError", "ImportRecord", "Approval", "SendRecord", "Event", "PaymentView",
    "StatusSnapshot", "BriefSnapshot", "bucket_for",
]
