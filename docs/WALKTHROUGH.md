# Walkthrough recording script (about 6-8 minutes)

> The brief requires a recorded walkthrough. A recording cannot be produced by the AI - record this yourself
> (Loom/OBS) following the script below.

1. **(30 s) Intro** - problem, what the system does. Show `docs/ARCHITECTURE.md` diagram.
2. **(1 min) Start-up** - `uvicorn app.main:app`, `npm run dev`; open http://localhost:8000/docs briefly.
3. **(1.5 min) Upload** - drag in a mix from `backend/data/eval/`: `digital_000.pdf`, `scan_001.pdf`, `image_014.jpg`, `table_000.csv`, `form_000.xlsx`, `legacy_000.xls`. Show progress bars/stages updating live.
4. **(1 min) Inspect** - open one invoice: fields, confidence dots, line items, raw text tab, processing history.
5. **(1.5 min) Validation** - upload `anomaly_000.pdf`, `missing_000.pdf`, `duplicate_000.pdf`: show red issues (TOTAL_MISMATCH, MISSING_FIELD, DUPLICATE_INVOICE), "needs review" badge.
6. **(1 min) Correct** - fix the total, "Save corrections & re-validate" -> status turns completed. Mention audit log (`corrections` table).
7. **(45 s) Failures** - upload `corrupt_truncated.pdf`: shows Failed + reason; "Retry all failed".
8. **(45 s) Search** - keyword vs semantic vs hybrid; filters (supplier, status, needs review).
9. **(1 min) Quality** - run `pytest` (96 passed), show `docs/EVALUATION.md` headline table and honest caveats, `docs/BENCHMARK_LARGE.md`.
10. **(30 s) Wrap-up** - limitations (synthetic data, LLM path untested live, queue seam) and next steps (pgvector, Celery, auth).
