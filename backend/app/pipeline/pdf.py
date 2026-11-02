"""PDF parsing: text layer first, OCR only for pages with (almost) no text. Streams page-by-page so a
1000-page file never holds more than one rendered page in memory."""
import logging
from typing import Callable

import pymupdf
from PIL import Image

from ..config import settings
from .ocr import ocr_image
from .types import PageText, ParsedDocument

log = logging.getLogger(__name__)


class CorruptFileError(Exception):
    pass


def page_rows_text(page) -> str:
    """Rebuild visual rows from word boxes so table cells drawn separately stay on one line."""
    words = page.get_text("words")
    if not words:
        return ""
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows, cur, cur_y, cur_h = [], [], None, 0.0
    for w in words:
        yc, h = (w[1] + w[3]) / 2, max(w[3] - w[1], 1.0)
        if cur and abs(yc - cur_y) > 0.55 * max(h, cur_h):
            rows.append(cur)
            cur = []
        if not cur:
            cur_y, cur_h = yc, h
        cur.append(w)
    rows.append(cur)
    out = []
    for r in rows:
        r.sort(key=lambda w: w[0])
        line, last_x1 = "", None
        for w in r:
            if last_x1 is not None:
                line += "  " if w[0] - last_x1 > 12 else " "
            line += w[4]
            last_x1 = w[2]
        out.append(line)
    return "\n".join(out)


def parse_pdf(path: str, progress: Callable[[float], None] | None = None) -> ParsedDocument:
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # noqa: BLE001
        raise CorruptFileError(f"Cannot open PDF: {e}") from e
    with doc:
        if doc.needs_pass:
            raise CorruptFileError("PDF is password protected")
        if doc.page_count == 0:
            raise CorruptFileError("PDF has no pages")
        out = ParsedDocument(kind="pdf")
        n = doc.page_count
        failed = 0
        for i in range(n):
            try:
                page = doc[i]
                text = page_rows_text(page).strip()
                if len(text) >= settings.pdf_min_text_chars:
                    out.pages.append(PageText(i + 1, text, "text_layer"))
                else:
                    pix = page.get_pixmap(dpi=settings.ocr_dpi)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    otext, conf, words = ocr_image(img)
                    # keep any (short) text layer content if OCR found nothing
                    out.pages.append(PageText(i + 1, otext or text, "ocr", conf, words=words))
                    if conf and conf < settings.ocr_min_confidence:
                        out.warnings.append(f"page {i + 1}: low OCR confidence ({conf:.0f})")
            except Exception as e:  # noqa: BLE001  one bad page must not kill the document
                failed += 1
                out.pages.append(PageText(i + 1, "", "error", error=str(e)))
                out.warnings.append(f"page {i + 1}: {e}")
            if progress and (i % 5 == 0 or i == n - 1):
                progress((i + 1) / n)
        if failed == n:
            raise CorruptFileError("No page could be read (file is likely corrupted)")
        out.pages = [p for p in out.pages if p.method != "error" or p.error]
        return out
