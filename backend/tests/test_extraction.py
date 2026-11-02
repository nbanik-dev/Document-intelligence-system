import pytest

from app.config import settings
from app.extraction import llm
from app.extraction.heuristic import extract_invoice
from app.extraction.segment import segment_pages
from app.pipeline.types import PageText

TEXT = """ACME Industrial Supplies Ltd
INVOICE
Invoice No: INV-2024-0012
Invoice Date: 15 March 2024
Due Date: 14 April 2024
Bill To: Beta Holdings Inc
Description Qty Unit Price Amount
Steel bolts M8 100 0.25 $25.00
Hydraulic pump 2 450.00 $900.00
Subtotal $925.00
VAT (10%) $92.50
Total $1,017.50
"""


def test_heuristic_extracts_all_fields():
    inv, conf = extract_invoice(TEXT)
    assert inv["invoice_number"] == "INV-2024-0012" and inv["invoice_date"] == "2024-03-15"
    assert inv["supplier_name"] == "ACME Industrial Supplies Ltd" and inv["customer_name"] == "Beta Holdings Inc"
    assert (inv["subtotal"], inv["tax"], inv["total"]) == (925.0, 92.5, 1017.5)
    assert len(inv["line_items"]) == 2 and inv["line_items"][1]["unit_price"] == 450.0
    assert inv["invoice_date"] != "2024-04-14"  # due date must not be mistaken for invoice date


def test_missing_information_is_none_not_invented():
    inv, conf = extract_invoice("Some random memo\nNothing to see here 123")
    assert inv["invoice_number"] is None and inv["invoice_date"] is None and inv["total"] is None
    assert inv["customer_name"] is None and inv["line_items"] == [] and inv["currency"] is None


def test_position_based_supplier_without_company_form_is_low_confidence():
    _, conf = extract_invoice("Joes place\nInvoice No: A-1\nTotal 5.00")
    assert conf["supplier_name"] < 0.6


def test_ambiguous_date_flagged():
    inv, conf = extract_invoice("Invoice No: A-1\nDate: 05/03/2024\nTotal 5.00")
    assert conf["invoice_date"] < 0.6


def test_segmentation_by_invoice_number():
    T = "text_layer"
    pages = [PageText(1, "Invoice No: A-1\nx", T), PageText(2, "continuation page", T),
             PageText(3, "Invoice No: B-2\ny", T), PageText(4, "", T)]
    segs = segment_pages(pages)
    assert [s["pages"] for s in segs] == [(1, 2), (3, 3)]


# ---------------------------------------------------------------- LLM path (network mocked)
@pytest.fixture()
def llm_on(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")


def _raw(**over):
    base = {"invoice_number": "INV-2024-0012", "invoice_date": "2024-03-15", "supplier_name": "ACME Industrial Supplies Ltd",
            "customer_name": "Beta Holdings Inc", "subtotal": 925.0, "tax": 92.5, "total": 1017.5, "currency": "USD",
            "line_items": [{"description": "Hydraulic pump", "quantity": 2, "unit_price": 450.0, "amount": 900.0}],
            "confidence": {"invoice_number": 0.99, "total": 0.98}}
    base.update(over)
    return base


def test_llm_happy_path(monkeypatch, llm_on):
    monkeypatch.setattr(llm, "call_llm", lambda t: _raw())
    inv, conf, method, notes = llm.extract_text(TEXT)
    assert method == "llm" and inv["total"] == 1017.5 and notes == [] and conf["total"] == 0.98


def test_llm_hallucinated_strings_are_discarded(monkeypatch, llm_on):
    monkeypatch.setattr(llm, "call_llm", lambda t: _raw(supplier_name="Totally Invented GmbH", invoice_number="ZZ-999"))
    inv, conf, method, notes = llm.extract_text(TEXT)
    assert inv["supplier_name"] is None and inv["invoice_number"] is None
    assert conf["supplier_name"] == 0.0 and len(notes) == 2


def test_llm_ungrounded_numbers_get_low_confidence_and_bad_lines_dropped(monkeypatch, llm_on):
    monkeypatch.setattr(llm, "call_llm", lambda t: _raw(total=1111.11, line_items=[
        {"description": "Ghost item", "quantity": 1, "unit_price": 777.0, "amount": 777.0},
        {"description": "Hydraulic pump", "quantity": 2, "unit_price": 450.0, "amount": 900.0}]))
    inv, conf, _, notes = llm.extract_text(TEXT)
    assert conf["total"] <= 0.4 and len(inv["line_items"]) == 1


def test_llm_garbage_values_are_coerced_safely(monkeypatch, llm_on):
    monkeypatch.setattr(llm, "call_llm", lambda t: _raw(total="n/a", currency="ZZZ", invoice_date="someday", subtotal=None))
    inv, *_ = llm.extract_text(TEXT)
    assert inv["total"] is None and inv["currency"] is None and inv["invoice_date"] is None


def test_llm_failure_falls_back_to_heuristics(monkeypatch, llm_on):
    def boom(_):
        raise TimeoutError("api down")
    monkeypatch.setattr(llm, "call_llm", boom)
    inv, conf, method, notes = llm.extract_text(TEXT)
    assert method == "heuristic_fallback" and inv["total"] == 1017.5 and "LLM unavailable" in notes[0]


def test_low_ocr_word_confidence_lowers_field_confidence():
    from app.extraction.ocr_confidence import apply_ocr_word_confidence
    data = {"invoice_number": "INV-2024-0012", "supplier_name": "ACME Ltd", "total": 1017.5, "customer_name": None}
    conf = {"invoice_number": 0.9, "supplier_name": 0.9, "total": 0.9}
    words = [("INV-2024-0012", 52.0), ("ACME", 96.0), ("Ltd", 94.0), ("$1,017.50", 93.0)]
    notes = apply_ocr_word_confidence(data, conf, words, 75)
    assert conf["invoice_number"] == 0.5 and conf["supplier_name"] == 0.9 and conf["total"] == 0.9
    assert len(notes) == 1 and "invoice_number" in notes[0]
    assert apply_ocr_word_confidence(data, {"invoice_number": 0.9}, [], 75) == []  # born-digital: no words, no change
