import time

import pytest

from app import services
from app.config import settings
from evaluation import generate_dataset as gen


def upload(client, *paths, headers=None):
    fs = [("files", (p.name, p.read_bytes())) for p in paths]
    return client.post("/api/documents", files=fs, headers=headers or {})


def wait(client, doc_id, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = client.get(f"/api/documents/{doc_id}/status").json()
        if s["status"] not in ("queued", "processing"):
            return s
        time.sleep(0.1)
    raise AssertionError(f"timeout; last={s}")


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_upload_process_and_fetch_results(client, files):
    r = upload(client, files["dir"] / "digital.pdf")
    assert r.status_code == 202
    doc_id = r.json()["documents"][0]["id"]
    st = wait(client, doc_id)
    assert st["status"] == "completed" and st["progress"] == 1.0
    res = client.get(f"/api/documents/{doc_id}/results").json()
    inv = res["invoices"][0]["data"]
    assert inv["invoice_number"] == files["inv"]["invoice_number"] and inv["total"] == files["inv"]["total"]
    assert client.get(f"/api/documents/{doc_id}").json()["events"]  # processing history recorded
    assert files["inv"]["invoice_number"] in client.get(f"/api/documents/{doc_id}/text").json()["extracted_text"]


def test_multi_file_upload_all_formats(client, files):
    names = ["inv.csv", "form.xlsx", "legacy.xls", "clean.png", "scan.pdf"]
    r = upload(client, *[files["dir"] / n for n in names])
    docs = r.json()["documents"]
    assert len(docs) == 5 and all(d["status"] == "queued" for d in docs)
    for d in docs:
        assert wait(client, d["id"])["status"] in ("completed", "needs_review"), d["filename"]


def test_unsupported_and_spoofed_files_rejected(client, tmp_path):
    exe = tmp_path / "evil.pdf"
    exe.write_bytes(b"MZ\x90\x00 this is an executable")
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    r = upload(client, exe, txt)
    assert r.status_code == 202 and all(d["status"] == "rejected" for d in r.json()["documents"])


def test_oversize_rejected(client, files, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    d = upload(client, files["dir"] / "digital.pdf").json()["documents"][0]
    assert d["status"] == "rejected" and "MB" in d["error"]


def test_too_many_files(client, files, monkeypatch):
    monkeypatch.setattr(settings, "max_files_per_request", 1)
    assert upload(client, files["dir"] / "inv.csv", files["dir"] / "form.xlsx").status_code == 413


def test_api_key_enforced(client, files, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret")
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200  # liveness stays open


def test_unknown_ids_404(client):
    assert client.get("/api/documents/nope").status_code == 404
    assert client.patch("/api/extractions/nope", json={"fields": {}}).status_code == 404


# ---------------------------------------------------------------- failures
def test_corrupt_file_ends_failed_with_message_and_reprocess_is_idempotent(client, tmp_path):
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"%PDF-1.4\n garbage")
    doc_id = upload(client, f).json()["documents"][0]["id"]
    st = wait(client, doc_id)
    assert st["status"] == "failed" and st["error_message"]
    assert client.get(f"/api/documents/{doc_id}/results").json()["invoices"] == []
    assert client.post(f"/api/documents/{doc_id}/reprocess").status_code == 202
    st2 = wait(client, doc_id)
    assert st2["status"] == "failed" and st2["attempts"] == 2


def test_transient_failure_recovers_on_reprocess(client, files, monkeypatch):
    real, calls = services._parse, {"n": 0}

    def flaky(doc, progress):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker crashed")
        return real(doc, progress)
    monkeypatch.setattr(services, "_parse", flaky)
    doc_id = upload(client, files["dir"] / "inv.csv").json()["documents"][0]["id"]
    assert wait(client, doc_id)["status"] == "failed"
    out = client.post("/api/documents/reprocess-failed").json()
    assert doc_id in out["requeued"]
    assert wait(client, doc_id)["status"] in ("completed", "needs_review")


def test_reprocess_rules(client, files):
    doc_id = upload(client, files["dir"] / "inv.csv").json()["documents"][0]["id"]
    wait(client, doc_id)
    assert client.post(f"/api/documents/{doc_id}/reprocess").status_code == 409  # not failed
    assert client.post(f"/api/documents/{doc_id}/reprocess?force=true").status_code == 202
    wait(client, doc_id)


def test_blank_image_fails_cleanly(client, tmp_path):
    from PIL import Image
    f = tmp_path / "blank.png"
    Image.new("RGB", (600, 800), "white").save(f)
    doc_id = upload(client, f).json()["documents"][0]["id"]
    st = wait(client, doc_id)
    assert st["status"] == "failed" and "No text" in st["error_message"]


# ---------------------------------------------------------------- validation, duplicates, corrections
def _inv_pdf(tmp_path, rng, name, **kw):
    inv = gen.make_invoice(rng, 3)
    inv.update(kw)
    p = tmp_path / name
    p.write_bytes(gen.render_pdf_bytes([inv], ["A"], [()]))
    return p, inv


def test_arithmetic_error_flags_review_and_correction_clears_it(client, tmp_path, rng):
    inv = gen.make_invoice(rng, 5)
    good_total = inv["total"]
    inv["total"] = round(good_total + 50, 2)
    p = tmp_path / "bad_total.pdf"
    p.write_bytes(gen.render_pdf_bytes([inv], ["A"], [()]))
    doc_id = upload(client, p).json()["documents"][0]["id"]
    assert wait(client, doc_id)["status"] == "needs_review"
    d = client.get(f"/api/documents/{doc_id}").json()
    ex = d["extractions"][0]
    assert any(i["code"] == "TOTAL_MISMATCH" for i in ex["issues"]) and d["needs_review"]

    r = client.patch(f"/api/extractions/{ex['id']}", json={"fields": {"total": good_total, "supplier_name": inv["supplier_name"]},
                                                         "reviewer": "qa"})  # supplier guess (no company suffix) also needs confirming
    assert r.status_code == 200 and r.json()["manually_corrected"] and not r.json()["needs_review"]
    d2 = client.get(f"/api/documents/{doc_id}").json()
    assert d2["status"] == "completed" and not d2["needs_review"]
    assert d2["extractions"][0]["field_confidence"]["total"] == 1.0


def test_correction_validation_errors(client, tmp_path, rng):
    p, _ = _inv_pdf(tmp_path, rng, "c.pdf")
    doc_id = upload(client, p).json()["documents"][0]["id"]
    wait(client, doc_id)
    ex = client.get(f"/api/documents/{doc_id}").json()["extractions"][0]
    assert client.patch(f"/api/extractions/{ex['id']}", json={"fields": {"total": "abc"}}).status_code == 422
    assert client.patch(f"/api/extractions/{ex['id']}", json={"fields": {"invoice_date": "31/31/2024"}}).status_code == 422
    assert client.patch(f"/api/extractions/{ex['id']}", json={"fields": {"hacker": "x"}}).status_code == 422


def test_duplicate_invoice_detected_even_in_other_format(client, tmp_path, rng):
    p1, inv = _inv_pdf(tmp_path, rng, "orig.pdf")
    first = upload(client, p1).json()["documents"][0]["id"]
    wait(client, first)
    csv = tmp_path / "dup.csv"
    gen.table_rows([inv]).to_csv(csv, index=False)
    second = upload(client, csv).json()["documents"][0]["id"]
    wait(client, second)
    ex = client.get(f"/api/documents/{second}").json()["extractions"][0]
    assert any(i["code"] == "DUPLICATE_INVOICE" for i in ex["issues"])
    first_ex = client.get(f"/api/documents/{first}").json()["extractions"][0]
    assert not any(i["code"] == "DUPLICATE_INVOICE" for i in first_ex["issues"])  # original stays clean


def test_concurrent_identical_uploads_flag_exactly_one(client, tmp_path, rng):
    p, _ = _inv_pdf(tmp_path, rng, "twin.pdf")
    ids = [d["id"] for d in upload(client, p, p, p).json()["documents"]]
    for i in ids:
        wait(client, i)
    flagged = [i for i in ids if any(x["code"] == "DUPLICATE_INVOICE"
               for x in client.get(f"/api/documents/{i}").json()["extractions"][0]["issues"])]
    assert len(flagged) == 2  # 1 original + 2 duplicates


def test_approve_clears_review_flag_but_keeps_issues(client, tmp_path, rng):
    inv = gen.make_invoice(rng, 9)
    inv["total"] += 20
    p = tmp_path / "appr.pdf"
    p.write_bytes(gen.render_pdf_bytes([inv], ["A"], [()]))
    doc_id = upload(client, p).json()["documents"][0]["id"]
    wait(client, doc_id)
    ex = client.get(f"/api/documents/{doc_id}").json()["extractions"][0]
    r = client.post(f"/api/extractions/{ex['id']}/approve").json()
    assert r["reviewed"] and not r["needs_review"] and r["issues"]
    assert client.get(f"/api/documents/{doc_id}").json()["status"] == "completed"


def test_multi_invoice_pdf_split(client, tmp_path, rng):
    invs = [gen.make_invoice(rng, 20 + i) for i in range(3)]
    p = tmp_path / "multi.pdf"
    p.write_bytes(gen.render_pdf_bytes(invs, ["B"] * 3, [()] * 3))
    doc_id = upload(client, p).json()["documents"][0]["id"]
    wait(client, doc_id)
    exs = client.get(f"/api/documents/{doc_id}/results").json()["invoices"]
    assert [e["data"]["invoice_number"] for e in exs] == [i["invoice_number"] for i in invs]


# ---------------------------------------------------------------- search & listing
def test_listing_filters_and_search_modes(client, tmp_path, rng):
    p, inv = _inv_pdf(tmp_path, rng, "searchme.pdf", supplier_name="Zyxwv Quasar Plumbing Ltd")
    doc_id = upload(client, p).json()["documents"][0]["id"]
    wait(client, doc_id)
    lst = client.get("/api/documents", params={"supplier": "quasar", "status": "completed"}).json()
    assert any(d["id"] == doc_id for d in lst["items"])
    assert not client.get("/api/documents", params={"supplier": "quasar", "min_total": inv["total"] + 1}).json()["items"]
    for mode in ("keyword", "semantic", "hybrid"):
        items = client.get("/api/search", params={"q": "quasar plumbing", "mode": mode}).json()["items"]
        assert any(d["id"] == doc_id for d in items), mode
    assert client.get("/api/search", params={"q": "zzzznotthere", "mode": "keyword"}).json()["items"] == []
    assert client.get("/api/search", params={"q": "x", "mode": "bogus"}).status_code == 422


def test_metrics_and_delete(client, tmp_path):
    m = client.get("/api/metrics").json()
    assert m["total"] > 0 and 0 <= m["success_rate"] <= 1 and m["failure_rate"] is not None
    f = tmp_path / "x.csv"
    f.write_text("Invoice Number,Date,Supplier,Total\nQ-1,2024-01-01,S,1.00\n")
    doc_id = upload(client, f).json()["documents"][0]["id"]
    wait(client, doc_id)
    assert client.delete(f"/api/documents/{doc_id}").status_code == 204
    assert client.get(f"/api/documents/{doc_id}").status_code == 404


def test_same_invoice_repeated_inside_one_pdf_is_flagged_and_does_not_crash(client, tmp_path, rng):
    """Regression: the 1000-page benchmark failed with 'can't compare offset-naive and offset-aware datetimes'
    when the same supplier+invoice number appeared twice (non-consecutively) in ONE document."""
    a, b = gen.make_invoice(rng, 41), gen.make_invoice(rng, 42)
    p = tmp_path / "abA.pdf"
    p.write_bytes(gen.render_pdf_bytes([a, b, dict(a)], ["A", "B", "C"], [(), (), ()]))
    doc_id = upload(client, p).json()["documents"][0]["id"]
    assert wait(client, doc_id)["status"] == "needs_review"
    exs = client.get(f"/api/documents/{doc_id}/results").json()["invoices"]
    assert len(exs) == 3
    codes = [[i["code"] for i in e["issues"]] for e in exs]
    assert "DUPLICATE_INVOICE" not in codes[0] and "DUPLICATE_INVOICE" in codes[2]


def test_error_messages_do_not_leak_server_paths(client, tmp_path):
    f = tmp_path / "broken2.pdf"
    f.write_bytes(b"%PDF-1.4\n garbage")
    doc_id = upload(client, f).json()["documents"][0]["id"]
    msg = wait(client, doc_id)["error_message"]
    assert msg and str(settings.storage_dir) not in msg and "/tmp" not in msg and ".bin" not in msg
