"""REST API."""
import hashlib
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import jobs
from .config import settings
from .db import get_session, init_db
from .extraction.normalize import parse_date, parse_money, parse_number
from .models import Correction, Document, Extraction
from .search import hybrid_search, keyword_search, semantic_search, snippet
from .services import _denormalise, refresh_document_flags, revalidate

log = logging.getLogger("docintel")

EXT_TYPES = {"pdf": "pdf", "jpg": "image", "jpeg": "image", "png": "image", "tif": "image", "tiff": "image",
             "xlsx": "xlsx", "xls": "xls", "csv": "csv"}


def detect_type(head: bytes, filename: str) -> str | None:
    """Trust magic bytes over the extension (a renamed .exe must not get through)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")):
        return "image"
    if head.startswith(b"PK\x03\x04") and ext == "xlsx":
        return "xlsx"
    if head.startswith(b"\xd0\xcf\x11\xe0") and ext == "xls":
        return "xls"
    if ext == "csv" and (b"\x00" not in head[:2048] or head[:2] in (b"\xff\xfe", b"\xfe\xff")):
        return "csv"  # plain text (or UTF-16 with BOM)
    if ext in ("pdf", "png", "jpg", "jpeg", "tif", "tiff", "xlsx", "xls") and len(head) < 8:
        return EXT_TYPES[ext]  # tiny/empty file: let the pipeline report it as corrupted
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    jobs.start()
    n = jobs.recover_pending()
    if n:
        log.info("re-queued %d unfinished documents", n)
    yield
    jobs.shutdown()


app = FastAPI(title="Document Intelligence API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])


def auth(x_api_key: str | None = Header(default=None)):
    if settings.api_key and not (x_api_key and secrets.compare_digest(x_api_key, settings.api_key)):
        raise HTTPException(401, "Invalid or missing X-API-Key")


# ------------------------------------------------------------------ serialisation
def doc_summary(d: Document) -> dict:
    ex = d.extractions[0] if d.extractions else None
    return {
        "id": d.id, "filename": d.filename, "file_type": d.file_type, "size_bytes": d.size_bytes,
        "status": d.status, "stage": d.stage, "progress": d.progress, "error_message": d.error_message,
        "needs_review": d.needs_review, "page_count": d.page_count, "extraction_method": d.extraction_method,
        "ocr_confidence": d.ocr_confidence, "attempts": d.attempts, "processing_ms": d.processing_ms,
        "invoice_count": len(d.extractions),
        "invoice_number": ex.invoice_number if ex else None, "supplier_name": ex.supplier_name if ex else None,
        "total": ex.total if ex else None, "currency": ex.currency if ex else None,
        "issue_count": len(d.issues), "created_at": d.created_at, "updated_at": d.updated_at,
    }


def extraction_out(e: Extraction) -> dict:
    return {"id": e.id, "seq": e.seq, "source_ref": e.source_ref, "method": e.method, "data": e.data,
            "field_confidence": e.field_confidence, "needs_review": e.needs_review, "reviewed": e.reviewed,
            "manually_corrected": e.manually_corrected,
            "issues": [{"id": i.id, "code": i.code, "severity": i.severity, "field": i.field, "message": i.message}
                       for i in e.issues]}


def doc_detail(d: Document, with_text=False) -> dict:
    out = doc_summary(d)
    out["extractions"] = [extraction_out(e) for e in d.extractions]
    out["document_issues"] = [{"id": i.id, "code": i.code, "severity": i.severity, "message": i.message}
                              for i in d.issues if i.extraction_id is None]
    out["events"] = [{"stage": e.stage, "status": e.status, "message": e.message, "duration_ms": e.duration_ms,
                      "created_at": e.created_at} for e in d.events]
    if with_text:
        out["extracted_text"] = d.extracted_text
    return out


def _get_doc(s: Session, doc_id: str) -> Document:
    d = s.get(Document, doc_id)
    if not d:
        raise HTTPException(404, "Document not found")
    return d


# ------------------------------------------------------------------ endpoints
@app.get("/health")
def health(s: Session = Depends(get_session)):
    s.execute(select(1))
    return {"status": "ok"}


@app.post("/api/documents", status_code=202, dependencies=[Depends(auth)])
async def upload(files: list[UploadFile] = File(...), s: Session = Depends(get_session)):
    """Accepts many files. Returns per-file result; processing happens in the background."""
    if len(files) > settings.max_files_per_request:
        raise HTTPException(413, f"Max {settings.max_files_per_request} files per request")
    results, limit = [], settings.max_upload_mb * 1024 * 1024
    for f in files:
        name = os.path.basename(f.filename or "upload")[:255]
        name = re.sub(r"[^\w.\- ()]", "_", name)
        doc_id = uuid.uuid4().hex
        path = settings.storage_dir / f"{doc_id}.bin"
        h, size, head = hashlib.sha256(), 0, b""
        try:
            with open(path, "wb") as out:
                while chunk := await f.read(1024 * 1024):
                    if not head:
                        head = chunk[:4096]
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")
                    h.update(chunk)
                    out.write(chunk)
        except HTTPException as e:
            path.unlink(missing_ok=True)
            results.append({"filename": name, "error": e.detail, "status": "rejected"})
            continue
        ftype = detect_type(head, name)
        if ftype is None:
            path.unlink(missing_ok=True)
            results.append({"filename": name, "status": "rejected",
                            "error": "Unsupported or mismatching file type (allowed: PDF, JPG, PNG, TIFF, XLSX, XLS, CSV)"})
            continue
        doc = Document(id=doc_id, filename=name, stored_path=str(path), file_type=ftype, size_bytes=size,
                       sha256=h.hexdigest(), status="queued", stage="queued")
        s.add(doc)
        s.commit()
        jobs.enqueue(doc.id)
        results.append({"id": doc.id, "filename": name, "status": "queued"})
    return {"documents": results}


@app.get("/api/documents", dependencies=[Depends(auth)])
def list_documents(status: str | None = None, file_type: str | None = None, needs_review: bool | None = None,
                   q: str | None = None, supplier: str | None = None, customer: str | None = None,
                   invoice_number: str | None = None, currency: str | None = None,
                   date_from: str | None = None, date_to: str | None = None,
                   min_total: float | None = None, max_total: float | None = None,
                   limit: int = Query(50, le=200), offset: int = 0, s: Session = Depends(get_session)):
    docs, total = keyword_search(s, q, limit=limit, offset=offset, status=status, file_type=file_type,
                                 needs_review=needs_review, supplier=supplier, customer=customer,
                                 invoice_number=invoice_number, currency=currency, date_from=date_from,
                                 date_to=date_to, min_total=min_total, max_total=max_total)
    return {"total": total, "items": [doc_summary(d) for d in docs]}


@app.get("/api/documents/{doc_id}", dependencies=[Depends(auth)])
def get_document(doc_id: str, s: Session = Depends(get_session)):
    return doc_detail(_get_doc(s, doc_id))


@app.get("/api/documents/{doc_id}/status", dependencies=[Depends(auth)])
def get_status(doc_id: str, s: Session = Depends(get_session)):
    d = _get_doc(s, doc_id)
    return {"id": d.id, "status": d.status, "stage": d.stage, "progress": d.progress,
            "error_message": d.error_message, "attempts": d.attempts}


@app.get("/api/documents/{doc_id}/results", dependencies=[Depends(auth)])
def get_results(doc_id: str, s: Session = Depends(get_session)):
    d = _get_doc(s, doc_id)
    if d.status in ("queued", "processing"):
        raise HTTPException(409, f"Document is still {d.status}")
    return {"id": d.id, "status": d.status, "needs_review": d.needs_review,
            "invoices": [extraction_out(e) for e in d.extractions], "error_message": d.error_message}


@app.get("/api/documents/{doc_id}/text", dependencies=[Depends(auth)])
def get_text(doc_id: str, s: Session = Depends(get_session)):
    d = _get_doc(s, doc_id)
    return {"id": d.id, "extracted_text": d.extracted_text or ""}


class CorrectionIn(BaseModel):
    fields: dict[str, Any] = Field(..., description="field -> new value (line_items: full replacement list)")
    reviewer: str | None = None


MONEY_FIELDS = {"subtotal", "tax", "total"}
TEXT_FIELDS = {"invoice_number", "supplier_name", "customer_name", "currency"}


def _coerce_field(name: str, v: Any):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if name in MONEY_FIELDS:
        n = parse_money(v)
        if n is None:
            raise HTTPException(422, f"'{name}' must be a number")
        return n
    if name == "invoice_date":
        iso, _ = parse_date(v)
        if not iso:
            raise HTTPException(422, "'invoice_date' must be a valid date (YYYY-MM-DD)")
        return iso
    if name == "currency":
        return str(v).strip().upper()[:8]
    if name in TEXT_FIELDS:
        return str(v).strip()[:256]
    if name == "line_items":
        if not isinstance(v, list):
            raise HTTPException(422, "'line_items' must be a list")
        out = []
        for li in v:
            if not isinstance(li, dict):
                raise HTTPException(422, "line item must be an object")
            out.append({"description": (str(li.get("description")).strip() if li.get("description") not in (None, "") else None),
                        "quantity": parse_number(li.get("quantity")), "unit_price": parse_money(li.get("unit_price")),
                        "amount": parse_money(li.get("amount"))})
        return out
    raise HTTPException(422, f"Unknown field '{name}'")


@app.patch("/api/extractions/{ext_id}", dependencies=[Depends(auth)])
def correct(ext_id: str, body: CorrectionIn, s: Session = Depends(get_session)):
    ex = s.get(Extraction, ext_id)
    if not ex:
        raise HTTPException(404, "Extraction not found")
    doc = s.get(Document, ex.document_id)
    data, conf = dict(ex.data), dict(ex.field_confidence or {})
    for name, raw in body.fields.items():
        new = _coerce_field(name, raw)
        if data.get(name) != new:
            s.add(Correction(extraction_id=ex.id, field=name, old_value={"v": data.get(name)},
                             new_value={"v": new}, reviewer=body.reviewer))
        data[name] = new
        conf[name] = 1.0  # human-verified
    ex.data, ex.field_confidence, ex.manually_corrected = data, conf, True
    ex.reviewed = False
    _denormalise(ex)
    s.flush()
    from .services import _validation_lock
    with _validation_lock:
        revalidate(s, doc, ex)
        s.flush()
        s.refresh(doc, ["extractions", "issues"])
        refresh_document_flags(s, doc)
        s.commit()
    return extraction_out(ex)


@app.post("/api/extractions/{ext_id}/approve", dependencies=[Depends(auth)])
def approve(ext_id: str, reviewer: str | None = None, s: Session = Depends(get_session)):
    """Human sign-off: keeps the validation issues on record but removes the review flag."""
    ex = s.get(Extraction, ext_id)
    if not ex:
        raise HTTPException(404, "Extraction not found")
    doc = s.get(Document, ex.document_id)
    ex.reviewed, ex.needs_review = True, False
    s.flush()
    s.refresh(doc, ["extractions", "issues"])
    refresh_document_flags(s, doc)
    s.commit()
    return extraction_out(ex)


@app.post("/api/documents/{doc_id}/reprocess", status_code=202, dependencies=[Depends(auth)])
def reprocess(doc_id: str, force: bool = False, s: Session = Depends(get_session)):
    d = _get_doc(s, doc_id)
    if d.status in ("queued", "processing"):
        raise HTTPException(409, f"Document is already {d.status}")
    if d.status != "failed" and not force:
        raise HTTPException(409, "Only failed documents can be reprocessed (pass force=true to override; manual corrections will be lost)")
    d.status, d.stage, d.progress, d.error_message = "queued", "queued", 0.0, None
    s.commit()
    jobs.enqueue(d.id)
    return {"id": d.id, "status": "queued"}


@app.post("/api/documents/reprocess-failed", status_code=202, dependencies=[Depends(auth)])
def reprocess_failed(s: Session = Depends(get_session)):
    docs = s.scalars(select(Document).where(Document.status == "failed")).all()
    for d in docs:
        d.status, d.stage, d.progress, d.error_message = "queued", "queued", 0.0, None
    s.commit()
    for d in docs:
        jobs.enqueue(d.id)
    return {"requeued": [d.id for d in docs]}


@app.delete("/api/documents/{doc_id}", status_code=204, dependencies=[Depends(auth)])
def delete_document(doc_id: str, s: Session = Depends(get_session)):
    d = _get_doc(s, doc_id)
    path = d.stored_path
    s.delete(d)
    s.commit()
    try:
        os.remove(path)
    except OSError:
        pass


@app.get("/api/search", dependencies=[Depends(auth)])
def search(q: str = Query(..., min_length=1), mode: str = Query("hybrid", pattern="^(keyword|semantic|hybrid)$"),
           status: str | None = None, file_type: str | None = None, supplier: str | None = None,
           customer: str | None = None, currency: str | None = None, date_from: str | None = None,
           date_to: str | None = None, min_total: float | None = None, max_total: float | None = None,
           needs_review: bool | None = None, limit: int = Query(20, le=100), s: Session = Depends(get_session)):
    flt = dict(status=status, file_type=file_type, supplier=supplier, customer=customer, currency=currency,
               date_from=date_from, date_to=date_to, min_total=min_total, max_total=max_total, needs_review=needs_review)
    if mode == "keyword":
        docs, _ = keyword_search(s, q, limit=limit, **flt)
        rows = [(d, None, snippet(d.extracted_text, q)) for d in docs]
    elif mode == "semantic":
        rows = semantic_search(s, q, limit=limit, **flt)
    else:
        rows = hybrid_search(s, q, limit=limit, **flt)
    return {"mode": mode, "items": [{**doc_summary(d), "score": sc, "snippet": sn} for d, sc, sn in rows]}


@app.get("/api/metrics", dependencies=[Depends(auth)])
def metrics(s: Session = Depends(get_session)):
    counts = dict(s.execute(select(Document.status, func.count()).group_by(Document.status)).all())
    total = sum(counts.values())
    done = counts.get("completed", 0) + counts.get("needs_review", 0)
    finished = done + counts.get("failed", 0)
    avg_ms = s.scalar(select(func.avg(Document.processing_ms)).where(Document.processing_ms.is_not(None)))
    return {"total": total, "by_status": counts,
            "success_rate": round(done / finished, 4) if finished else None,
            "failure_rate": round(counts.get("failed", 0) / finished, 4) if finished else None,
            "avg_processing_ms": round(avg_ms) if avg_ms else None}
