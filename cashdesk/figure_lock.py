"""Figure Lock: the model writes the words; the ledger owns the numbers.

``extract_figures`` reads every amount, date, invoice number, payment reference
and IBAN out of free text in either language and puts each into a canonical
form (Decimal plus currency, ISO date, letters-and-digits identifier,
spaceless IBAN with its checksum verified). ``check`` compares what a text
contains with the figures a draft must carry (``required``) and the figures
the ledger allows for that customer (``allowed``): a required figure that is
absent is *missing*, a figure outside the ledger is *extra*, and a figure that
is close to a required one but wrong is *changed*. ``revise`` applies a new
text to a draft only when the lock passes.

Extraction is deliberately conservative. Plain counts (``14 days``), years and
short numbers are not figures; a long digit string, a spelled-out amount, an
amount without its currency, or an IBAN that fails its checksum is.
"""

from __future__ import annotations

import bisect
import difflib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from decimal import Decimal

from .approval import with_hash
from .dates import MONTH_NAME_RE, parse_date
from .model import Draft, Figure, Invoice
from .money import format_money, normalise_currency, parse_amount

ID_KINDS = ("invoice_no", "reference")
KIND_LABEL = {"money": "amount", "date": "date", "invoice_no": "invoice number", "reference": "reference", "iban": "IBAN"}

IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BR": 29, "CH": 21, "CR": 22, "CY": 28,
    "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23,
    "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30,
    "NL": 18, "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24, "SE": 24, "SI": 19,
    "SK": 24, "SM": 27, "TN": 24, "TR": 26, "UA": 29, "VG": 24, "XK": 20,
}

# ------------------------------------------------------------- spelled numbers

EN_SMALL = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
CS_SMALL = {
    "nula": 0, "jedna": 1, "jeden": 1, "jedno": 1, "dva": 2, "dvě": 2, "dve": 2, "tři": 3, "tri": 3, "čtyři": 4, "ctyri": 4,
    "pět": 5, "pet": 5, "šest": 6, "sest": 6, "sedm": 7, "osm": 8, "devět": 9, "devet": 9, "deset": 10, "jedenáct": 11,
    "dvanáct": 12, "třináct": 13, "čtrnáct": 14, "patnáct": 15, "šestnáct": 16, "sedmnáct": 17, "osmnáct": 18, "devatenáct": 19,
    "dvacet": 20, "třicet": 30, "čtyřicet": 40, "padesát": 50, "šedesát": 60, "sedmdesát": 70, "osmdesát": 80, "devadesát": 90,
}
HUNDRED_WORDS = {"hundred", "sto", "stě", "sta", "set"}
THOUSAND_WORDS = {"thousand", "tisíc", "tisíce", "tisíců", "tisic"}
MILLION_WORDS = {"million", "millions", "milion", "miliony", "milionů", "milión", "miliónů"}
CONNECTORS = {"and", "a"}
SMALL = {**EN_SMALL, **CS_SMALL}
NUMBER_WORDS = sorted(set(SMALL) | HUNDRED_WORDS | THOUSAND_WORDS | MILLION_WORDS, key=len, reverse=True)
CURRENCY_WORD_RE = r"(?:euros?|eura|eurech|eur|korun(?:a|y|ách)?|kč|czk|crowns?|dollars?|usd|pounds?|gbp)"
CENT_RE = r"(?:cents?|centů|centy|haléřů|haléře|hal\.?)"
_NUMBER_WORD_RE = "|".join(re.escape(w) for w in NUMBER_WORDS)
SPELLED_RE = re.compile(
    rf"\b(?P<words>(?:{_NUMBER_WORD_RE})(?:[\s-]+(?:{_NUMBER_WORD_RE}|and|a))*)\s+(?P<cur>{CURRENCY_WORD_RE})\b"
    rf"(?:\s+(?:and|a)\s+(?P<cents>\d{{1,2}})\s+{CENT_RE})?",
    re.IGNORECASE,
)


def spelled_number(words: str, lang: str = "en") -> Decimal | None:
    """``twelve hundred and fifty`` -> 1250; ``tisíc dvě stě padesát`` -> 1250; None when not a number."""
    tokens = [t for t in re.split(r"[\s-]+", (words or "").strip().lower()) if t]
    if not tokens:
        return None
    total, current, seen = 0, 0, False
    for tok in tokens:
        if tok in CONNECTORS:
            continue
        if tok in SMALL:
            current += SMALL[tok]
        elif tok in HUNDRED_WORDS:
            current = (current or 1) * 100
        elif tok in THOUSAND_WORDS:
            total += (current or 1) * 1000
            current = 0
        elif tok in MILLION_WORDS:
            total += (current or 1) * 1_000_000
            current = 0
        else:
            return None
        seen = True
    return Decimal(total + current) if seen else None


