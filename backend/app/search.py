"""Keyword + semantic + hybrid search.

Embeddings: `hash` provider = dependency-free signed feature-hashing of word uni/bi-grams (fast, deterministic,
lexical-semantic). Set EMBEDDING_PROVIDER=sbert to use sentence-transformers (true semantic). Vectors are stored
as JSON; for >100k chunks move them to pgvector (see docs/ARCHITECTURE.md)."""
import hashlib
import math
import re
from functools import lru_cache

import numpy as np
from sqlalchemy import and_, func, or_, select

from .config import settings
from .models import Chunk, Document, Extraction

_TOKEN = re.compile(r"[a-z0-9]+")
# tiny domain synonym map so "bill"/"vendor"/"cost" queries reach invoice vocabulary even with hash embeddings
_SYN = {"bill": "invoice", "bills": "invoice", "vendor": "supplier", "seller": "supplier", "client": "customer",
        "buyer": "customer", "cost": "total", "price": "total", "vat": "tax", "gst": "tax", "purchase": "order"}
_STOP = {"the", "a", "an", "of", "and", "to", "for", "in", "on", "at", "is", "are", "by", "with", "from"}


def _tokens(text: str) -> list[str]:
    return [_SYN.get(t, t) for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def _hash_embed(text: str) -> list[float]:
    dim = settings.embedding_dim
    v = np.zeros(dim, dtype=np.float32)
    toks = _tokens(text)
    feats = toks + [a + "_" + b for a, b in zip(toks, toks[1:])]
    for f in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
        v[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    v = np.sign(v) * np.log1p(np.abs(v))
    n = np.linalg.norm(v)
    return (v / n).tolist() if n else v.tolist()


@lru_cache(maxsize=1)
def _sbert():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")


def embed(text: str) -> list[float]:
    if settings.embedding_provider == "sbert":
        return _sbert().encode(text, normalize_embeddings=True).tolist()
    return _hash_embed(text)


def chunk_text(text: str, size: int = 900, overlap: int = 120, max_chunks: int = 400) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    out, i = [], 0
    while i < len(text) and len(out) < max_chunks:
        j = min(len(text), i + size)
        if j < len(text):  # prefer breaking at a newline/space
            k = max(text.rfind("\n", i, j), text.rfind(" ", i, j))
            j = k if k > i + size // 2 else j
        out.append(text[i:j].strip())
        i = max(j - overlap, i + 1) if j < len(text) else len(text)
    return [c for c in out if c]


def index_document(session, doc: Document, text: str):
    """One summary chunk per invoice (so supplier/number/total queries always hit, even for 1000-page files)
    plus overlapping text chunks."""
    session.query(Chunk).filter(Chunk.document_id == doc.id).delete()
    parts = []
    for e in doc.extractions[:2000]:
        parts.append(f"{doc.filename} {e.supplier_name or ''} {e.customer_name or ''} invoice {e.invoice_number or ''} "
                     f"{e.invoice_date or ''} {e.currency or ''} {e.total if e.total is not None else ''}".strip())
    if not parts:
        parts.append(doc.filename)
    parts += chunk_text(text, size=1200, overlap=120, max_chunks=2500)
    for i, p in enumerate(parts):
        session.add(Chunk(document_id=doc.id, idx=i, text=p, embedding=[round(x, 4) for x in embed(p)]))


def _filters(q, *, status=None, file_type=None, needs_review=None, supplier=None, customer=None, invoice_number=None,
             currency=None, date_from=None, date_to=None, min_total=None, max_total=None):
    if status:
        q = q.where(Document.status == status)
    if file_type:
        q = q.where(Document.file_type == file_type)
    if needs_review is not None:
        q = q.where(Document.needs_review == needs_review)
    ex = []
    if supplier:
        ex.append(func.lower(Extraction.supplier_name).like(f"%{supplier.lower()}%"))
    if customer:
        ex.append(func.lower(Extraction.customer_name).like(f"%{customer.lower()}%"))
    if invoice_number:
        ex.append(func.lower(Extraction.invoice_number).like(f"%{invoice_number.lower()}%"))
    if currency:
        ex.append(Extraction.currency == currency.upper())
    if date_from:
        ex.append(Extraction.invoice_date >= date_from)
    if date_to:
        ex.append(Extraction.invoice_date <= date_to)
    if min_total is not None:
        ex.append(Extraction.total >= min_total)
    if max_total is not None:
        ex.append(Extraction.total <= max_total)
    if ex:
        q = q.where(Document.id.in_(select(Extraction.document_id).where(and_(*ex))))
    return q


def keyword_search(session, query: str | None, limit=50, offset=0, **flt):
    """AND-match every token against filename, extracted text and structured fields."""
    q = _filters(select(Document), **flt)
    toks = _TOKEN.findall((query or "").lower())
    for t in toks:
        like = f"%{t}%"
        q = q.where(or_(
            func.lower(Document.filename).like(like),
            func.lower(func.coalesce(Document.extracted_text, "")).like(like),
            Document.id.in_(select(Extraction.document_id).where(or_(
                func.lower(func.coalesce(Extraction.supplier_name, "")).like(like),
                func.lower(func.coalesce(Extraction.customer_name, "")).like(like),
                func.lower(func.coalesce(Extraction.invoice_number, "")).like(like)))),
        ))
    total = session.scalar(select(func.count()).select_from(q.subquery()))
    docs = session.scalars(q.order_by(Document.created_at.desc()).limit(limit).offset(offset)).all()
    return docs, total


def semantic_search(session, query: str, limit=20, min_score=0.12, **flt):
    """Cosine similarity over chunk embeddings; document score = best chunk."""
    qv = np.asarray(embed(query), dtype=np.float32)
    id_q = _filters(select(Document.id), **flt)
    rows = session.execute(select(Chunk.document_id, Chunk.text, Chunk.embedding).where(Chunk.document_id.in_(id_q))).all()
    if not rows:
        return []
    mat = np.asarray([r[2] for r in rows], dtype=np.float32)
    scores = mat @ qv
    best: dict = {}
    for (doc_id, text, _), s in zip(rows, scores):
        if s >= min_score and (doc_id not in best or s > best[doc_id][0]):
            best[doc_id] = (float(s), text)
    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:limit]
    docs = {d.id: d for d in session.scalars(select(Document).where(Document.id.in_([k for k, _ in ranked])))}
    return [(docs[k], s, snip) for k, (s, snip) in ranked if k in docs]


def snippet(text: str | None, query: str | None, width=160) -> str | None:
    if not text:
        return None
    t = " ".join(text.split())
    for tok in _TOKEN.findall((query or "").lower()):
        i = t.lower().find(tok)
        if i >= 0:
            a = max(0, i - width // 2)
            return ("…" if a else "") + t[a:a + width] + ("…" if a + width < len(t) else "")
    return t[:width]


def hybrid_search(session, query: str, limit=20, **flt):
    """Reciprocal-rank fusion of keyword (OR-relaxed) and semantic rankings."""
    sem = semantic_search(session, query, limit=limit * 2, **flt)
    kw_docs, _ = keyword_search(session, query, limit=limit * 2, **flt)
    score: dict = {}
    meta: dict = {}
    for r, (d, s, sn) in enumerate(sem):
        score[d.id] = score.get(d.id, 0) + 1 / (60 + r)
        meta[d.id] = (d, sn)
    for r, d in enumerate(kw_docs):
        score[d.id] = score.get(d.id, 0) + 1 / (60 + r)
        meta.setdefault(d.id, (d, None))
    ranked = sorted(score, key=lambda k: -score[k])[:limit]
    return [(meta[k][0], round(score[k] * 60 / 2, 4), meta[k][1] or snippet(meta[k][0].extracted_text, query)) for k in ranked]
