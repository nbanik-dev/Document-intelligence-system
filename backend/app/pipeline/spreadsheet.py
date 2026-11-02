"""CSV / XLSX / XLS parsing. Supports (a) tabular sheets (one row per invoice or per line item) and
(b) form-like sheets (label/value cells + a line-item table). Anything unrecognised falls back to the
text-extraction path so nothing is silently dropped."""
import csv
import io
import logging
import re

import pandas as pd

from ..extraction.normalize import (clean_str, detect_currency, new_invoice, parse_date, parse_money,
                                    parse_number)
from .pdf import CorruptFileError
from .types import PageText, ParsedDocument

log = logging.getLogger(__name__)
MAX_TEXT_CHARS = 1_000_000


def _n(v) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v).lower()) if v is not None and str(v) != "nan" else ""


ALIASES = {
    "invoice_number": {"invoicenumber", "invoiceno", "invoice", "invno", "invoiceid", "invnumber", "invoicenum",
                       "invoicenr", "billnumber", "billno", "documentnumber"},
    "invoice_date": {"invoicedate", "date", "issuedate", "dateofissue", "billdate", "dateissued"},
    "supplier_name": {"supplier", "suppliername", "vendor", "vendorname", "seller", "sellername", "from",
                      "issuedby", "company"},
    "customer_name": {"customer", "customername", "billto", "billedto", "client", "clientname", "buyer",
                      "soldto", "buyername"},
    "currency": {"currency", "currencycode", "ccy"},
    "description": {"description", "item", "itemdescription", "product", "productdescription", "details",
                    "service", "particulars", "itemname"},
    "quantity": {"quantity", "qty", "units", "count"},
    "unit_price": {"unitprice", "price", "rate", "unitcost", "priceperunit", "unitrate", "cost"},
    "amount": {"amount", "lineamount", "linetotal", "itemtotal", "extendedprice", "lineprice", "amountusd"},
}
TOTAL_LABELS = {
    "subtotal": ("subtotal", "netamount", "netsubtotal", "totalbeforetax", "amountbeforetax"),
    "tax": ("tax", "vat", "gst", "salestax", "taxamount", "hst", "iva"),
    "total": ("total", "grandtotal", "totaldue", "amountdue", "totalamount", "invoicetotal", "balancedue"),
}
DOC_FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "currency"]
TOTAL_COL_FIELDS = {"subtotal": "subtotal", "tax": "tax", "total": "total"}


def _col_field(cell) -> str | None:
    k = _n(cell)
    if not k:
        return None
    for f, names in ALIASES.items():
        if k in names:
            return f
    for f, names in TOTAL_LABELS.items():
        if k in names:
            return f
    if k.startswith("tax") or k.startswith("vat"):
        return "tax"
    return None


def _total_label(cell) -> str | None:
    k = _n(cell)
    if not k:
        return None
    if k.startswith("subtotal") or k in TOTAL_LABELS["subtotal"]:
        return "subtotal"
    if k in TOTAL_LABELS["total"] or (k.startswith("total") and not k.startswith("totalbefore")) \
            or k.startswith("grandtotal") or k.startswith("amountdue") or k.startswith("balancedue"):
        return "total"
    if k.startswith(("tax", "vat", "gst", "salestax", "hst")) or k in TOTAL_LABELS["tax"]:
        return "tax"
    return None


# ---------------------------------------------------------------- reading
def read_sheets(path: str, file_type: str) -> dict:
    try:
        if file_type == "csv":
            raw = open(path, "rb").read()
            if not raw.strip():
                raise CorruptFileError("CSV file is empty")
            for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
                try:
                    txt = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            try:
                dialect = csv.Sniffer().sniff(txt[:8192], delimiters=",;\t|")
                sep = dialect.delimiter
            except csv.Error:
                sep = ","
            df = pd.read_csv(io.StringIO(txt), sep=sep, header=None, dtype=object, keep_default_na=False,
                             engine="python", on_bad_lines="skip", skip_blank_lines=True)
            return {"csv": df}
        engine = "openpyxl" if file_type == "xlsx" else "xlrd"
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object, engine=engine)
        return {name: df for name, df in sheets.items()}
    except CorruptFileError:
        raise
    except pd.errors.EmptyDataError as e:
        raise CorruptFileError("Spreadsheet is empty") from e
    except Exception as e:  # noqa: BLE001
        raise CorruptFileError(f"Cannot read {file_type.upper()} file: {e}") from e


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = df.map(lambda v: None if (v is None or (isinstance(v, float) and v != v) or (isinstance(v, str) and not v.strip())) else v)
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df = df.reset_index(drop=False)  # 'index' column keeps original row number
    return df.astype(object).where(df.notna(), None)