# ---------------------------------------------------------------------- IBAN


def iban_valid(iban: str) -> bool:
    """Format, country length (when known) and the ISO 7064 mod-97 checksum."""
    s = "".join(str(iban or "").split()).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    expected = IBAN_LENGTHS.get(s[:2])
    if expected is not None and len(s) != expected:
        return False
    digits = "".join(str(int(ch, 36)) for ch in s[4:] + s[:4])
    return int(digits) % 97 == 1


# ---------------------------------------------------------------- scanning

_IBAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}(?![A-Z0-9])")
_ISO_RE = re.compile(r"(?<!\d)\d{4}[-/]\d{2}[-/]\d{2}(?!\d)")
_LONG_DMY_RE = re.compile(rf"(?<!\d)\d{{1,2}}(?:st|nd|rd|th)?\.?\s+(?:{MONTH_NAME_RE})\.?,?\s+\d{{4}}(?!\d)", re.IGNORECASE)
_LONG_MDY_RE = re.compile(rf"\b(?:{MONTH_NAME_RE})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}(?!\d)", re.IGNORECASE)
_DOTTED_RE = re.compile(r"(?<!\d)\d{1,2}\.\s?\d{1,2}\.\s?\d{4}(?!\d)")
_SLASHED_RE = re.compile(r"(?<![\d/])\d{1,2}/\d{1,2}/\d{4}(?![\d/])")
_CUR_PRE = r"(?:EUR|CZK|USD|GBP|PLN|CHF|€|\$|£|Kč)"
_CUR_POST = r"(?:EUR|CZK|USD|GBP|PLN|CHF|€|\$|£|Kč|euros?|eura|eurech|korun(?:a|y|ách)?|crowns?|dollars?|pounds?)"
_NUM = r"\d{1,3}(?:[ .,']\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
# A sign counts only when it touches what follows: "-12.50" and "EUR -12.50"
# are negative amounts, a bullet "- EUR 12.50" is not.
_MONEY_RE = re.compile(
    rf"(?<![\w.,])(?P<sign1>[-−](?=\S))?(?:(?P<pre>{_CUR_PRE})\s?)?(?P<sign2>[-−](?=\d))?"
    rf"(?P<num>{_NUM})(?P<dash>\s?,\s?[-–])?(?:\s?(?P<post>{_CUR_POST})(?![\w]))?(?![\w])"
)
_TOKEN_RE = re.compile(r"(?<![\w@./-])(?:[A-Za-z]{1,8}[-/]?)?\d[A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*(?![\w@/])")
_SKIP_RE = re.compile(r"\S+@\S+|https?://\S+|www\.\S+")
_ORDINAL_RE = re.compile(r"^\d+(?:st|nd|rd|th)$", re.IGNORECASE)
_SPLIT_RES = (
    re.compile(r"\d{1,3}(?:[ .,']\d{3})*[.,][ \t]*\n[ \t]*\d{2}(?!\d)"),
    re.compile(r"\d{1,3}[.,][ \t]*\n[ \t]*\d{3}(?:[.,]\d{2})?(?!\d)"),
    re.compile(r"\d[ \t]*\n[ \t]*[.,]\d{2}(?!\d)"),
)


@dataclass(frozen=True)
class Figures:
    """The figures found in one text, first occurrence of each, in text order."""

    items: tuple[Figure, ...] = ()

    def __iter__(self) -> Iterator[Figure]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __contains__(self, figure: object) -> bool:
        return figure in self.items

    def of(self, kind: str) -> tuple[Figure, ...]:
        return tuple(f for f in self.items if f.kind == kind)


class _Scanner:
    def __init__(self, text: str):
        self.text = text
        self.work = text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ")
        self.mask = bytearray(len(text))
        self.lines = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
        self.found: list[Figure] = []

    def line_of(self, pos: int) -> int:
        return bisect.bisect_right(self.lines, pos)

    def free(self, start: int, end: int) -> bool:
        return not any(self.mask[start:end])

    def take(self, start: int, end: int) -> None:
        for i in range(start, end):
            self.mask[i] = 1

    def add(self, figure: Figure) -> None:
        if figure not in self.found:
            self.found.append(figure)


