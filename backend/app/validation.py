"""Validation rules. `validate_invoice` is pure (unit-testable); duplicate checks need the DB."""
import math
from dataclasses import dataclass
from datetime import date, timedelta

from .config import settings
from .extraction.normalize import ISO_CURRENCIES, norm_key, parse_date

REQUIRED_ERROR = ["invoice_number", "invoice_date", "supplier_name", "total"]
REQUIRED_WARN = ["customer_name", "currency"]
REVIEW_WARNING_CODES = {"MISSING_FIELD", "LOW_CONFIDENCE", "LOW_OCR_CONFIDENCE", "AMBIGUOUS_DATE", "NO_LINE_ITEMS"}


@dataclass
class Issue:
    code: str
    severity: str  # error | warning
    field: str | None
    message: str


def _ts(dt):
    """Naive-UTC timestamp: SQLite returns naive datetimes while in-session defaults are tz-aware."""
    from datetime import timezone
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _tol(v: float) -> float:
    return max(settings.amount_abs_tol, abs(v) * settings.amount_rel_tol)


def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isnan(v) and not math.isinf(v)


def validate_invoice(data: dict, conf: dict | None = None, ocr_conf: float | None = None) -> list[Issue]:
    conf, out = conf or {}, []
    add = lambda c, s, f, m: out.append(Issue(c, s, f, m))  # noqa: E731

    # 1. missing fields
    for f in REQUIRED_ERROR:
        if data.get(f) in (None, ""):
            add("MISSING_FIELD", "error", f, f"Required field '{f}' is missing")
    for f in REQUIRED_WARN:
        if data.get(f) in (None, ""):
            add("MISSING_FIELD", "warning", f, f"Field '{f}' is missing")
    items = data.get("line_items") or []
    if not items:
        add("NO_LINE_ITEMS", "warning", "line_items", "No line items were extracted")

    # 2. numeric sanity
    for f in ("subtotal", "tax", "total"):
        v = data.get(f)
        if v is None:
            continue
        if not _isnum(v):
            add("INVALID_NUMBER", "error", f, f"'{f}' is not a valid number: {v!r}")
        elif v < 0:
            add("NEGATIVE_AMOUNT", "warning", f, f"'{f}' is negative ({v}) - credit note?")
    for i, li in enumerate(items, 1):
        for f in ("quantity", "unit_price", "amount"):
            v = li.get(f)
            if v is not None and not _isnum(v):
                add("INVALID_NUMBER", "error", f"line_items[{i}].{f}", f"Line {i}: '{f}' is not numeric: {v!r}")
            elif _isnum(v) and v < 0 and f != "amount":
                add("NEGATIVE_AMOUNT", "warning", f"line_items[{i}].{f}", f"Line {i}: negative {f} ({v})")
        q, u, a = li.get("quantity"), li.get("unit_price"), li.get("amount")
        if all(_isnum(x) for x in (q, u, a)) and abs(q * u - a) > _tol(a):
            add("LINE_ITEM_MISMATCH", "error", f"line_items[{i}]",
                f"Line {i}: quantity x unit price = {q * u:.2f} but amount is {a:.2f}")

    # 3. date / currency
    d = data.get("invoice_date")
    if d:
        iso, _ = parse_date(d)
        if not iso:
            add("INVALID_DATE", "error", "invoice_date", f"Invalid date: {d!r}")
        else:
            dd = date.fromisoformat(iso)
            if dd > date.today() + timedelta(days=1):
                add("FUTURE_DATE", "warning", "invoice_date", f"Invoice date {iso} is in the future")
            elif dd.year < 2000:
                add("OLD_DATE", "warning", "invoice_date", f"Invoice date {iso} is implausibly old")
    if conf.get("invoice_date", 1) is not None and 0 < conf.get("invoice_date", 1) <= 0.6 and d:
        add("AMBIGUOUS_DATE", "warning", "invoice_date", "Date format ambiguous (dd/mm vs mm/dd) - please confirm")
    cur = data.get("currency")
    if cur and cur.upper() not in ISO_CURRENCIES:
        add("INVALID_CURRENCY", "warning", "currency", f"Unrecognised currency code {cur!r}")

    # 4. arithmetic consistency
    sub, tax, tot = data.get("subtotal"), data.get("tax"), data.get("total")
    amounts = [li.get("amount") for li in items]
    if items and all(_isnum(a) for a in amounts):
        s = round(sum(amounts), 2)
        if _isnum(sub) and abs(s - sub) > _tol(sub):
            add("SUBTOTAL_MISMATCH", "error", "subtotal", f"Sum of line items ({s:.2f}) != subtotal ({sub:.2f})")
        elif sub is None and _isnum(tot) and not _isnum(tax) and abs(s - tot) > _tol(tot):
            add("TOTAL_MISMATCH", "error", "total", f"Sum of line items ({s:.2f}) != total ({tot:.2f})")
    if _isnum(sub) and _isnum(tax) and _isnum(tot) and abs(sub + tax - tot) > _tol(tot):
        add("TOTAL_MISMATCH", "error", "total", f"Subtotal ({sub:.2f}) + tax ({tax:.2f}) = {sub + tax:.2f} but total is {tot:.2f}")
    elif sub is None and _isnum(tax) and _isnum(tot) and items and all(_isnum(a) for a in amounts):
        s = round(sum(amounts), 2)
        if abs(s + tax - tot) > _tol(tot):
            add("TOTAL_MISMATCH", "error", "total", f"Line items ({s:.2f}) + tax ({tax:.2f}) != total ({tot:.2f})")
    if _isnum(sub) and _isnum(tax) and sub > 0 and tax >= 0 and tax / sub > 0.5:
        add("TAX_RATE_SUSPICIOUS", "warning", "tax", f"Tax is {tax / sub:.0%} of subtotal")
    if _isnum(tot) and _isnum(sub) and sub >= 0 and tot < sub - _tol(sub) and (tax or 0) >= 0:
        add("TOTAL_BELOW_SUBTOTAL", "error", "total", f"Total ({tot:.2f}) is lower than subtotal ({sub:.2f})")

    # 5. confidence
    low = [f for f in REQUIRED_ERROR + REQUIRED_WARN + ["subtotal", "tax"]
           if data.get(f) is not None and conf.get(f) is not None and conf[f] < 0.6]
    if low:
        add("LOW_CONFIDENCE", "warning", ",".join(low), f"Low extraction confidence for: {', '.join(low)}")
    if ocr_conf is not None and ocr_conf < settings.ocr_min_confidence:
        add("LOW_OCR_CONFIDENCE", "warning", None, f"Poor scan quality (OCR confidence {ocr_conf:.0f}%)")
    return out


