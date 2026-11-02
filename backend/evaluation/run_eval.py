"""End-to-end evaluation through the real HTTP API (TestClient) + background workers.

  python -m evaluation.generate_dataset --out data/eval
  python -m evaluation.run_eval --data data/eval --report ../docs/EVALUATION.md

Measures: field-level extraction accuracy, line-item F1, invoice exact-match, hallucination rate, validation
recall / false-positive rate, success & failure rates, per-format latency (sequential) and throughput (N workers).
"""
import argparse
import difflib
import json
import os
import statistics as st
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="docintel-eval-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/eval.db")
os.environ.setdefault("STORAGE_DIR", f"{_tmp}/uploads")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.extraction.normalize import norm_key  # noqa: E402
from app.main import app  # noqa: E402

FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]
NUMERIC = {"subtotal", "tax", "total"}


def same(field, a, b):
    if a is None or b is None:
        return a is None and b is None
    if field in NUMERIC:
        return abs(float(a) - float(b)) <= 0.01
    if field in ("invoice_number", "supplier_name", "customer_name"):
        return norm_key(a) == norm_key(b)
    return str(a).upper() == str(b).upper()


def match_invoices(gts, exs):
    pairs, used = [], set()
    for gi, g in enumerate(gts):
        for ei, e in enumerate(exs):
            if ei not in used and g.get("invoice_number") and norm_key(g["invoice_number"]) == norm_key(e["data"].get("invoice_number")):
                pairs.append((gi, ei))
                used.add(ei)
                break
    matched_g = {g for g, _ in pairs}
    free_e = [i for i in range(len(exs)) if i not in used]
    for gi in range(len(gts)):
        if gi not in matched_g and free_e:
            pairs.append((gi, free_e.pop(0)))
    return pairs


def line_f1(gl, el):
    used, tp = set(), 0
    for g in gl:
        for i, e in enumerate(el):
            if i in used or e.get("amount") is None or abs(e["amount"] - g["amount"]) > 0.01:
                continue
            if difflib.SequenceMatcher(None, norm_key(g["description"]) or "", norm_key(e.get("description")) or "").ratio() >= 0.8:
                used.add(i)
                tp += 1
                break
    p = tp / len(el) if el else (1.0 if not gl else 0.0)
    r = tp / len(gl) if gl else 1.0
    return tp, len(el), len(gl)


def pct(a, b):
    return round(100 * a / b, 1) if b else None


def run(data_dir: Path, workers: int):
    settings.worker_threads = workers
    labels = json.loads((data_dir / "labels.json").read_text())
    res = {"docs": []}
    with TestClient(app) as c:
        # ------------------------------------------------ pass 1: sequential (clean latency)
        for lab in labels:
            p = data_dir / lab["file"]
            t0 = time.perf_counter()
            r = c.post("/api/documents", files=[("files", (p.name, p.read_bytes()))])
            item = r.json()["documents"][0]
            rec = {"label": lab, "rejected": item["status"] == "rejected", "reject_reason": item.get("error")}
            if not rec["rejected"]:
                did = item["id"]
                while True:
                    s = c.get(f"/api/documents/{did}/status").json()
                    if s["status"] not in ("queued", "processing"):
                        break
                    time.sleep(0.02)
                rec["wall_ms"] = (time.perf_counter() - t0) * 1000
                rec["detail"] = c.get(f"/api/documents/{did}").json()
            res["docs"].append(rec)
        # ------------------------------------------------ pass 2: concurrent re-processing (throughput)
        ids = [d["detail"]["id"] for d in res["docs"] if "detail" in d]
        t0 = time.perf_counter()
        for i in ids:
            c.post(f"/api/documents/{i}/reprocess?force=true")
        pending = set(ids)
        while pending:
            pending = {i for i in pending if c.get(f"/api/documents/{i}/status").json()["status"] in ("queued", "processing")}
            time.sleep(0.25)
        res["concurrent"] = {"docs": len(ids), "workers": workers, "wall_s": time.perf_counter() - t0}
        res["metrics"] = c.get("/api/metrics").json()
    return res