def _iban_candidates(sc: _Scanner) -> None:
    for m in _IBAN_RE.finditer(sc.work):
        if not sc.free(m.start(), m.end()):
            continue
        span = m.group(0)
        country = span[:2]
        expected = IBAN_LENGTHS.get(country)
        end = m.end()
        if expected is not None:
            count, end = 0, m.start()
            for i, ch in enumerate(span):
                if ch.isalnum():
                    count += 1
                    if count == expected:
                        end = m.start() + i + 1
                        break
            if count < expected:
                continue
            raw = sc.text[m.start():end]
            compact = "".join(raw.split()).upper()
        else:
            raw = sc.text[m.start():end]
            compact = "".join(raw.split()).upper()
            if not iban_valid(compact):
                continue
        sc.take(m.start(), end)
        sc.add(Figure.iban(compact, raw, sc.line_of(m.start()), valid=iban_valid(compact)))


def _dates(sc: _Scanner, lang: str) -> None:
    for regex in (_ISO_RE, _LONG_DMY_RE, _LONG_MDY_RE, _DOTTED_RE, _SLASHED_RE):
        for m in regex.finditer(sc.work):
            if not sc.free(m.start(), m.end()):
                continue
            raw = sc.text[m.start():m.end()]
            parsed = parse_date(raw, lang)
            sc.take(m.start(), m.end())
            if parsed is None:
                sc.add(Figure("date", re.sub(r"\s+", "", raw), raw, sc.line_of(m.start()), valid=False))
            else:
                sc.add(Figure.date(parsed, raw, sc.line_of(m.start())))


def _money(sc: _Scanner, lang: str) -> None:
    for m in _MONEY_RE.finditer(sc.work):
        if not sc.free(m.start(), m.end()):
            continue
        num = m.group("num")
        has_currency = bool(m.group("pre") or m.group("post"))
        decimal_part = re.search(r"[.,]\d{2}$", num)
        separators = re.search(r"[.,']", num)
        if not has_currency and not m.group("dash") and not decimal_part and not separators:
            continue  # a bare count, year or phone-number group is not an amount
        raw = sc.text[m.start():m.end()]
        amount = parse_amount(raw, lang)
        if amount is None:
            continue
        sc.take(m.start(), m.end())
        sc.add(Figure.money(amount.value, amount.currency, raw, sc.line_of(m.start())))


def _spelled(sc: _Scanner, lang: str) -> None:
    for m in SPELLED_RE.finditer(sc.work):
        if not sc.free(m.start(), m.end()):
            continue
        value = spelled_number(m.group("words"), lang)
        currency = normalise_currency(m.group("cur"))
        if value is None or currency is None:
            continue
        if m.group("cents"):
            value += Decimal(m.group("cents")) / 100
        sc.take(m.start(), m.end())
        sc.add(Figure.money(value, currency, sc.text[m.start():m.end()], sc.line_of(m.start())))


def _identifiers(sc: _Scanner) -> None:
    for m in _TOKEN_RE.finditer(sc.work):
        if not sc.free(m.start(), m.end()):
            continue
        tok = m.group(0)
        if _ORDINAL_RE.match(tok):
            continue
        digits_only = re.sub(r"[-/]", "", tok).isdigit()
        if digits_only:
            digits = re.sub(r"[-/]", "", tok)
            if len(digits) < 4 or (len(digits) == 4 and 1900 <= int(digits) <= 2100):
                continue
            if digits != tok and len(digits) < 6:
                continue  # a short hyphenated pair is a page range or a house number, not a reference
            sc.take(m.start(), m.end())
            sc.add(Figure.reference(digits, tok, sc.line_of(m.start())))
        else:
            sc.take(m.start(), m.end())
            sc.add(Figure.invoice(tok, tok, sc.line_of(m.start())))


def extract_figures(text: str, lang: str = "en") -> Figures:
    """Every figure in ``text``: IBANs, dates, amounts (digits or words), identifiers."""
    sc = _Scanner(text or "")
    for m in _SKIP_RE.finditer(sc.work):
        sc.take(m.start(), m.end())
    _iban_candidates(sc)
    _dates(sc, lang)
    _money(sc, lang)
    _spelled(sc, lang)
    _identifiers(sc)
    return Figures(tuple(sc.found))


def broken_amounts(text: str) -> list[tuple[str, int]]:
    """Amounts split across a line break, as (fragment, line)."""
    out = []
    lines = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    for regex in _SPLIT_RES:
        for m in regex.finditer(text):
            out.append((m.group(0), bisect.bisect_right(lines, m.start())))
    return out


# ---------------------------------------------------------------- the lock