def needs_review(issues: list[Issue]) -> bool:
    return any(i.severity == "error" or i.code in REVIEW_WARNING_CODES for i in issues)


def duplicate_issues(session, extraction, document) -> list[Issue]:
    """Flag an invoice as duplicate when an *earlier* extraction has the same supplier + invoice number,
    or when the whole file is byte-identical to an earlier upload."""
    from sqlalchemy import select

    from .models import Document, Extraction

    out = []
    if extraction.invoice_number_norm:
        q = select(Extraction, Document).join(Document, Extraction.document_id == Document.id).where(
            Extraction.invoice_number_norm == extraction.invoice_number_norm,
            Extraction.id != extraction.id,
        )
        if extraction.supplier_norm:
            q = q.where(Extraction.supplier_norm == extraction.supplier_norm)
        for other, odoc in session.execute(q).all():
            # the earlier one is the original; ties broken by id so exactly one of a pair is flagged
            if (_ts(odoc.created_at), _ts(other.created_at), other.id) < (_ts(document.created_at), _ts(extraction.created_at), extraction.id):
                out.append(Issue("DUPLICATE_INVOICE", "error", "invoice_number",
                                 f"Duplicate of invoice {other.invoice_number} in document '{odoc.filename}' ({odoc.id})"))
                break
    same = session.execute(select(Document).where(Document.sha256 == document.sha256, Document.id != document.id)).scalars().all()
    for o in same:
        if (_ts(o.created_at), o.id) < (_ts(document.created_at), document.id):
            out.append(Issue("DUPLICATE_FILE", "warning", None,
                             f"File is byte-identical to '{o.filename}' ({o.id}) uploaded earlier"))
            break
    return out


def propagate_duplicates(session, extraction, document):
    """Order-independence for concurrent workers: if a *later* copy was validated before this (earlier)
    invoice was committed, it could not see us. Re-check the later copies now and flag them."""
    from sqlalchemy import select

    from .models import Document, Extraction, ValidationIssue

    if not extraction.invoice_number_norm:
        return []
    q = select(Extraction, Document).join(Document, Extraction.document_id == Document.id).where(
        Extraction.invoice_number_norm == extraction.invoice_number_norm, Extraction.id != extraction.id)
    if extraction.supplier_norm:
        q = q.where(Extraction.supplier_norm == extraction.supplier_norm)
    touched = []
    for other, odoc in session.execute(q).all():
        if (_ts(odoc.created_at), _ts(other.created_at), other.id) > (_ts(document.created_at), _ts(extraction.created_at), extraction.id):
            if not any(i.code == "DUPLICATE_INVOICE" for i in other.issues):
                session.add(ValidationIssue(
                    document_id=odoc.id, extraction_id=other.id, code="DUPLICATE_INVOICE", severity="error",
                    field="invoice_number",
                    message=f"Duplicate of invoice {extraction.invoice_number} in document '{document.filename}' ({document.id})"))
                other.needs_review = not other.reviewed
                touched.append(odoc)
    return touched
