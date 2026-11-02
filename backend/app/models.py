"""Database schema (see docs/ARCHITECTURE.md for the ER diagram)."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _now():
    return datetime.now(timezone.utc)


def _uuid():
    return uuid.uuid4().hex


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    file_type: Mapped[str] = mapped_column(String(16), index=True)  # pdf|image|xlsx|xls|csv
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="queued")
    # queued | processing | completed | needs_review | failed
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extraction_method: Mapped[str | None] = mapped_column(String(32), nullable=True)  # text_layer|ocr|mixed|table
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    processing_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    extractions = relationship("Extraction", back_populates="document", cascade="all, delete-orphan",
                               order_by="Extraction.seq")
    issues = relationship("ValidationIssue", back_populates="document", cascade="all, delete-orphan")
    events = relationship("ProcessingEvent", back_populates="document", cascade="all, delete-orphan",
                          order_by="ProcessingEvent.id")
    chunks = relationship("Chunk", back_populates="document", cascade="all, delete-orphan")


class Extraction(Base):
    """One invoice found in a document (a PDF/sheet can contain many)."""
    __tablename__ = "extractions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)  # "pages 3-4" / "Sheet1!rows 2-9"
    data: Mapped[dict] = mapped_column(JSON)  # full structured JSON incl. line_items
    field_confidence: Mapped[dict] = mapped_column(JSON, default=dict)
    method: Mapped[str] = mapped_column(String(32))  # llm | heuristic | heuristic_fallback | table
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)  # human approved
    manually_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    # denormalised columns for filtering/search/duplicate detection
    invoice_number: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    invoice_number_norm: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supplier_name: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    supplier_norm: Mapped[str | None] = mapped_column(String(256), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    invoice_date: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)  # ISO yyyy-mm-dd
    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    document = relationship("Document", back_populates="extractions")
    issues = relationship("ValidationIssue", back_populates="extraction", cascade="all, delete-orphan")
    corrections = relationship("Correction", back_populates="extraction", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_extr_dupe", "supplier_norm", "invoice_number_norm"),)


class ValidationIssue(Base):
    __tablename__ = "validation_issues"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    extraction_id: Mapped[str | None] = mapped_column(ForeignKey("extractions.id", ondelete="CASCADE"),
                                                      nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(10))  # error | warning
    field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    document = relationship("Document", back_populates="issues")
    extraction = relationship("Extraction", back_populates="issues")


class ProcessingEvent(Base):
    __tablename__ = "processing_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # started | ok | error | info
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    document = relationship("Document", back_populates="events")


class Correction(Base):
    """Audit trail of manual edits."""
    __tablename__ = "corrections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("extractions.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    extraction = relationship("Extraction", back_populates="corrections")


class Chunk(Base):
    """Text chunk + embedding for semantic search (swap `embedding` for a pgvector column in production)."""
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(JSON)

    document = relationship("Document", back_populates="chunks")
