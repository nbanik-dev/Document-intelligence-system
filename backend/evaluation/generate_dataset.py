"""Synthetic labelled dataset generator (invoices as digital PDF, scanned PDF, PNG/JPG/TIFF, CSV, XLSX/XLS,
plus anomalies, duplicates, missing fields and corrupted files). Writes <out>/labels.json with ground truth.

Usage: python -m evaluation.generate_dataset --out data/eval --seed 42 [--scale 1.0]
"""
import argparse
import io
import json
import os
import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pymupdf
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

SUPPLIERS = ["Acme Industrial Supplies Ltd", "Northwind Traders", "Blue Harbor Logistics LLC", "Kestrel Components GmbH",
             "Summit Office Solutions Inc", "Lumen Print Services", "Orion Freight Co", "Pioneer Chemicals Corp",
             "Greenfield Packaging Ltd", "Delta Cloud Systems Inc", "Maple Leaf Stationery", "Atlas Tooling Partners",
             "Harborview Catering Services", "Redwood Electrical Supplies Ltd", "Quantum Data Holdings"]
CUSTOMERS = ["Beta Holdings Inc", "Gamma Retail Group", "Zenith Manufacturing Ltd", "Pacific Rim Traders", "Omega Health Services",
             "Sterling Construction Co", "Nova Digital Agency", "Riverside Hospital Trust", "Tandem Logistics LLC",
             "Vertex Engineering Ltd", "Cobalt Media Group", "Evergreen Foods Inc"]
ITEMS = ["Steel bolts M8", "Hydraulic pump", "Office chairs", "A4 copy paper (box)", "Consulting hours", "Cloud hosting - monthly",
         "Network switch 24-port", "Safety gloves (pair)", "LED panel light", "Freight surcharge", "Printer toner cartridge",
         "Software license (annual)", "Packaging tape roll", "Maintenance visit", "Laptop docking station", "Industrial lubricant 5L"]
CUR = {"USD": "$", "EUR": "€", "GBP": "£"}
TAXES = [("Tax", 10), ("VAT", 20), ("GST", 5), ("Sales Tax", 8), ("VAT", 15)]
STYLES = {
    "A": dict(title="INVOICE", num="Invoice No", date="Invoice Date", dfmt="%d %B %Y", frm=None, cust="Bill To",
              hdr=["Description", "Qty", "Unit Price", "Amount"], sub="Subtotal", tot="Total"),
    "B": dict(title="TAX INVOICE", num="Invoice #", date="Date", dfmt="%Y-%m-%d", frm="From", cust="Customer",
              hdr=["Description", "Quantity", "Price", "Amount"], sub="Sub-total", tot="Total Due"),
    "C": dict(title="Invoice", num="Invoice Number", date="Issue Date", dfmt="%b %d, %Y", frm="Supplier", cust="Billed To",
              hdr=["Description", "Qty", "Rate", "Line Total"], sub="Subtotal", tot="Amount Due"),
}


def money(v, sym):
    return f"{sym}{v:,.2f}"


def make_invoice(rng: random.Random, idx: int, supplier=None, customer=None) -> dict:
    cur = rng.choices(list(CUR), weights=[6, 2, 2])[0]
    taxname, rate = rng.choice(TAXES)
    items = []
    for it in rng.sample(ITEMS, rng.randint(1, 6)):
        q = rng.choice([1, 1, 2, 3, 5, 10, 12, 25, 100])
        u = round(rng.choice([0.25, 1.5, 4.99, 12.0, 19.95, 45.5, 89.0, 250.0, 450.0, 1200.0]) * rng.uniform(0.9, 1.1), 2)
        items.append({"description": it, "quantity": float(q), "unit_price": u, "amount": round(q * u, 2)})
    sub = round(sum(i["amount"] for i in items), 2)
    tax = round(sub * rate / 100, 2)
    d = date(2023, 1, 1) + timedelta(days=rng.randint(0, 800))
    fmt = rng.choice(["INV-{y}-{n:04d}", "{y}/{n:04d}", "A{n:05d}", "INV{n:06d}", "{y}-{n:03d}-X"])
    return {"invoice_number": fmt.format(y=d.year, n=idx + rng.randint(1, 900)),
            "invoice_date": d.isoformat(), "supplier_name": supplier or rng.choice(SUPPLIERS),
            "customer_name": customer or rng.choice(CUSTOMERS), "subtotal": sub, "tax": tax,
            "total": round(sub + tax, 2), "currency": cur, "line_items": items,
            "_taxname": taxname, "_rate": rate}


