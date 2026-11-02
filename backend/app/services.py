"""Document processing orchestration (runs inside background workers)."""
import logging
import os
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import delete

from .config import settings
from .db import session_scope
from .extraction.llm import extract_text
from .extraction.normalize import clean_str, norm_key, parse_date
from .extraction.ocr_confidence import apply_ocr_word_confidence
from .extraction.segment import segment_pages
from .models import Chunk, Correction, Document, Extraction, ProcessingEvent, ValidationIssue
from .pipeline.image import parse_image
from .pipeline.pdf import CorruptFileError, parse_pdf
from .pipeline.spreadsheet import parse_spreadsheet
from .search import index_document
from .validation import Issue, duplicate_issues, needs_review, propagate_duplicates, validate_invoice

log = logging.getLogger(__name__)
_validation_lock = threading.Lock()  # serialises validate+commit so duplicate detection is race-free (single node)


def _public_error(msg: str) -> str:
    """Never leak server paths to API clients."""
    import re
    from pathlib import Path
    for base in {str(settings.storage_dir), str(Path(settings.storage_dir).resolve())}:
        msg = re.sub(re.escape(base) + r"[^\s'\"]*", "<file>", msg)
    return re.sub(r"(?:/[\w.\-]+){2,}", "<path>", msg)


def _event(s, doc_id, stage, status, msg=None, ms=None):
    s.add(ProcessingEvent(document_id=doc_id, stage=stage, status=status, message=msg, duration_ms=ms))


def _set(doc_id, **kw):
    with session_scope() as s:
        s.query(Document).filter(Document.id == doc_id).update(kw)


def _denormalise(ex: Extraction):
    d = ex.data
    ex.invoice_number = clean_str(d.get("invoice_number"))
    ex.invoice_number_norm = norm_key(d.get("invoice_number"))
    ex.supplier_name = clean_str(d.get("supplier_name"))
    ex.supplier_norm = norm_key(d.get("supplier_name"))
    ex.customer_name = clean_str(d.get("customer_name"))
    ex.invoice_date = parse_date(d.get("invoice_date"))[0] if d.get("invoice_date") else None
    t = d.get("total")
    ex.total = float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None
    ex.currency = d.get("currency")


def revalidate(s, doc: Document, ex: Extraction):
    """Replace validation issues of one extraction (used after processing and after manual corrections)."""
    s.execute(delete(ValidationIssue).where(ValidationIssue.extraction_id == ex.id))
    issues = validate_invoice(ex.data, ex.field_confidence, doc.ocr_confidence if doc.extraction_method != "text_layer" else None)
    issues += duplicate_issues(s, ex, doc)
    for i in issues:
        s.add(ValidationIssue(document_id=doc.id, extraction_id=ex.id, code=i.code, severity=i.severity,
                              field=i.field, message=i.message))
    ex.needs_review = needs_review(issues) and not ex.reviewed
    return issues


def refresh_document_flags(s, doc: Document):
    s.flush()
    flag = any(e.needs_review for e in doc.extractions) or any(i.extraction_id is None and i.severity == "error" for i in doc.issues)
    doc.needs_review = flag
    if doc.status in ("completed", "needs_review"):
        doc.status = "needs_review" if flag else "completed"


def _parse(doc: Document, progress):
    if doc.file_type == "pdf":
        return parse_pdf(doc.stored_path, progress)
    if doc.file_type == "image":
        return parse_image(doc.stored_path, progress)
    if doc.file_type in ("xlsx", "xls", "csv"):
        return parse_spreadsheet(doc.stored_path, doc.file_type, progress)
    raise CorruptFileError(f"Unsupported file type {doc.file_type}")


