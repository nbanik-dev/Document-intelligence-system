import shutil

import pytest
from PIL import Image

from app.pipeline.image import parse_image
from app.pipeline.pdf import CorruptFileError, parse_pdf

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def test_digital_pdf_uses_text_layer_not_ocr(files):
    p = parse_pdf(str(files["dir"] / "digital.pdf"))
    assert p.method == "text_layer" and p.ocr_confidence is None
    assert files["inv"]["invoice_number"] in p.text
    # table cells stay on one row (needed for line-item parsing)
    first = files["inv"]["line_items"][0]["description"]
    assert any(first in ln and "Qty" not in ln and len(ln.split()) > 3 for ln in p.text.splitlines())


def test_multipage_pdf(tmp_path, rng):
    from evaluation import generate_dataset as gen
    invs = [gen.make_invoice(rng, i) for i in range(5)]
    f = tmp_path / "m.pdf"
    f.write_bytes(gen.render_pdf_bytes(invs, ["A"] * 5, [()] * 5))
    seen = []
    p = parse_pdf(str(f), progress=seen.append)
    assert len(p.pages) == 5 and seen[-1] == 1.0
    assert all(i["invoice_number"] in p.text for i in invs)


@needs_tesseract
def test_scanned_pdf_goes_through_ocr(files):
    p = parse_pdf(str(files["dir"] / "scan.pdf"))
    assert p.method == "ocr" and p.ocr_confidence > 70
    assert files["inv"]["invoice_number"] in p.text


@needs_tesseract
def test_clean_png_ocr(files):
    p = parse_image(str(files["dir"] / "clean.png"))
    assert p.ocr_confidence > 70 and files["inv"]["supplier_name"] in p.text


@needs_tesseract
def test_poor_quality_jpg_still_readable_and_confidence_reported(files):
    p = parse_image(str(files["dir"] / "poor.jpg"))
    assert p.ocr_confidence is not None
    assert files["inv"]["invoice_number"] in p.text.replace(" ", "") or p.ocr_confidence < 80


@needs_tesseract
def test_multipage_tiff(tmp_path, files):
    a = Image.open(files["dir"] / "clean.png").convert("RGB")
    f = tmp_path / "m.tiff"
    a.save(f, save_all=True, append_images=[a])
    assert len(parse_image(str(f)).pages) == 2


@needs_tesseract
def test_blank_image_gives_empty_text_not_crash(tmp_path):
    f = tmp_path / "b.png"
    Image.new("RGB", (800, 1000), "white").save(f)
    assert parse_image(str(f)).text.strip() == ""


@pytest.mark.parametrize("content", [b"", b"%PDF-1.4 truncated", b"not a pdf at all"])
def test_corrupt_pdf(tmp_path, content):
    f = tmp_path / "c.pdf"
    f.write_bytes(content)
    with pytest.raises(CorruptFileError):
        parse_pdf(str(f))


def test_password_protected_pdf(tmp_path, files):
    import pymupdf
    f = tmp_path / "locked.pdf"
    d = pymupdf.open(str(files["dir"] / "digital.pdf"))
    d.save(str(f), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    with pytest.raises(CorruptFileError, match="password"):
        parse_pdf(str(f))


def test_corrupt_image(tmp_path):
    f = tmp_path / "c.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    with pytest.raises(CorruptFileError):
        parse_image(str(f))
