"""Parsing/normalisation helpers shared by every extractor. They return None rather than guessing."""
import re
from datetime import date, datetime

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
ISO_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "INR", "CAD", "AUD", "CHF", "CNY", "SEK", "NOK", "DKK", "NZD", "SGD", "HKD",
    "MXN", "BRL", "ZAR", "AED", "SAR", "PLN", "CZK", "HUF", "TRY", "KRW", "THB", "IDR", "MYR", "PHP", "PKR",
    "ILS", "RUB", "EGP", "NGN", "KES",
}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_money(value) -> float | None:
    """'$1,234.56' | '1.234,56' | '(45.00)' | 1234 -> float. Returns None if not a clean number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if value != value else round(float(value), 2)
    s = str(value).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-") or s.endswith("-")
    s = re.sub(r"[A-Za-z$€£¥₹\s()\-+]", "", s)
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # European 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + "." + tail if len(tail) in (1, 2) else s.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        return None
    v = round(float(s), 2)
    return -v if neg else v


def parse_number(value) -> float | None:
    """Quantities (can be fractional, no currency)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)
    s = str(value).strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(value) -> tuple[str | None, bool]:
    """Returns (iso_date | None, ambiguous). Ambiguous = dd/mm vs mm/dd cannot be told apart."""
    if value is None:
        return None, False
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d"), False
    s = str(value).strip()
    if not s:
        return None, False
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s, flags=re.I)
    m = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", s)
    if m:
        return _mk(int(m[1]), int(m[2]), int(m[3])), False
    m = re.search(r"\b(\d{1,2})[\s\-/.]*([A-Za-z]{3,9})\.?,?[\s\-/.]*(\d{4})\b", s)
    if m and m[2][:3].lower() in MONTHS:
        return _mk(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1])), False
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", s)
    if m and m[1][:3].lower() in MONTHS:
        return _mk(int(m[3]), MONTHS[m[1][:3].lower()], int(m[2])), False
    m = re.search(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 >= b:
            return _mk(y, b, a), False
        if b > 12 >= a:
            return _mk(y, a, b), False
        if a == b:
            return _mk(y, a, b), False
        return _mk(y, a, b), True  # assume US mm/dd, flag as ambiguous
    return None, False


def _mk(y, m, d):
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def detect_currency(text: str) -> tuple[str | None, float]:
    """Returns (ISO code, confidence). Explicit code beats symbol; '$' alone is ambiguous."""
    m = re.search(r"(?i)\bcurrency\s*[:\-]?\s*([A-Z]{3})\b", text)
    if m and m[1].upper() in ISO_CURRENCIES:
        return m[1].upper(), 0.95
    codes = [c for c in re.findall(r"\b([A-Z]{3})\b", text) if c in ISO_CURRENCIES]
    if codes:
        best = max(set(codes), key=codes.count)
        return best, 0.9
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in text:
            return code, 0.6 if sym in "$¥" else 0.8
    return None, 0.0


def clean_str(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip(" \t:;|-")
    return s or None


def norm_key(v) -> str | None:
    """Normalised key for duplicate detection."""
    if not v:
        return None
    return re.sub(r"[^a-z0-9]", "", str(v).lower()) or None


def new_invoice() -> dict:
    return {"invoice_number": None, "invoice_date": None, "supplier_name": None, "customer_name": None,
            "subtotal": None, "tax": None, "total": None, "currency": None, "line_items": []}


FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name",
          "subtotal", "tax", "total", "currency"]