def process_document(doc_id: str):
    t0 = time.perf_counter()
    stage = "start"
    try:
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            if doc is None:
                return
            if not os.path.exists(doc.stored_path):
                raise CorruptFileError("Stored file is missing")
            doc.status, doc.stage, doc.progress, doc.error_message = "processing", "extracting_text", 0.02, None
            doc.attempts += 1
            s.execute(delete(Extraction).where(Extraction.document_id == doc_id))
            s.execute(delete(ValidationIssue).where(ValidationIssue.document_id == doc_id))
            s.execute(delete(Chunk).where(Chunk.document_id == doc_id))
            _event(s, doc_id, "processing", "started", f"attempt {doc.attempts}")
            ftype, path = doc.file_type, doc.stored_path

        # ---- stage 1: text extraction / OCR / spreadsheet parsing
        stage, t = "extracting_text", time.perf_counter()
        last = [0.0]

        def progress(f):
            if f - last[0] >= 0.05 or f >= 1:
                last[0] = f
                _set(doc_id, progress=round(0.02 + 0.58 * f, 3))

        with session_scope() as s:
            doc = s.get(Document, doc_id)
            parsed = _parse(doc, progress)
        text = parsed.text
        if not text.strip() and not parsed.invoices:
            raise CorruptFileError("No text could be extracted (blank or unreadable document)")
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            doc.extracted_text = text
            doc.page_count = len(parsed.pages)
            doc.extraction_method = parsed.method
            doc.ocr_confidence = parsed.ocr_confidence
            doc.stage, doc.progress = "extracting_fields", 0.6
            _event(s, doc_id, "extracting_text", "ok",
                   "; ".join(parsed.warnings[:5]) or f"{len(parsed.pages)} page(s) via {parsed.method}",
                   int((time.perf_counter() - t) * 1000))

        # ---- stage 2: structured extraction
        stage, t = "extracting_fields", time.perf_counter()
        results = []  # (data, conf, method, source_ref, notes)
        if parsed.invoices:
            for inv in parsed.invoices:
                results.append((inv["data"], inv["field_confidence"], "table", inv["source_ref"], []))
        else:
            segs = segment_pages(parsed.pages) or [{"text": text, "pages": (1, max(len(parsed.pages), 1))}]
            for k, seg in enumerate(segs):
                data, conf, method, notes = extract_text(seg["text"])
                a, b = seg["pages"]
                words = [w for p in parsed.pages if a <= p.page_no <= b for w in p.words]
                notes = notes + apply_ocr_word_confidence(data, conf, words, settings.ocr_field_min_confidence)
                results.append((data, conf, method, f"page {a}" if a == b else f"pages {a}-{b}", notes))
                if k % 10 == 0:
                    _set(doc_id, progress=round(0.6 + 0.3 * (k + 1) / len(segs), 3))

        # ---- stage 3: validation (+ duplicates) under lock
        stage = "validating"
        with _validation_lock, session_scope() as s:
            doc = s.get(Document, doc_id)
            doc.stage, doc.progress = "validating", 0.92
            for seq, (data, conf, method, ref, notes) in enumerate(results):
                ex = Extraction(document_id=doc_id, seq=seq, source_ref=ref, data=data, field_confidence=conf,
                                method=method)
                _denormalise(ex)
                s.add(ex)
                s.flush()
                issues = revalidate(s, doc, ex)
                s.flush()
                for odoc in propagate_duplicates(s, ex, doc):
                    s.flush()
                    s.refresh(odoc, ["extractions", "issues"])
                    refresh_document_flags(s, odoc)
                for n in notes:
                    s.add(ValidationIssue(document_id=doc_id, extraction_id=ex.id, code="EXTRACTION_NOTE",
                                          severity="warning", field=None, message=n))
                if any(n for n in notes if "not found" in n or "dropped" in n or "discarded" in n):
                    ex.needs_review = True
            s.flush()
            s.refresh(doc, ["extractions"])
            doc.status = "completed"
            refresh_document_flags(s, doc)

        # ---- stage 4: indexing
        stage, t = "indexing", time.perf_counter()
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            doc.stage, doc.progress = "indexing", 0.96
            s.refresh(doc, ["extractions"])
            index_document(s, doc, text)
            ms = int((time.perf_counter() - t0) * 1000)
            doc.stage, doc.progress, doc.processing_ms = "done", 1.0, ms
            _event(s, doc_id, "processing", "ok", f"{len(results)} invoice(s); status={doc.status}", ms)
    except Exception as e:  # noqa: BLE001 - every failure must end in a recorded 'failed' state
        if isinstance(e, CorruptFileError):
            log.warning("processing %s failed at %s: %s", doc_id, stage, e)
        else:
            log.exception("processing %s failed at %s", doc_id, stage)
        msg = _public_error(str(e) if isinstance(e, CorruptFileError) else f"{type(e).__name__}: {e}")
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            if doc:
                doc.status, doc.stage, doc.error_message = "failed", f"failed:{stage}", msg[:2000]
                doc.processing_ms = int((time.perf_counter() - t0) * 1000)
                _event(s, doc_id, stage, "error", msg[:1000])