def gt(inv: dict) -> dict:
    return {k: v for k, v in inv.items() if not k.startswith("_")}


def layout(inv: dict, style: str, omit=()) -> list:
    """-> list of rows; row = list of (x, text, bold). Printed values follow inv (even if internally inconsistent)."""
    S, sym = STYLES[style], CUR[inv["currency"]]
    d = date.fromisoformat(inv["invoice_date"])
    rows = []
    if S["frm"] is None:
        rows.append([(50, inv["supplier_name"], True)])
    rows.append([(50, S["title"], True)])
    rows.append([(50, f'{S["num"]}: {inv["invoice_number"]}', False)])
    if "invoice_date" not in omit:
        rows.append([(50, f'{S["date"]}: {d.strftime(S["dfmt"])}', False)])
        rows.append([(50, f'Due Date: {(d + timedelta(days=30)).strftime(S["dfmt"])}', False)])
    if S["frm"]:
        rows.append([(50, f'{S["frm"]}: {inv["supplier_name"]}', False)])
    if "customer_name" not in omit:
        rows.append([(50, f'{S["cust"]}: {inv["customer_name"]}', False)])
    rows.append([])
    xs = [50, 280, 340, 440]
    rows.append([(x, h, True) for x, h in zip(xs, S["hdr"])])
    for li in inv["line_items"]:
        q = int(li["quantity"]) if float(li["quantity"]).is_integer() else li["quantity"]
        rows.append([(xs[0], li["description"], False), (xs[1], str(q), False),
                     (xs[2], money(li["unit_price"], sym), False), (xs[3], money(li["amount"], sym), False)])
    rows.append([])
    rows.append([(340, S["sub"], False), (440, money(inv["subtotal"], sym), False)])
    rows.append([(340, f'{inv["_taxname"]} ({inv["_rate"]}%)', False), (440, money(inv["tax"], sym), False)])
    rows.append([(340, S["tot"], True), (440, money(inv["total"], sym), True)])
    return rows


def render_pdf_bytes(invoices: list, styles: list, omits: list) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for inv, st, om in zip(invoices, styles, omits):
        y = 790
        for row in layout(inv, st, om):
            for x, text, bold in row:
                c.setFont("Helvetica-Bold" if bold else "Helvetica", 11)
                c.drawString(x, y, text)
            y -= 20
        c.showPage()
    c.save()
    return buf.getvalue()


def pdf_to_images(pdf_bytes: bytes, dpi=150) -> list:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for p in doc:
        pix = p.get_pixmap(dpi=dpi)
        out.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    return out


def degrade(img: Image.Image, rng: random.Random, level: str) -> Image.Image:
    if level == "clean":
        return img
    nrng = np.random.default_rng(rng.randint(0, 10**6))
    img = img.rotate(rng.uniform(-2.5, 2.5), expand=True, fillcolor=(255, 255, 255), resample=Image.BICUBIC)
    w, h = img.size
    img = img.resize((int(w * 0.7), int(h * 0.7)), Image.BILINEAR)  # lower resolution
    img = img.filter(ImageFilter.GaussianBlur(radius=0.9))
    arr = np.asarray(img).astype(np.float32)
    arr += nrng.normal(0, 14, arr.shape)  # sensor noise
    arr = arr * rng.uniform(0.82, 0.95) + 12  # washed-out contrast
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def save_image(img, path: Path, fmt: str, rng):
    if fmt == "png":
        img.save(path, "PNG")
    elif fmt == "jpg":
        img.save(path, "JPEG", quality=rng.choice([45, 60, 85]))
    else:
        img.save(path, "TIFF", compression="tiff_lzw")


def images_to_pdf(imgs: list, path: Path):
    doc = pymupdf.open()
    for im in imgs:
        b = io.BytesIO()
        im.save(b, "JPEG", quality=70)
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=b.getvalue())
    doc.save(str(path))


def table_rows(invs: list) -> pd.DataFrame:
    rows = []
    for inv in invs:
        for k, li in enumerate(inv["line_items"]):
            last = k == len(inv["line_items"]) - 1
            rows.append({"Invoice Number": inv["invoice_number"], "Invoice Date": inv["invoice_date"],
                         "Supplier": inv["supplier_name"], "Customer": inv["customer_name"],
                         "Description": li["description"], "Qty": li["quantity"], "Unit Price": li["unit_price"],
                         "Amount": li["amount"], "Subtotal": inv["subtotal"] if last else None,
                         "Tax": inv["tax"] if last else None, "Total": inv["total"] if last else None,
                         "Currency": inv["currency"]})
    return pd.DataFrame(rows)