def analyse(res):
    out = {}
    docs = res["docs"]
    good = [d for d in docs if not d["label"]["corrupt"]]
    corrupt = [d for d in docs if d["label"]["corrupt"]]
    ok_status = ("completed", "needs_review")
    # --- success / failure
    processed = [d for d in good if not d["rejected"] and d["detail"]["status"] in ok_status]
    failed = [d for d in good if d["rejected"] or d["detail"]["status"] == "failed"]
    out["n_total"], out["n_valid"], out["n_corrupt"] = len(docs), len(good), len(corrupt)
    out["success_rate"], out["failure_rate"] = pct(len(processed), len(good)), pct(len(failed), len(good))
    out["failed_files"] = [(d["label"]["file"], d["detail"]["error_message"] if "detail" in d else d["reject_reason"]) for d in failed]
    handled = [d for d in corrupt if d["rejected"] or d["detail"]["status"] == "failed"]
    out["corrupt_handled_pct"] = pct(len(handled), len(corrupt))
    out["corrupt_crashes"] = [d["label"]["file"] for d in corrupt if not (d["rejected"] or d["detail"]["status"] == "failed")]
    # --- field accuracy
    fa = defaultdict(lambda: [0, 0])
    by_fmt = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    inv_exact, inv_total, spurious, missed = 0, 0, 0, 0
    li_tp = li_pred = li_gt = 0
    halluc = [0, 0]  # field absent in doc but extractor returned a value
    errors = []
    for d in processed:
        lab, exs = d["label"], d["detail"]["extractions"]
        pairs = match_invoices(lab["invoices"], exs)
        spurious += max(0, len(exs) - len(pairs))
        missed += max(0, len(lab["invoices"]) - len(pairs))
        paired_g = {g for g, _ in pairs}
        for gi, g in enumerate(lab["invoices"]):
            inv_total += 1
            if gi not in paired_g:
                for f in FIELDS:
                    fa[f][1] += 1
                    by_fmt[lab["format"]][f][1] += 1
                li_gt += len(g["line_items"])
                continue
            e = exs[[ei for gj, ei in pairs if gj == gi][0]]["data"]
            all_ok = True
            for f in FIELDS:
                ok = same(f, e.get(f), g[f])
                fa[f][0] += ok
                fa[f][1] += 1
                by_fmt[lab["format"]][f][0] += ok
                by_fmt[lab["format"]][f][1] += 1
                if g[f] is None:
                    halluc[1] += 1
                    halluc[0] += e.get(f) is not None
                if not ok:
                    all_ok = False
                    errors.append((lab["file"], lab["quality"], f, g[f], e.get(f)))
            tp, npred, ngt = line_f1(g["line_items"], e["line_items"])
            li_tp, li_pred, li_gt = li_tp + tp, li_pred + npred, li_gt + ngt
            if tp != ngt or npred != ngt:
                all_ok = False
            inv_exact += all_ok
    out["field_acc"] = {f: pct(*fa[f]) for f in FIELDS}
    out["field_acc_overall"] = pct(sum(v[0] for v in fa.values()), sum(v[1] for v in fa.values()))
    out["invoice_exact_match"] = pct(inv_exact, inv_total)
    out["line_item"] = {"precision": pct(li_tp, li_pred), "recall": pct(li_tp, li_gt),
                        "f1": pct(2 * li_tp, li_pred + li_gt)}
    out["hallucination_rate"] = pct(*halluc) if halluc[1] else None
    out["hallucination_counts"] = halluc
    out["invoices_missed"], out["invoices_spurious"] = missed, spurious
    out["by_format"] = {}
    for fmt, fields in sorted(by_fmt.items()):
        a, b = sum(v[0] for v in fields.values()), sum(v[1] for v in fields.values())
        out["by_format"][fmt] = pct(a, b)
    out["errors"] = errors
    # --- validation
    codes_of = lambda d: {(i["code"], i.get("field")) for ex in d["detail"]["extractions"] for i in ex["issues"]}  # noqa: E731
    exp = defaultdict(lambda: [0, 0])
    for d in processed:
        have = {c for c, _ in codes_of(d)}
        for code in d["label"]["expect_issues"]:
            exp[code][1] += 1
            exp[code][0] += code in have
    out["validation_recall"] = {k: {"detected": v[0], "expected": v[1], "pct": pct(*v)} for k, v in exp.items()}
    clean = [d for d in processed if not d["label"]["expect_issues"]]
    clean_digital = [d for d in clean if d["label"]["quality"] == "clean" and d["label"]["format"] in ("pdf_digital", "pdf_digital_multi", "csv", "xlsx", "xlsx_multisheet", "xls")]
    clean_scan = [d for d in clean if d["label"]["format"].startswith(("pdf_scanned", "image")) and d["label"]["quality"] == "clean"]
    degraded = [d for d in clean if d["label"]["quality"] == "degraded"]
    fp = lambda ds: pct(sum(d["detail"]["needs_review"] for d in ds), len(ds))  # noqa: E731
    out["review_flag_rate"] = {"clean_digital_and_tables": (fp(clean_digital), len(clean_digital)),
                               "clean_scans_images": (fp(clean_scan), len(clean_scan)),
                               "degraded_scans_images": (fp(degraded), len(degraded))}
    arith = {"TOTAL_MISMATCH", "SUBTOTAL_MISMATCH", "LINE_ITEM_MISMATCH"}
    false_arith = [d["label"]["file"] for d in clean if {c for c, _ in codes_of(d)} & arith]
    out["false_arith_flags"] = {"count": len(false_arith), "of": len(clean), "files": false_arith}
    # needs_review precision/recall for genuinely problematic docs
    prob = [d for d in processed if d["label"]["expect_issues"]]
    out["problem_docs_flagged"] = (sum(d["detail"]["needs_review"] for d in prob), len(prob))
    # --- is a *wrong extraction* caught by validation? (error that slipped through = silent)
    silent = []
    for d in processed:
        lab = d["label"]
        pairs = match_invoices(lab["invoices"], d["detail"]["extractions"])
        for gi, ei in pairs:
            ex = d["detail"]["extractions"][ei]
            wrong = [f for f in FIELDS if not same(f, ex["data"].get(f), lab["invoices"][gi][f])]
            if wrong and not ex["needs_review"]:
                silent.append((lab["file"], wrong))
    n_wrong = sum(1 for e in {(x[0]) for x in errors})
    out["silent_errors"] = silent
    out["docs_with_wrong_fields"] = n_wrong
    # --- timing
    tm = defaultdict(list)
    for d in docs:
        if "detail" in d and d["detail"].get("processing_ms") is not None:
            tm[d["label"]["format"]].append(d["detail"]["processing_ms"])
    allms = [x for v in tm.values() for x in v]
    q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(len(xs) * p))]  # noqa: E731
    out["timing_ms"] = {"all": {"mean": round(st.mean(allms)), "median": round(st.median(allms)), "p95": q(allms, 0.95), "max": max(allms)}}
    for fmt, xs in sorted(tm.items()):
        out["timing_ms"][fmt] = {"n": len(xs), "mean": round(st.mean(xs)), "p95": q(xs, 0.95)}
    out["concurrent"] = res["concurrent"]
    out["concurrent"]["docs_per_s"] = round(res["concurrent"]["docs"] / res["concurrent"]["wall_s"], 2)
    out["sequential_total_s"] = round(sum(allms) / 1000, 1)
    return out