def _render(name: str, df: pd.DataFrame) -> str:
    lines = [f"[sheet: {name}]"]
    for _, row in df.iterrows():
        cells = [str(v).strip() for v in row.tolist()[1:] if v is not None]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


# ---------------------------------------------------------------- structure detection
def _header_row(df: pd.DataFrame):
    """Find the best 'tabular' header row -> (row_pos, {field: col_pos}) or None."""
    best = None
    for pos in range(min(len(df), 200)):
        row = df.iloc[pos].tolist()
        mapping = {}
        for ci, cell in enumerate(row[1:], start=1):
            f = _col_field(cell)
            if f and f not in mapping:
                mapping[f] = ci
        doc_level = [f for f in mapping if f in DOC_FIELDS or f in TOTAL_COL_FIELDS]
        line_level = [f for f in mapping if f in ("description", "quantity", "unit_price", "amount")]
        score = len(doc_level) + len(line_level)
        if score >= 3 and (best is None or score > best[0]):
            best = (score, pos, mapping, len(doc_level), len(line_level))
        if best and best[0] >= 5:
            break
    return best


def _build_line(row, mapping):
    desc = clean_str(row[mapping["description"]]) if "description" in mapping else None
    qty = parse_number(row[mapping["quantity"]]) if "quantity" in mapping else None
    unit = parse_money(row[mapping["unit_price"]]) if "unit_price" in mapping else None
    amt = parse_money(row[mapping["amount"]]) if "amount" in mapping else None
    if desc is None and qty is None and unit is None and amt is None:
        return None
    return {"description": desc, "quantity": qty, "unit_price": unit, "amount": amt}


CONF_TABLE = 0.95


def _tabular(df, pos, mapping, sheet) -> list:
    invs, groups, order = [], {}, []
    rows = df.iloc[pos + 1:]
    has_lines = any(f in mapping for f in ("description", "quantity", "unit_price"))
    for _, r in rows.iterrows():
        row = r.tolist()
        if all(v is None for v in row[1:]):
            continue
        first_cell = next((v for v in row[1:] if v is not None), None)
        if _total_label(first_cell) and "invoice_number" not in mapping and not has_lines:
            continue
        key = _n(row[mapping["invoice_number"]]) if "invoice_number" in mapping and row[mapping["invoice_number"]] is not None else None
        if key is None and "invoice_number" in mapping and has_lines:
            key = order[-1] if order else "__none__"  # continuation row of previous invoice
        if key is None:
            key = f"__row{row[0]}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    for key in order:
        grp = groups[key]
        inv = new_invoice()
        conf = {}
        for f in ("invoice_number", "supplier_name", "customer_name", "currency"):
            if f in mapping:
                val = next((clean_str(g[mapping[f]]) for g in grp if g[mapping[f]] is not None), None)
                inv[f] = val.upper() if (f == "currency" and val) else val
                conf[f] = CONF_TABLE if val else 0.0
        if "invoice_date" in mapping:
            for g in grp:
                if g[mapping["invoice_date"]] is not None:
                    iso, amb = parse_date(g[mapping["invoice_date"]])
                    inv["invoice_date"], conf["invoice_date"] = iso, (0.6 if amb else CONF_TABLE) if iso else 0.0
                    break
        for f in ("subtotal", "tax", "total"):
            if f in mapping:
                vals = [parse_money(g[mapping[f]]) for g in grp if g[mapping[f]] is not None]
                vals = [v for v in vals if v is not None]
                inv[f] = vals[-1] if vals else None
                conf[f] = CONF_TABLE if vals else 0.0
        if has_lines:
            inv["line_items"] = [li for li in (_build_line(g, mapping) for g in grp) if li]
            conf["line_items"] = CONF_TABLE
        if inv["currency"] is None:
            code, c = detect_currency(" ".join(str(v) for g in grp for v in g[1:] if v is not None))
            inv["currency"], conf["currency"] = code, c
        first, last = grp[0][0] + 1, grp[-1][0] + 1  # Excel-style 1-based row numbers
        invs.append({"data": inv, "field_confidence": conf, "source_ref": f"{sheet}!rows {first}-{last}",
                     "method": "table"})
    return invs


