"""Large-document benchmark (the brief mentions PDFs of ~1000 pages).

  python -m evaluation.bench_large --pages 1000 --scan-pages 40

1. Digital PDF with N pages (one invoice per page): full pipeline (parse -> segment -> extract -> validate -> index).
2. Scanned PDF with M pages: measured, then linearly extrapolated to N pages (clearly labelled as extrapolation).
Reports wall time, pages/s, peak RSS and accuracy vs the generated ground truth.
"""
import argparse
import os
import random
import resource
import sys
import tempfile
import time
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="docintel-bench-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/bench.db"
os.environ["STORAGE_DIR"] = f"{_tmp}/uploads"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal, init_db  # noqa: E402
from app.extraction.normalize import norm_key  # noqa: E402
from app.models import Document  # noqa: E402
from app.services import process_document  # noqa: E402
from evaluation import generate_dataset as gen  # noqa: E402


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def run_doc(path: Path, ftype: str):
    from app.config import settings
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as s:
        d = Document(filename=path.name, stored_path=str(path), file_type=ftype, size_bytes=path.stat().st_size, sha256=path.name)
        s.add(d)
        s.commit()
        did = d.id
    t0 = time.perf_counter()
    process_document(did)
    wall = time.perf_counter() - t0
    with SessionLocal() as s:
        d = s.get(Document, did)
        exs = list(d.extractions)
        return d, exs, wall


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=1000)
    ap.add_argument("--scan-pages", type=int, default=40)
    ap.add_argument("--out", default="../docs/BENCHMARK_LARGE.md")
    a = ap.parse_args()
    init_db()
    rng = random.Random(5)
    lines = ["# Large-document benchmark\n", "_Measured by `python -m evaluation.bench_large`. Single CPU core, synthetic invoices._\n"]

    # ---- 1) digital
    invs = [gen.make_invoice(rng, i * 3) for i in range(a.pages)]
    for i, inv in enumerate(invs):
        inv["invoice_number"] = f"INV-{i + 1:05d}"  # unique numbers: collisions would be (correct) duplicate flags
    t = time.perf_counter()
    pdf = Path(_tmp) / f"digital_{a.pages}p.pdf"
    pdf.write_bytes(gen.render_pdf_bytes(invs, [rng.choice("ABC") for _ in invs], [()] * len(invs)))
    gen_s = time.perf_counter() - t
    base_rss = rss_mb()
    d, exs, wall = run_doc(pdf, "pdf")
    by_no = {norm_key(e.invoice_number): e for e in exs}
    ok_no = sum(1 for i in invs if norm_key(i["invoice_number"]) in by_no)
    ok_total = sum(1 for i in invs if (e := by_no.get(norm_key(i["invoice_number"]))) and e.total is not None and abs(e.total - i["total"]) < 0.01)
    flagged = sum(e.needs_review for e in exs)
    lines += [f"## Digital PDF, {a.pages} pages ({pdf.stat().st_size / 1e6:.1f} MB)\n",
              "| Metric | Value |\n|---|---|",
              f"| Status | {d.status} |",
              f"| Pipeline wall time | **{wall:.1f} s** ({a.pages / wall:.0f} pages/s) |",
              f"| Invoices extracted | {len(exs)} of {a.pages} |",
              f"| Invoice numbers found / totals correct | {ok_no} / {ok_total} (of {a.pages}) |",
              f"| Flagged for review | {flagged} |",
              f"| Peak RSS (whole process) | {rss_mb():.0f} MB (baseline before processing {base_rss:.0f} MB) |"]

    # ---- 2) scanned
    n = a.scan_pages
    sinv = [gen.make_invoice(rng, 5000 + i) for i in range(n)]
    for i, inv in enumerate(sinv):
        inv["invoice_number"] = f"SCN-{i + 1:05d}"
    imgs = []
    for i in sinv:
        imgs.append(gen.degrade(gen.pdf_to_images(gen.render_pdf_bytes([i], ["A"], [()]))[0], rng, "clean"))
    spdf = Path(_tmp) / f"scan_{n}p.pdf"
    gen.images_to_pdf(imgs, spdf)
    d2, exs2, wall2 = run_doc(spdf, "pdf")
    by2 = {norm_key(e.invoice_number): e for e in exs2}
    ok2 = sum(1 for i in sinv if (e := by2.get(norm_key(i["invoice_number"]))) and e.total is not None and abs(e.total - i["total"]) < 0.01)
    per_page = wall2 / n
    lines += [f"\n## Scanned (image-only) PDF, {n} pages measured\n", "| Metric | Value |\n|---|---|",
              f"| Status | {d2.status} |", f"| Wall time (OCR on every page) | **{wall2:.1f} s** = {per_page:.2f} s/page |",
              f"| Invoices with correct number + total | {ok2} / {n} |",
              f"| **Extrapolated** to {a.pages} pages (linear, 1 core) | ~{per_page * a.pages / 60:.0f} min on 1 core; ~{per_page * a.pages / 60 / 4:.0f} min if split over 4 cores/workers |",
              f"| Peak RSS (whole process) | {rss_mb():.0f} MB |",
              "\nThe scanned figure above is an extrapolation, not a measurement of a 1000-page scan."]
    Path(a.out).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
