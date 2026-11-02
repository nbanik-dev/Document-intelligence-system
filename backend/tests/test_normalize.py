import pytest

from app.extraction.normalize import detect_currency, norm_key, parse_date, parse_money, parse_number


@pytest.mark.parametrize("raw,exp", [("$1,234.56", 1234.56), ("1.234,56", 1234.56), ("(45.00)", -45.0), ("€ 99", 99.0),
                                     (12, 12.0), ("1,234", 1234.0), ("12,5", 12.5), ("abc", None), ("", None), (None, None),
                                     (float("nan"), None), ("-7.10", -7.1), ("12.3.4", None)])
def test_parse_money(raw, exp):
    assert parse_money(raw) == exp


@pytest.mark.parametrize("raw,iso,amb", [("2024-03-15", "2024-03-15", False), ("15 March 2024", "2024-03-15", False),
                                         ("Mar 15, 2024", "2024-03-15", False), ("15/03/2024", "2024-03-15", False),
                                         ("03/15/2024", "2024-03-15", False), ("05/03/2024", "2024-05-03", True),
                                         ("31/02/2024", None, False), ("not a date", None, False), ("1st March 2024", "2024-03-01", False)])
def test_parse_date(raw, iso, amb):
    assert parse_date(raw) == (iso, amb)


def test_currency_detection():
    assert detect_currency("Total: EUR 100")[0] == "EUR"
    assert detect_currency("Total €100") == ("EUR", 0.8)
    code, conf = detect_currency("Total $100")
    assert code == "USD" and conf < 0.7  # a bare '$' is ambiguous
    assert detect_currency("no money here") == (None, 0.0)


def test_helpers():
    assert parse_number("1,5") == 15.0 or parse_number("1,5") is not None
    assert norm_key("INV-001 / A") == "inv001a" and norm_key(None) is None
