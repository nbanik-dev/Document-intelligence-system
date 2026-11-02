"""Deterministic rule-based extractor. Used (a) as fallback when no LLM key/quota, (b) as the baseline in
the evaluation. Principle: if a field is not clearly present it stays None - never guessed."""
import re

from .normalize import clean_str, detect_currency, new_invoice, parse_date, parse_money, parse_number

M = r"\(?-?\s?[$€£¥₹]?\s?\d[\d,.\s]*\d(?:\s?[$€£¥₹])?\)?|\(?-?\s?[$€£¥₹]?\s?\d\)?"
MONEY_RE = re.compile(r"-?\(?[$€£¥₹]?\s?\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?\)?|-?\(?[$€£¥₹]?\s?\d+(?:\.\d{1,2})?\)?")
INV_NO_RE = re.compile(
    r"(?i)\b(?:tax\s+)?(?:invoice|inv|bill)\s*(?:number|no\.?|num\.?|nr\.?|#|id)\s*[:#.\-]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-/_.]*\d[A-Za-z0-9\-/_.]*)")
INV_NO_RE2 = re.compile(r"(?i)\binvoice\s*[:#]\s*([A-Za-z0-9][A-Za-z0-9\-/_.]*\d[A-Za-z0-9\-/_.]*)")
DATE_TXT = r"([0-9]{1,4}[\s/.\-][0-9A-Za-z]{1,9}[\s/.\-,]*[0-9]{1,4}|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
DATE_LABEL_RE = re.compile(r"(?i)\b(?:invoice\s*date|date\s*of\s*issue|issue\s*date|date\s*issued|billing\s*date|date)\s*[:\-]?\s*" + DATE_TXT)
SUPPLIER_LABELS = r"(?:from|supplier|vendor|seller|sold\s*by|issued\s*by|remit\s*to)"
CUSTOMER_LABELS = r"(?:bill(?:ed)?\s*to|customer|client|sold\s*to|buyer|invoice\s*to)"
COMPANY_RE = re.compile(r"(?i)\b(ltd|limited|inc|llc|llp|plc|corp|corporation|gmbh|co|company|pty|s\.?a\.?|ag|bv|group|holdings|industries|solutions|services|supplies|traders|systems|partners)\b\.?")
GENERIC_TOP = re.compile(r"(?i)^(tax\s+)?(invoice|receipt|bill|credit\s*note|statement|page\s*\d+|\[page \d+\])\b")
STOP_LINE = re.compile(r"(?i)^\s*(sub\s*-?\s*total|total|tax|vat|gst|sales\s*tax|amount\s*due|balance|grand\s*total|net\s*amount|payment|notes?|terms)\b")
LINE_RE = re.compile(
    r"^(?P<desc>.*?[A-Za-z].*?)\s+(?P<qty>\d+(?:\.\d+)?)\s+(?P<unit>(?:" + MONEY_RE.pattern + r"))\s+(?P<amt>(?:" + MONEY_RE.pattern + r"))\s*$")


def _label_value(lines, label_re, same_line=True):
    """Value after a 'Label:' either on the same line or the next non-empty line."""
    pat = re.compile(r"(?i)^\W*" + label_re + r"\b\s*[:\-]?\s*(.*)$")
    for i, ln in enumerate(lines):
        m = pat.match(ln.strip())
        if m:
            val = m.group(1).strip()
            if val:
                return val
            for nxt in lines[i + 1: i + 3]:
                if nxt.strip():
                    return nxt.strip()
    return None


def _last_money(line: str):
    line = re.sub(r"\d+(?:\.\d+)?\s*%", " ", line)  # drop percentages (e.g. 'Tax (10%)')
    line = re.sub(r"(?i)\b(?:usd|eur|gbp|cad|aud|inr|jpy|chf)\b", " ", line)
    vals = [parse_money(m.group(0)) for m in MONEY_RE.finditer(line)]
    vals = [v for v in vals if v is not None]
    return vals[-1] if vals else None


def extract_invoice(text: str) -> tuple[dict, dict]:
    """text -> (invoice dict, per-field confidence)."""
    inv, conf = new_invoice(), {}
    lines = [l.rstrip() for l in text.splitlines() if l.strip() and not re.match(r"^\[page \d+\]$", l.strip())]

    # invoice number
    for ln in lines:
        m = INV_NO_RE.search(ln) or INV_NO_RE2.search(ln)
        if m:
            inv["invoice_number"], conf["invoice_number"] = m.group(1).strip(".,;"), 0.9
            break

    # invoice date (skip 'due date' lines unless explicitly 'invoice date')
    for ln in lines:
        if re.search(r"(?i)\bdue\b", ln) and not re.search(r"(?i)invoice\s*date", ln):
            continue
        m = DATE_LABEL_RE.search(ln)
        if m:
            iso, amb = parse_date(m.group(1))
            if iso:
                inv["invoice_date"], conf["invoice_date"] = iso, 0.55 if amb else 0.9
                break

    # supplier / customer
    sup = _label_value(lines, SUPPLIER_LABELS)
    if sup:
        inv["supplier_name"], conf["supplier_name"] = clean_str(sup), 0.85
    else:
        for ln in lines[:6]:
            s = ln.strip()
            if GENERIC_TOP.match(s) or len(s) < 3 or re.search(r"\d{3,}", s) or ":" in s:
                continue
            # position-based guess: trusted only when it looks like a company name
            looks_company = bool(COMPANY_RE.search(s))
            inv["supplier_name"], conf["supplier_name"] = clean_str(s), 0.7 if looks_company else 0.5
            break
    cus = _label_value(lines, CUSTOMER_LABELS)
    if cus:
        inv["customer_name"], conf["customer_name"] = clean_str(cus), 0.85

    # totals
    sub = tax = tot = None
    for ln in lines:
        low = re.sub(r"[^a-z ]", "", ln.lower()).strip()
        if re.match(r"(sub\s*total|net amount|total before tax)", low):
            v = _last_money(ln)
            if v is not None and sub is None:
                sub = v
        elif re.match(r"(sales tax|tax|vat|gst|hst)\b", low) and "total" not in low:
            v = _last_money(ln)
            if v is not None and tax is None:
                tax = v
        elif re.match(r"(grand total|total due|amount due|total amount|balance due|total)\b", low):
            v = _last_money(ln)
            if v is not None:
                tot = v  # last 'total' wins (grand total is at the bottom)
    for name, v in (("subtotal", sub), ("tax", tax), ("total", tot)):
        if v is not None:
            inv[name], conf[name] = v, 0.9

    # currency
    code, c = detect_currency(text)
    if code:
        inv["currency"], conf["currency"] = code, c

    # line items
    items, in_items = [], False
    for ln in lines:
        s = ln.strip()
        if re.search(r"(?i)\bdescription\b", s) and re.search(r"(?i)\b(qty|quantity)\b", s):
            in_items = True
            continue
        if in_items and STOP_LINE.match(s):
            in_items = False
            break
        if in_items or not items:
            m = LINE_RE.match(s)
            if m and not STOP_LINE.match(s) and (in_items or True):
                qty, unit, amt = parse_number(m["qty"]), parse_money(m["unit"]), parse_money(m["amt"])
                if qty is not None and amt is not None:
                    items.append({"description": clean_str(m["desc"]), "quantity": qty,
                                  "unit_price": unit, "amount": amt})
    inv["line_items"] = items
    conf["line_items"] = 0.85 if items else 0.0
    return inv, conf
