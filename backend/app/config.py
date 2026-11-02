"""Central configuration. Everything is overridable through environment variables."""
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./data/docintel.db"))
    storage_dir: Path = field(default_factory=lambda: Path(_env("STORAGE_DIR", "./data/uploads")))
    max_upload_mb: int = field(default_factory=lambda: int(_env("MAX_UPLOAD_MB", "200")))
    max_files_per_request: int = field(default_factory=lambda: int(_env("MAX_FILES_PER_REQUEST", "50")))
    api_key: str = field(default_factory=lambda: _env("API_KEY", ""))  # empty => auth disabled (dev)
    cors_origins: list = field(default_factory=lambda: _env("CORS_ORIGINS", "http://localhost:5173").split(","))
    worker_threads: int = field(default_factory=lambda: int(_env("WORKER_THREADS", "4")))

    # OCR
    ocr_dpi: int = field(default_factory=lambda: int(_env("OCR_DPI", "200")))
    ocr_min_confidence: float = field(default_factory=lambda: float(_env("OCR_MIN_CONFIDENCE", "60")))
    ocr_lang: str = field(default_factory=lambda: _env("OCR_LANG", "eng"))
    ocr_field_min_confidence: float = field(default_factory=lambda: float(_env("OCR_FIELD_MIN_CONFIDENCE", "70")))
    pdf_min_text_chars: int = field(default_factory=lambda: int(_env("PDF_MIN_TEXT_CHARS", "40")))

    # LLM extraction (falls back to the rule-based extractor when no key is set)
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-sonnet-5-5"))
    llm_timeout_s: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_S", "60")))
    llm_max_chars: int = field(default_factory=lambda: int(_env("LLM_MAX_CHARS", "60000")))

    # Search
    embedding_provider: str = field(default_factory=lambda: _env("EMBEDDING_PROVIDER", "hash"))  # hash | sbert
    embedding_dim: int = 512

    # Validation tolerances
    amount_abs_tol: float = 0.02
    amount_rel_tol: float = 0.0  # keep 0: a relative tolerance hides real errors on large invoices


settings = Settings()