def _form_like(df, sheet) -> list:
    inv, conf = new_invoice(), {}
    cells = [(pos, ci, v) for pos in range(len(df)) for ci, v in enumerate(df.iloc[pos].tolist()) if ci >= 1 and v is not None]
    rows = {pos: df.iloc[pos].tolist() for pos in range(len(df))}

    def value_after(pos, ci):
        row = rows[pos]
        for cj in range(ci + 1, min(ci + 4, len(row))):
            if row[cj] is not None:
                return row[cj]
        if pos + 1 < len(df) and rows[pos + 1][ci] is not None:
            return rows[pos + 1][ci]
        return None

    for pos, ci, v in cells:
        text = str(v)
        label_txt, inline_val = text, None
        if ":" in text and len(text) < 120:
            label_txt, _, rest = text.partition(":")
            inline_val = rest.strip() or None
        f = _col_field(label_txt)
        if f in DOC_FIELDS and inv.get(f) is None:
            val = inline_val if inline_val is not None else value_after(pos, ci)
            if val is None or _col_field(val) in DOC_FIELDS:
                continue
            if f == "invoice_date":
                iso, amb = parse_date(val)
                if iso:
                    inv[f], conf[f] = iso, 0.6 if amb else CONF_TABLE
            else:
                s = clean_str(val)
                if s:
                    inv[f] = s.upper() if f == "currency" else s
                    conf[f] = CONF_TABLE
    # totals + line items
    hdr = None
    for pos in range(len(df)):
        m = {}
        for ci, cell in enumerate(rows[pos][1:], start=1):
            ff = _col_field(cell)
            if ff in ("description", "quantity", "unit_price", "amount") and ff not in m:
                m[ff] = ci
        if "description" in m and len(m) >= 3:
            hdr = (pos, m)
            break
    stop_pos = len(df)
    if hdr:
        pos0, m = hdr
        for pos in range(pos0 + 1, len(df)):
            row = rows[pos]
            first = next((v for v in row[1:] if v is not None), None)
            if first is None or _total_label(first):
                stop_pos = pos
                break
            li = _build_line(row, m)
            if li:
                inv["line_items"].append(li)
        conf["line_items"] = CONF_TABLE
    for pos in range(stop_pos if hdr else 0, len(df)):
        row = rows[pos]
        for ci, v in enumerate(row[1:], start=1):
            tl = _total_label(str(v).split(":")[0]) if v is not None and not isinstance(v, (int, float)) else None
            if tl and inv[tl] is None:
                nums = [parse_money(x) for x in row[ci + 1:] if x is not None]
                if ":" in str(v) and str(v).partition(":")[2].strip():
                    nums = [parse_money(str(v).partition(":")[2])] + nums
                nums = [x for x in nums if x is not None]
                if nums:
                    inv[tl], conf[tl] = nums[-1], CONF_TABLE
                break
    if inv["currency"] is None:
        code, c = detect_currency(" ".join(str(v) for _, _, v in cells))
        inv["currency"], conf["currency"] = code, c
    found = sum(1 for k in ("invoice_number", "invoice_date", "supplier_name", "total") if inv[k] is not None)
    if found < 2 and not inv["line_items"]:
        return []
    return [{"data": inv, "field_confidence": conf, "source_ref": f"{sheet}", "method": "table"}]


def parse_spreadsheet(path: str, file_type: str, progress=None) -> ParsedDocument:
    sheets = read_sheets(path, file_type)
    out = ParsedDocument(kind="table", invoices=[])
    total_chars, n = 0, max(len(sheets), 1)
    for i, (name, raw) in enumerate(sheets.items()):
        df = _clean(raw)
        if df.shape[0] == 0 or df.shape[1] <= 1:
            out.warnings.append(f"sheet '{name}' is empty - skipped")
            continue
        text = _render(name, df)
        if total_chars + len(text) > MAX_TEXT_CHARS:
            text = text[: max(0, MAX_TEXT_CHARS - total_chars)]
            out.warnings.append(f"sheet '{name}': text truncated for storage")
        total_chars += len(text)
        out.pages.append(PageText(i + 1, text, "table"))
        try:
            hdr = _header_row(df)
            found = []
            if hdr and hdr[3] >= 2:  # >=2 document-level columns => tabular layout
                found = _tabular(df, hdr[1], hdr[2], name)
            if not found:
                found = _form_like(df, name)
            if not found and hdr:
                found = _tabular(df, hdr[1], hdr[2], name)
            if not found:
                out.warnings.append(f"sheet '{name}': no invoice structure recognised")
            out.invoices.extend(found)
        except Exception as e:  # noqa: BLE001
            out.warnings.append(f"sheet '{name}': structure parsing failed ({e})")
        if progress:
            progress((i + 1) / n)
    if not out.pages:
        raise CorruptFileError("Spreadsheet contains no data")
    if not out.invoices:
        out.invoices = None  # fall back to text extraction
    return out
