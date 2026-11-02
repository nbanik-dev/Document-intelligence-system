import os
import random
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="docintel-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["STORAGE_DIR"] = f"{_TMP}/uploads"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from evaluation import generate_dataset as gen  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


_seed = iter(range(1000, 10**6))


@pytest.fixture()
def rng():
    # unique seed per test: the DB is shared across the session and identical invoices would (correctly) be
    # flagged as duplicates of one another
    return random.Random(next(_seed))


@pytest.fixture(scope="session")
def files(tmp_path_factory):
    """A handful of generated documents of every supported kind (+ ground truth)."""
    d = tmp_path_factory.mktemp("docs")
    r = random.Random(11)
    out = {}
    inv = gen.make_invoice(r, 1)
    out["inv"] = inv
    (d / "digital.pdf").write_bytes(gen.render_pdf_bytes([inv], ["A"], [()]))
    img = gen.pdf_to_images(gen.render_pdf_bytes([inv], ["B"], [()]))[0]
    img.save(d / "clean.png")
    gen.images_to_pdf([img], d / "scan.pdf")
    gen.degrade(img, r, "degraded").save(d / "poor.jpg", quality=40)
    gen.table_rows([inv]).to_csv(d / "inv.csv", index=False)
    import pandas as pd
    with pd.ExcelWriter(d / "form.xlsx", engine="openpyxl") as w:
        gen.form_sheet(inv).to_excel(w, header=False, index=False, sheet_name="Invoice")
        pd.DataFrame([["notes"]]).to_excel(w, header=False, index=False, sheet_name="Notes")
    gen.write_xls(gen.table_rows([inv]), d / "legacy.xls")
    out["dir"] = d
    return out
