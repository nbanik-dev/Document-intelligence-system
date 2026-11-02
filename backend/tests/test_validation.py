import copy

from app.validation import needs_review, validate_invoice

GOOD = {"invoice_number": "A1", "invoice_date": "2024-01-02", "supplier_name": "X Ltd", "customer_name": "Y Inc",
        "subtotal": 100.0, "tax": 10.0, "total": 110.0, "currency": "USD",
        "line_items": [{"description": "a", "quantity": 2, "unit_price": 50.0, "amount": 100.0}]}


def codes(d, conf=None, ocr=None):
    return {(i.code, i.severity) for i in validate_invoice(d, conf, ocr)}


def mod(**kw):
    d = copy.deepcopy(GOOD)
    d.update(kw)
    return d


def test_clean_invoice_has_no_issues():
    assert validate_invoice(GOOD) == [] and not needs_review([])


def test_missing_required_fields_are_errors():
    c = codes(mod(invoice_number=None, total=None, supplier_name=""))
    assert ("MISSING_FIELD", "error") in c
    assert len([i for i in validate_invoice(mod(invoice_number=None, total=None)) if i.code == "MISSING_FIELD"]) == 2


def test_missing_optional_field_is_warning_but_still_needs_a_human():
    issues = validate_invoice(mod(customer_name=None))
    assert [(i.code, i.severity) for i in issues] == [("MISSING_FIELD", "warning")] and needs_review(issues)


def test_total_mismatch():
    assert ("TOTAL_MISMATCH", "error") in codes(mod(total=125.0))


def test_rounding_within_tolerance_ok():
    assert validate_invoice(mod(total=110.01)) == []


def test_subtotal_vs_line_items():
    assert ("SUBTOTAL_MISMATCH", "error") in codes(mod(subtotal=90.0, total=100.0))


def test_line_item_arithmetic():
    d = mod(line_items=[{"description": "a", "quantity": 3, "unit_price": 50.0, "amount": 100.0}])
    assert ("LINE_ITEM_MISMATCH", "error") in codes(d)


def test_invalid_and_negative_numbers():
    assert ("INVALID_NUMBER", "error") in codes(mod(total="abc"))
    assert ("NEGATIVE_AMOUNT", "warning") in codes(mod(tax=-5.0, total=95.0))
    assert ("INVALID_NUMBER", "error") in codes(mod(total=float("nan")))


def test_dates_and_currency():
    assert ("INVALID_DATE", "error") in codes(mod(invoice_date="2024-13-45"))
    assert ("FUTURE_DATE", "warning") in codes(mod(invoice_date="2999-01-01"))
    assert ("INVALID_CURRENCY", "warning") in codes(mod(currency="XXX"))


def test_suspicious_tax_rate():
    assert ("TAX_RATE_SUSPICIOUS", "warning") in codes(mod(tax=80.0, total=180.0))


def test_no_line_items_requires_review():
    issues = validate_invoice(mod(line_items=[]))
    assert any(i.code == "NO_LINE_ITEMS" for i in issues) and needs_review(issues)


def test_low_confidence_and_ocr_quality_flag_review():
    assert needs_review(validate_invoice(GOOD, {"total": 0.4}))
    assert needs_review(validate_invoice(GOOD, {}, ocr_conf=35.0))
    assert not needs_review(validate_invoice(GOOD, {"total": 0.95}, ocr_conf=92.0))


def test_total_without_subtotal_uses_line_items():
    d = mod(subtotal=None, tax=None, total=150.0)
    assert ("TOTAL_MISMATCH", "error") in codes(d)


def test_error_on_large_invoice_is_not_hidden_by_tolerance():
    big = mod(subtotal=9000.0, tax=900.0, total=9920.0,
              line_items=[{"description": "a", "quantity": 1, "unit_price": 9000.0, "amount": 9000.0}])
    assert ("TOTAL_MISMATCH", "error") in codes(big)  # off by 20.00 on 9,900 (0.2%)