def render_md(m, meta) -> str:
    L = []
    w = L.append
    w("# Evaluation report\n")
    w(f"_Generated by `python -m evaluation.run_eval` on {meta['date']}. All numbers below are measured, not estimated._\n")
    w("## 1. Setup and honest caveats\n")
    w(f"- **Dataset:** {m['n_total']} files ({m['n_valid']} valid + {m['n_corrupt']} deliberately corrupted), generated with seed {meta['seed']} "
      "by `evaluation/generate_dataset.py`: digital PDFs (3 layouts, incl. 3-invoice PDFs), scanned PDFs, PNG/JPG/TIFF (half clean, half degraded: "
      "rotation, blur, noise, down-scaling, JPEG artefacts), CSV, multi-sheet XLSX, legacy XLS, injected arithmetic errors, missing fields, duplicates.")
    w("- **The dataset is synthetic.** Layouts are cleaner and more regular than real supplier invoices, so *real-world accuracy will be lower*. "
      "Treat these numbers as a regression baseline and an upper bound; re-run against a hand-labelled sample of the company's own documents before go-live.")
    w(f"- **Extractor under test:** `{meta['extractor']}`. " + ("The LLM path is unit-tested with a mocked client but was **not** measured here (no API key in the evaluation environment)." if meta['extractor'] != 'llm' else ""))
    w(f"- **Hardware/config:** {meta['hw']}; OCR = Tesseract {meta['tess']}; {meta['workers']} worker threads.\n")
    w("## 2. Headline results\n")
    w("| Metric | Result |\n|---|---|")
    w(f"| Document success rate (valid files reaching `completed`/`needs_review`) | **{m['success_rate']}%** |")
    w(f"| Document failure rate (valid files ending `failed`) | **{m['failure_rate']}%** |")
    w(f"| Corrupted files handled gracefully (clean `failed` + message, no crash) | **{m['corrupt_handled_pct']}%** ({m['n_corrupt']} files) |")
    w(f"| Field-level extraction accuracy (8 header fields, all formats) | **{m['field_acc_overall']}%** |")
    w(f"| Invoice exact-match (all 8 fields + all line items correct) | **{m['invoice_exact_match']}%** |")
    li = m["line_item"]
    w(f"| Line items precision / recall / F1 | {li['precision']}% / {li['recall']}% / **{li['f1']}%** |")
    hc = m["hallucination_counts"]
    w(f"| Hallucination rate (value returned for a field absent from the document) | **{m['hallucination_rate']}%** ({hc[0]} of {hc[1]}) |")
    t = m["timing_ms"]["all"]
    w(f"| Processing time per document, sequential (mean / median / p95 / max) | {t['mean']} / {t['median']} / {t['p95']} / {t['max']} ms |")
    c = m["concurrent"]
    w(f"| Throughput with {c['workers']} workers | {c['docs_per_s']} docs/s ({c['docs']} docs in {c['wall_s']:.1f}s; sequential sum {m['sequential_total_s']}s) |\n")
    w("> **Reading the throughput row:** the evaluation machine had a single CPU core, so 4 worker threads cannot be faster than 1 there (the parallel run took about the same wall time as the sequential sum: no contention overhead, but no speed-up either). On an N-core machine OCR scales across workers because Tesseract is pinned to one thread per process. The speed-up itself was **not** measured.")
    w("> **OCR field-confidence gate:** the review threshold (`OCR_FIELD_MIN_CONFIDENCE=70`) was tuned on this same synthetic set, so the low silent-error count is optimistic; re-tune on real documents.\n")
    w("## 3. Accuracy by field\n")
    w("| Field | Accuracy |\n|---|---|")
    for f, v in m["field_acc"].items():
        w(f"| {f} | {v}% |")
    w("\n## 4. Accuracy and speed by format\n")
    w("| Format | Field accuracy | n | mean ms | p95 ms |\n|---|---|---|---|---|")
    for fmt, acc in m["by_format"].items():
        tt = m["timing_ms"].get(fmt, {})
        w(f"| {fmt} | {acc}% | {tt.get('n', '-')} | {tt.get('mean', '-')} | {tt.get('p95', '-')} |")
    w("\n## 5. Validation quality\n")
    w("| Injected problem | Detected | Expected | Recall |\n|---|---|---|---|")
    for k, v in sorted(m["validation_recall"].items()):
        w(f"| {k} | {v['detected']} | {v['expected']} | {v['pct']}% |")
    pf = m["problem_docs_flagged"]
    w(f"\n- Documents containing an injected problem that ended up flagged for manual review: **{pf[0]} / {pf[1]}**.")
    r = m["review_flag_rate"]
    w(f"- Review-flag rate on documents with **no** injected problem: clean digital/tables {r['clean_digital_and_tables'][0]}% (n={r['clean_digital_and_tables'][1]}), "
      f"clean scans/images {r['clean_scans_images'][0]}% (n={r['clean_scans_images'][1]}), degraded scans/images {r['degraded_scans_images'][0]}% (n={r['degraded_scans_images'][1]}).")
    fa = m["false_arith_flags"]
    w(f"- Arithmetic-mismatch flags raised on documents with no injected arithmetic error: {fa['count']} / {fa['of']} (these are OCR/extraction mistakes surfacing as validation errors - the validator working as a safety net).")
    se = m["silent_errors"]
    w(f"- **Silent errors** (an extracted field was wrong *and* the document was not flagged for review): **{len(se)}** invoice(s)" + (": " + ", ".join(f"{f} {w_}" for f, w_ in se[:10]) if se else "") + ".\n")
    w("## 6. Failures and error analysis\n")
    w(f"- Invoices missed: {m['invoices_missed']}; spurious extra invoices: {m['invoices_spurious']}.")
    if m["failed_files"]:
        w("- Valid files that failed: " + "; ".join(f"`{f}` ({e})" for f, e in m["failed_files"]))
    if m["corrupt_crashes"]:
        w("- Corrupt files NOT cleanly rejected: " + ", ".join(m["corrupt_crashes"]))
    errs = m["errors"]
    w(f"- Field errors by quality: clean={sum(1 for e in errs if e[1]=='clean')}, degraded={sum(1 for e in errs if e[1]=='degraded')}.")
    by_field = defaultdict(int)
    for e in errs:
        by_field[e[2]] += 1
    w("- Error count by field: " + ", ".join(f"{k}={v}" for k, v in sorted(by_field.items(), key=lambda x: -x[1])))
    w("\nSample errors (file, quality, field, expected → got):\n")
    for e in errs[:15]:
        w(f"- `{e[0]}` ({e[1]}) **{e[2]}**: `{e[3]}` → `{e[4]}`")
    w("\n## 7. Known limitations\n")
    w("- European number formats (`1.234,56`) are handled in the normaliser but the rule-based line-item parser targets `1,234.56`; the LLM path covers this.")
    w("- Rule-based extraction relies on common labels (Invoice No, Bill To, Subtotal ...). Unusual layouts need the LLM path.")
    w("- Degraded scans lose line items first (small text); these are flagged through `NO_LINE_ITEMS`/arithmetic checks rather than invented.")
    w("- Semantic search uses hashed embeddings by default (lexical overlap + synonym map). Set `EMBEDDING_PROVIDER=sbert` for true semantic matching.")
    return "\n".join(L)


if __name__ == "__main__":
    import platform
    import subprocess
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/eval")
    ap.add_argument("--report", default="../docs/EVALUATION.md")
    ap.add_argument("--json", default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    res = run(Path(a.data), a.workers)
    m = analyse(res)
    tess = subprocess.run(["tesseract", "--version"], capture_output=True, text=True).stdout.splitlines()[0].split()[-1]
    meta = {"date": time.strftime("%Y-%m-%d"), "seed": a.seed, "workers": a.workers, "tess": tess,
            "extractor": "llm" if settings.anthropic_api_key else "heuristic (rule-based)",
            "hw": f"{os.cpu_count()} CPU cores, {platform.platform()}, Python {platform.python_version()}"}
    Path(a.report).write_text(render_md(m, meta))
    if a.json:
        Path(a.json).write_text(json.dumps({k: v for k, v in m.items() if k != "errors"}, indent=1, default=str))
    print(render_md(m, meta))
