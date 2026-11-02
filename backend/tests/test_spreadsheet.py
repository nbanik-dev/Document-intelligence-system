import pandas as pd
import pytest

from app.pipeline.pdf import CorruptFileError
from app.pipeline.spreadsheet import parse_spreadsheet


def _assert_matches(parsed, inv):
    assert parsed.invoices and len(parsed.invoices) == 1
    d = parsed.invoices[0]["data"]
    for k in ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"):
        assert d[k] == inv[k], k
    assert len(d["line_items"]) == len(inv["line_items"])
    assert [li["amount"] for li in d["line_items"]] == [li["amount"] for li in inv["line_items"]]


def test_csv_tabular(files):
    _assert_matches(parse_spreadsheet(str(files["dir"] / "inv.csv"), "csv"), files["inv"])


def test_xlsx_form_like_multi_sheet(files):
    p = parse_spreadsheet(str(files["dir"] / "form.xlsx"), "xlsx")
    _assert_matches(p, files["inv"])
    assert len(p.pages) == 2  # both sheets kept as searchable text
    assert any("Notes" in w for w in p.warnings)  # sheet without invoice structure reported, not crashed


def test_legacy_xls(files):
    _assert_matches(parse_spreadsheet(str(files["dir"] / "legacy.xls"), "xls"), files["inv"])


def test_multiple_invoices_grouped_by_number(tmp_path):
    f = tmp_path / "m.csv"
    f.write_text("Invoice Number,Date,Vendor,Description,Qty,Unit Price,Amount,Total\n"
                 "A1,2024-01-01,V,x,1,5,5,\nA1,2024-01-01,V,y,2,5,10,15\nB2,2024-02-01,V,z,1,9,9,9\n")
    p = parse_spreadsheet(str(f), "csv")
    assert [i["data"]["invoice_number"] for i in p.invoices] == ["A1", "B2"]
    assert len(p.invoices[0]["data"]["line_items"]) == 2 and p.invoices[0]["data"]["total"] == 15


def test_semicolon_delimiter_and_latin1(tmp_path):
    f = tmp_path / "s.csv"
    f.write_bytes("Invoice Number;Date;Supplier;Total\nX-9;2024-05-06;Café Ltd;12.50\n".encode("latin-1"))
    inv = parse_spreadsheet(str(f), "csv").invoices[0]["data"]
    assert inv["invoice_number"] == "X-9" and inv["supplier_name"] == "Café Ltd" and inv["total"] == 12.5


def test_ambiguous_date_gets_low_confidence(tmp_path):
    f = tmp_path / "d.csv"
    f.write_text("Invoice Number,Date,Supplier,Total\nX-9,05/03/2024,S,1.00\n")
    conf = parse_spreadsheet(str(f), "csv").invoices[0]["field_confidence"]
    assert conf["invoice_date"] <= 0.6


def test_unstructured_sheet_falls_back_to_text(tmp_path):
    f = tmp_path / "u.xlsx"
    pd.DataFrame([["random", "stuff"], ["more", "cells"]]).to_excel(f, header=False, index=False)
    p = parse_spreadsheet(str(f), "xlsx")
    assert p.invoices is None and "random" in p.text


@pytest.mark.parametrize("name,content,ftype", [("e.csv", b"", "csv"), ("bad.xlsx", b"PK\x03\x04garbage", "xlsx"),
                                                ("bad.xls", b"\xd0\xcf\x11\xe0junk", "xls")])
def test_corrupt_spreadsheets_raise_clean_error(tmp_path, name, content, ftype):
    f = tmp_path / name
    f.write_bytes(content)
    with pytest.raises(CorruptFileError):
        parse_spreadsheet(str(f), ftype)