@dataclass(frozen=True)
class LockResult:
    ok: bool
    missing: tuple[Figure, ...] = ()
    extra: tuple[Figure, ...] = ()
    changed: tuple[tuple[Figure, Figure], ...] = ()
    diff: tuple[str, ...] = ()
    found: Figures = Figures()


@dataclass(frozen=True)
class RevisionResult:
    ok: bool
    draft: Draft
    lock: LockResult


def ledger_figures(invoices: Iterable[Invoice], extra: Iterable[Figure] = ()) -> Figures:
    """Every figure the ledger holds for these invoices, plus ``extra`` (e.g. the sender's own identifiers)."""
    out: list[Figure] = []
    for inv in invoices:
        out.append(Figure.invoice(inv.number))
        out.append(Figure.money(inv.amount, inv.currency))
        if inv.balance > 0:
            out.append(Figure.money(inv.balance, inv.currency))
        out.append(Figure.date(inv.due_date))
        out.append(Figure.date(inv.issue_date))
        if inv.paid_date:
            out.append(Figure.date(inv.paid_date))
        if inv.reference:
            out.append(Figure.reference(inv.reference))
        if inv.iban:
            out.append(Figure.iban(inv.iban))
    out.extend(extra)
    return Figures(tuple(dict.fromkeys(out)))


def _same_identifier(a: Figure, b: Figure) -> bool:
    return a.kind in ID_KINDS and b.kind in ID_KINDS and a.canonical == b.canonical


def _satisfies(found: Figure, required: Figure) -> bool:
    """``found`` is exactly ``required`` (currency included for money)."""
    if not found.valid:
        return False
    if required.kind == "money":
        return found.kind == "money" and found.currency == required.currency and found.amount == required.amount
    if required.kind in ID_KINDS:
        return _same_identifier(found, required)
    return found.kind == required.kind and found.canonical == required.canonical


def _explained(found: Figure, allowed: Iterable[Figure]) -> bool:
    """``found`` is a ledger figure; an amount without a currency counts when the amount is a ledger amount."""
    if not found.valid:
        return False
    for a in allowed:
        if found.kind == "money" and a.kind == "money" and found.amount == a.amount and (found.currency is None or found.currency == a.currency):
            return True
        if found.kind != "money" and _satisfies(found, a):
            return True
    return False


def _ratio(a: str, b: str) -> Decimal:
    return Decimal(str(round(difflib.SequenceMatcher(None, a, b).ratio(), 4)))


def _closeness(found: Figure, required: Figure) -> Decimal | None:
    """Smaller is closer (0 to about 1); None when the two are not plausibly the same figure."""
    if found.kind == "money" and required.kind == "money":
        if found.currency not in (None, required.currency) and found.amount != required.amount:
            return None
        if found.amount == required.amount:
            return Decimal("0")
        rel = abs(found.amount - required.amount) / max(required.amount, Decimal("0.01"))
        if rel <= Decimal("0.5") or _ratio(f"{found.amount}", f"{required.amount}") >= Decimal("0.6"):
            return min(rel, Decimal(1)) + (Decimal("0") if found.currency == required.currency else Decimal("0.01"))
        return None
    if found.kind == "date" and required.kind == "date":
        if not found.valid:
            return Decimal("1")
        days = abs((parse_date(found.canonical, "en") - parse_date(required.canonical, "en")).days)  # type: ignore[operator]
        return Decimal(days) / 400 if days <= 400 else None
    if found.kind in ID_KINDS and required.kind in ID_KINDS:
        ratio = _ratio(found.canonical, required.canonical)
        return 1 - ratio if ratio >= Decimal("0.6") else None
    if found.kind == "iban" and required.kind == "iban":
        ratio = _ratio(found.canonical, required.canonical)
        if (found.canonical[:2] == required.canonical[:2] and ratio >= Decimal("0.8")) or ratio >= Decimal("0.9"):
            return 1 - ratio
        return None
    return None


def describe(figure: Figure) -> str:
    """Short human form: ``EUR 1,250.00``, ``2026-10-27``, ``INV-2026-0042``, ``DE89370400440532013000``."""
    if figure.kind == "money":
        amount = figure.amount or Decimal("0")
        if figure.currency is None:
            return f"{amount:,.2f} (no currency)"
        return format_money(amount, figure.currency, "en")
    if figure.kind in ID_KINDS:
        return figure.raw or figure.canonical
    if figure.kind == "date" and not figure.valid:
        return figure.raw or figure.canonical
    return figure.canonical