def write_xls(df: pd.DataFrame, path: Path):
    import xlwt  # pandas 3 dropped its xlwt writer; write the legacy BIFF file directly
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Invoices")
    for c, h in enumerate(df.columns):
        ws.write(0, c, h)
    for r, row in enumerate(df.itertuples(index=False), start=1):
        for c, v in enumerate(row):
            if v is not None and not (isinstance(v, float) and v != v):
                ws.write(r, c, v)
    wb.save(str(path))


def form_sheet(inv: dict) -> pd.DataFrame:
    rows = [["INVOICE", None, None, None], ["Invoice Number", inv["invoice_number"], None, None],
            ["Invoice Date", inv["invoice_date"], None, None], ["Supplier", inv["supplier_name"], None, None],
            ["Bill To", inv["customer_name"], None, None], ["Currency", inv["currency"], None, None], [None] * 4,
            ["Description", "Qty", "Unit Price", "Amount"]]
    rows += [[li["description"], li["quantity"], li["unit_price"], li["amount"]] for li in inv["line_items"]]
    rows += [[None] * 4, [None, None, "Subtotal", inv["subtotal"]], [None, None, f'{inv["_taxname"]} ({inv["_rate"]}%)', inv["tax"]],
             [None, None, "Total", inv["total"]]]
    return pd.DataFrame(rows)


