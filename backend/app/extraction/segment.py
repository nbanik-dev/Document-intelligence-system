"""Split a multi-invoice document (e.g. a 1000-page PDF) into per-invoice page groups."""
import re

from .heuristic import INV_NO_RE, INV_NO_RE2
from .normalize import norm_key


def page_invoice_number(text: str) -> str | None:
    for ln in text.splitlines():
        m = INV_NO_RE.search(ln) or INV_NO_RE2.search(ln)
        if m:
            return m.group(1).strip(".,;")
    return None


def segment_pages(pages) -> list[dict]:
    """pages: list[PageText]. A new invoice starts on a page whose invoice number differs from the current one.
    Pages without an invoice number are treated as continuations. Returns [{'text','pages':(first,last)}]."""
    segs: list[dict] = []
    cur_key = None
    for p in pages:
        if not p.text or not p.text.strip():
            continue
        key = norm_key(page_invoice_number(p.text))
        if segs and (key is None or key == cur_key):
            segs[-1]["text"] += "\n" + p.text
            segs[-1]["pages"] = (segs[-1]["pages"][0], p.page_no)
            continue
        segs.append({"text": p.text, "pages": (p.page_no, p.page_no)})
        cur_key = key
    return segs