def _note(figure: Figure, required: Figure | None = None) -> str:
    if figure.kind == "iban" and not figure.valid:
        return " - IBAN fails its checksum"
    if figure.kind == "date" and not figure.valid:
        return " - not a valid date"
    if figure.kind == "money" and figure.currency is None and required is not None:
        return " - currency missing"
    if figure.kind == "money" and required is not None and figure.currency != required.currency and figure.amount == required.amount:
        return " - currency changed"
    return ""


def unknown_figures(text: str, lang: str, allowed: Iterable[Figure], kinds: Iterable[str] = ("money", "iban", "invoice_no")) -> list[Figure]:
    """Figures of ``kinds`` in ``text`` that ``allowed`` does not explain, in order of appearance.

    The reply guard's question: does this free text state an amount, IBAN or invoice number the
    ledger does not hold? Dates and plain counts are not asked about.
    """
    wanted = set(kinds)
    allowed_all = tuple(allowed)
    return [f for f in extract_figures(text, lang) if f.kind in wanted and not _explained(f, allowed_all)]


def check(text: str, lang: str, required: Iterable[Figure], allowed: Iterable[Figure]) -> LockResult:
    """Pass only when every required figure is present and nothing outside ``allowed`` appears.

    ``changed`` pairs a required figure with the nearest wrong value found
    (``EUR 1,250.00 -> EUR 1,520.00``); ``diff`` explains each rejection in one
    line with the line number in ``text`` (line 1 is the first line given).
    """
    req = tuple(dict.fromkeys(required))
    allowed_all = tuple(dict.fromkeys((*allowed, *req)))
    found = extract_figures(text, lang)
    splits = broken_amounts(text)
    unexplained = [f for f in found if not _explained(f, allowed_all)]
    satisfied = {r for r in req if any(_satisfies(f, r) for f in found)}
    changed: list[tuple[Figure, Figure]] = []
    paired: set[int] = set()
    # an amount written without its currency, or in another currency, is a change of the required amount
    for r in req:
        if r.kind != "money" or r in satisfied:
            continue
        for f in found:
            if f.kind == "money" and f.amount == r.amount and f.currency != r.currency and id(f) not in paired:
                changed.append((r, f))
                paired.add(id(f))
                break
    for f in unexplained:
        if id(f) in paired:
            continue
        best: tuple[Decimal, Figure] | None = None
        for r in req:
            score = _closeness(f, r)
            if score is not None and (best is None or score < best[0]):
                best = (score, r)
        if best is not None:
            changed.append((best[1], f))
            paired.add(id(f))
    extra = tuple(f for f in unexplained if id(f) not in paired)
    explained_by_change = {r for r, _ in changed}
    missing = tuple(r for r in req if r not in satisfied and r not in explained_by_change)
    diff: list[str] = []
    for r, f in changed:
        diff.append(f"changed: {describe(r)} -> {describe(f)} (line {f.line}: {f.raw!r}){_note(f, r)}")
    for r in missing:
        diff.append(f"missing: {KIND_LABEL[r.kind]} {describe(r)} not found in the text")
    for f in extra:
        diff.append(f"extra: {KIND_LABEL[f.kind]} {describe(f)} is not a ledger figure (line {f.line}){_note(f)}")
    for raw, line in splits:
        diff.append(f"split: {raw!r} - an amount is broken across a line break (line {line})")
    ok = not missing and not extra and not changed and not splits
    return LockResult(ok, missing, extra, tuple(changed), tuple(diff), found)


def revise(draft: Draft, new_text: str, allowed: Iterable[Figure], new_subject: str | None = None) -> RevisionResult:
    """Apply ``new_text`` (and optionally a new subject) to ``draft`` only when the lock passes.

    The required set is the draft's own figures; subject and body are checked
    together. On success the returned draft carries the new text and a new
    hash; on failure the original draft is returned untouched with the diff.
    """
    subject = draft.subject if new_subject is None else new_subject
    lock = check(f"{subject}\n{new_text}", draft.language, draft.figures, allowed)
    if not lock.ok:
        return RevisionResult(False, draft, lock)
    revised = with_hash(replace(draft, subject=subject, body=new_text, status="pending"))
    return RevisionResult(True, revised, lock)


__all__ = [
    "ID_KINDS",
    "IBAN_LENGTHS",
    "Figures",
    "LockResult",
    "RevisionResult",
    "spelled_number",
    "iban_valid",
    "extract_figures",
    "unknown_figures",
    "broken_amounts",
    "ledger_figures",
    "describe",
    "check",
    "revise",
]