def build_dataset(out: str, seed: int = 42, scale: float = 1.0) -> list:
    rng = random.Random(seed)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    labels, counter = [], [0]

    def new_inv(**kw):
        counter[0] += 1
        return make_invoice(rng, counter[0] * 7, **kw)

    def add(file, fmt, invs, quality="clean", expect=None, omit=(), corrupt=False, dup=False, notes=""):
        labels.append({"file": file, "format": fmt, "quality": quality, "corrupt": corrupt,
                       "invoices": [gt(i) for i in invs], "omitted_fields": list(omit),
                       "expect_issues": expect or [], "duplicate": dup, "notes": notes})

    def n(x):
        return max(1, round(x * scale))

    anomalies = ["total_mismatch", "subtotal_mismatch", "line_mismatch"]

    def apply_anomaly(inv, kind):
        if kind == "total_mismatch":
            inv["total"] = round(inv["total"] + rng.choice([10, 25.5, 100, -40]), 2)
            return ["TOTAL_MISMATCH"]
        if kind == "subtotal_mismatch":
            inv["subtotal"] = round(inv["subtotal"] + rng.choice([5, 17.25, 60]), 2)
            inv["total"] = round(inv["subtotal"] + inv["tax"], 2)  # internally consistent total, lines disagree
            return ["SUBTOTAL_MISMATCH"]
        li = inv["line_items"][0]
        li["amount"] = round(li["amount"] + rng.choice([3, 12.5, 80]), 2)
        return ["LINE_ITEM_MISMATCH", "SUBTOTAL_MISMATCH"]

    # --- digital PDFs
    for i in range(n(26)):
        inv, st = new_inv(), rng.choice("ABC")
        f = f"digital_{i:03d}.pdf"
        (out / f).write_bytes(render_pdf_bytes([inv], [st], [()]))
        add(f, "pdf_digital", [inv])
    for i in range(n(5)):  # multi-invoice PDFs (3 invoices, 3 pages)
        invs, st = [new_inv() for _ in range(3)], rng.choice("ABC")
        f = f"digital_multi_{i:03d}.pdf"
        (out / f).write_bytes(render_pdf_bytes(invs, [st] * 3, [()] * 3))
        add(f, "pdf_digital_multi", invs)
    # --- scanned PDFs & images
    for i in range(n(20)):
        q = "clean" if i % 2 == 0 else "degraded"
        inv, st = new_inv(), rng.choice("ABC")
        img = degrade(pdf_to_images(render_pdf_bytes([inv], [st], [()]))[0], rng, q)
        f = f"scan_{i:03d}.pdf"
        images_to_pdf([img], out / f)
        add(f, "pdf_scanned", [inv], q)
    for i, fmt in enumerate(["png"] * n(14) + ["jpg"] * n(10) + ["tiff"] * n(5)):
        q = "clean" if i % 2 == 0 else "degraded"
        inv, st = new_inv(), rng.choice("ABC")
        img = degrade(pdf_to_images(render_pdf_bytes([inv], [st], [()]))[0], rng, q)
        f = f"image_{i:03d}.{fmt}"
        save_image(img, out / f, fmt, rng)
        add(f, f"image_{fmt}", [inv], q)
    # --- spreadsheets
    for i in range(n(10)):
        invs = [new_inv() for _ in range(rng.choice([1, 1, 3]))]
        f = f"table_{i:03d}.csv"
        table_rows(invs).to_csv(out / f, index=False)
        add(f, "csv", invs)
    for i in range(n(6)):
        inv = new_inv()
        f = f"form_{i:03d}.xlsx"
        with pd.ExcelWriter(out / f, engine="openpyxl") as w:
            form_sheet(inv).to_excel(w, header=False, index=False, sheet_name="Invoice")
            pd.DataFrame([["Internal notes"], ["Paid by wire"]]).to_excel(w, header=False, index=False, sheet_name="Notes")
        add(f, "xlsx", [inv])
    for i in range(n(4)):
        invs = [new_inv() for _ in range(3)]
        f = f"table_{i:03d}.xlsx"
        with pd.ExcelWriter(out / f, engine="openpyxl") as w:
            table_rows(invs[:2]).to_excel(w, index=False, sheet_name="Q1")
            table_rows(invs[2:]).to_excel(w, index=False, sheet_name="Q2")
        add(f, "xlsx_multisheet", invs)
    for i in range(n(2)):
        inv = new_inv()
        f = f"legacy_{i:03d}.xls"
        write_xls(table_rows([inv]), out / f)
        add(f, "xls", [inv])
    # --- anomalies (arithmetic errors) in PDF + CSV
    for i in range(n(9)):
        inv = new_inv()
        exp = apply_anomaly(inv, anomalies[i % 3])
        if i % 3 == 2:
            f = f"anomaly_{i:03d}.csv"
            table_rows([inv]).to_csv(out / f, index=False)
            add(f, "csv", [inv], expect=exp, notes="injected arithmetic error")
        else:
            f = f"anomaly_{i:03d}.pdf"
            (out / f).write_bytes(render_pdf_bytes([inv], [rng.choice("ABC")], [()]))
            add(f, "pdf_digital", [inv], expect=exp, notes="injected arithmetic error")
    # --- missing fields
    for i in range(n(8)):
        inv = new_inv()
        om = ("customer_name",) if i % 2 == 0 else ("invoice_date",)
        inv2 = dict(inv)
        f = f"missing_{i:03d}.pdf"
        (out / f).write_bytes(render_pdf_bytes([inv2], [rng.choice("ABC")], [om]))
        g = gt(inv2)
        g[om[0]] = None
        labels.append({"file": f, "format": "pdf_digital", "quality": "clean", "corrupt": False, "invoices": [g],
                       "omitted_fields": list(om), "expect_issues": ["MISSING_FIELD"], "duplicate": False,
                       "notes": "field intentionally absent from document"})
    # --- duplicates (same invoice re-issued in another layout/format, later upload)
    originals = [l for l in labels if l["format"] == "pdf_digital" and not l["expect_issues"] and not l["omitted_fields"]]
    for i, o in enumerate(rng.sample(originals, min(n(6), len(originals)))):
        src = dict(o["invoices"][0])
        src["_taxname"], src["_rate"] = "Tax", round(src["tax"] / src["subtotal"] * 100) if src["subtotal"] else 0
        f = f"duplicate_{i:03d}.pdf"
        (out / f).write_bytes(render_pdf_bytes([src], [rng.choice("ABC")], [()]))
        add(f, "pdf_digital", [src], expect=["DUPLICATE_INVOICE"], dup=True, notes=f"duplicate of {o['file']}")
    # --- corrupted / unreadable files
    good = (out / "digital_000.pdf").read_bytes()
    corrupt = {"corrupt_truncated.pdf": good[:300], "corrupt_empty.pdf": b"", "corrupt_garbage.png": b"\x89PNG\r\n\x1a\n" + os.urandom(500),
               "corrupt_bad.xlsx": b"PK\x03\x04" + os.urandom(300), "corrupt_binary.jpg": b"\xff\xd8\xff\xe0" + os.urandom(400),
               "corrupt_blank.png": None, "corrupt_empty.csv": b"\n\n", "corrupt_tiff.tiff": b"II*\x00" + os.urandom(200)}
    for name, content in list(corrupt.items())[: max(4, n(8))]:
        if content is None:  # valid but blank image
            Image.new("RGB", (800, 1000), "white").save(out / name)
        else:
            (out / name).write_bytes(content)
        add(name, "corrupt", [], corrupt=True)
    (out / "labels.json").write_text(json.dumps(labels, indent=1))
    return labels


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/eval")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scale", type=float, default=1.0)
    a = ap.parse_args()
    labs = build_dataset(a.out, a.seed, a.scale)
    print(f"wrote {len(labs)} files to {a.out}")
