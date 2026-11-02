from dataclasses import dataclass, field


@dataclass
class PageText:
    page_no: int
    text: str
    method: str  # text_layer | ocr | table
    confidence: float | None = None  # OCR confidence 0-100
    error: str | None = None
    words: list = field(default_factory=list)  # [(word, ocr_conf 0-100)] for OCR pages


@dataclass
class ParsedDocument:
    kind: str  # pdf | image | table
    pages: list = field(default_factory=list)  # list[PageText]
    invoices: list | None = None  # pre-structured invoices (spreadsheets) -> list[dict]
    warnings: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(f"[page {p.page_no}]\n{p.text}" if self.kind != "table" else p.text
                           for p in self.pages if p.text)

    @property
    def method(self) -> str:
        ms = {p.method for p in self.pages}
        return "mixed" if len(ms) > 1 else (ms.pop() if ms else "none")

    @property
    def ocr_confidence(self) -> float | None:
        c = [p.confidence for p in self.pages if p.confidence is not None]
        return round(sum(c) / len(c), 1) if c else None
