# AI-Powered Document Intelligence System

Upload invoices/receipts as **PDF, JPG, PNG, TIFF, XLSX, XLS or CSV** (digital or scanned, multi-page, multi-sheet, up to
~1000 pages). The system reads them (text layer / OCR / spreadsheet parser), extracts structured invoice JSON
(never inventing values), validates the arithmetic, detects duplicates, flags records for manual review, and makes
everything searchable through a REST API and a React UI.
Recording Walkthrough Link:https://drive.google.com/file/d/19lnDv7eO59a3EiBA6JsX4mSwXeiKYmBH/view?usp=sharing
| Task in the brief | Where |
|---|---|
| 1 Document processing | `backend/app/pipeline/` (`pdf.py`, `ocr.py`, `image.py`, `spreadsheet.py`) |
| 2 AI information extraction | `backend/app/extraction/` (LLM tool-use + grounding, rule-based fallback, prompts) |
| 3 Data validation | `backend/app/validation.py` |
| 4 Backend API + background jobs | `backend/app/main.py`, `jobs.py`, `services.py` |
| 5 Database + keyword/semantic search | `backend/app/models.py`, `search.py` |
| 6 Frontend | `frontend/` (React + Vite) |
| 7 Tests + evaluation | `backend/tests/` (96 tests) + `frontend/src/App.test.jsx` (9 tests), `backend/evaluation/` |
| Docs / diagrams / schema | `docs/ARCHITECTURE.md` |

Documents: [Architecture & schema](docs/ARCHITECTURE.md) · [Evaluation report](docs/EVALUATION.md) ·
[Large-PDF benchmark](docs/BENCHMARK_LARGE.md) · [Test results](docs/TEST_RESULTS.txt) · [Prompts](docs/PROMPTS.md) ·
[AI-assistance disclosure](docs/AI_DISCLOSURE.md) · [Walkthrough script](docs/WALKTHROUGH.md)

## Quick start (no Docker)
Requirements: Python 3.11+, Node 18+, **Tesseract OCR** (`apt install tesseract-ocr` / `brew install tesseract`).

```bash
# backend  (http://localhost:8000, docs at /docs)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env            # optional; export the variables or use your shell
export ANTHROPIC_API_KEY=sk-...    # optional - without it the rule-based extractor is used
uvicorn app.main:app --reload

# frontend (http://localhost:5173, proxies /api -> :8000)
cd frontend
npm install
npm run dev
```

## Docker (PostgreSQL + backend + nginx UI)
```bash
docker compose up --build        # UI http://localhost:8080 , API http://localhost:8000/docs
```
> The compose/PostgreSQL path was written but **not executed** in the authoring environment (no Docker/PostgreSQL there).
> SQLite (default) is what the tests and evaluation ran on. Verify the compose stack once before relying on it.

## Try it
```bash
cd backend
python -m evaluation.generate_dataset --out data/eval --seed 42      # 133 labelled sample files
curl -F "files=@data/eval/digital_000.pdf" -F "files=@data/eval/table_000.csv" http://localhost:8000/api/documents
```
Or drag the files from `backend/data/eval/` into the UI. Files such as `anomaly_*.pdf`, `missing_*.pdf`,
`duplicate_*.pdf`, `corrupt_*` demonstrate validation, review flags and failure handling.

## REST API (full interactive docs at `/docs`)
| Method & path | Purpose |
|---|---|
| `POST /api/documents` | Upload many files (multipart `files`). `202` + per-file result; processing is asynchronous |
| `GET /api/documents` | List + filter (`status, file_type, needs_review, supplier, customer, invoice_number, currency, date_from/to, min/max_total, q`) |
| `GET /api/documents/{id}` | Detail: extractions, validation issues, processing history |
| `GET /api/documents/{id}/status` | `status, stage, progress (0-1), error_message, attempts` |
| `GET /api/documents/{id}/results` | Structured invoice JSON(s) (`409` while still processing) |
| `GET /api/documents/{id}/text` | Extracted raw text |
| `GET /api/search?q=&mode=keyword\|semantic\|hybrid` | Search with the same filters |
| `PATCH /api/extractions/{id}` | Manual correction `{fields:{...}, reviewer}` - audited, re-validated |
| `POST /api/extractions/{id}/approve` | Human sign-off of a flagged record |
| `POST /api/documents/{id}/reprocess` / `POST /api/documents/reprocess-failed` | Re-queue failed jobs |
| `DELETE /api/documents/{id}` · `GET /api/metrics` · `GET /health` | housekeeping / success & failure rates |

Document status: `queued → processing → completed | needs_review | failed`.

## Configuration (environment variables, see `.env.example`)
`DATABASE_URL`, `STORAGE_DIR`, `API_KEY` (enables `X-API-Key` auth), `CORS_ORIGINS`, `WORKER_THREADS`, `MAX_UPLOAD_MB`,
`ANTHROPIC_API_KEY`, `LLM_MODEL`, `EMBEDDING_PROVIDER` (`hash`|`sbert`), `OCR_*`.

## Tests
```bash
cd backend
pytest                                   # 96 tests: parsing, OCR, spreadsheets, extraction (LLM mocked), validation, API, failures
python -m evaluation.generate_dataset --out data/eval
python -m evaluation.run_eval --data data/eval --report ../docs/EVALUATION.md    # measured accuracy / time / success rate
python -m evaluation.bench_large --pages 1000                                     # 1000-page benchmark
cd ../frontend && npm test && npm run build   # 9 UI tests + production build
```

## Honest limitations
- The evaluation set is **synthetic**; expect lower accuracy on real-world supplier layouts. Re-run `run_eval` with your own labelled samples.
- The **live LLM path was not exercised against the real API** (no key during development) - it is covered by mocked-client tests (hallucination removal, fallback, coercion). Test it with your key first.
- Job queue is an in-process worker pool with DB-persisted state (restart-safe, single node). Swap `jobs.enqueue` for Celery/RQ/BullMQ to scale out (see Architecture).
- Hash embeddings are largely lexical; use `EMBEDDING_PROVIDER=sbert` and/or pgvector for true semantic search at scale.
- No user accounts/RBAC, TLS, or rate limiting (put behind a reverse proxy/IdP for production).
- The screen-recorded walkthrough must be recorded by you - a script is in `docs/WALKTHROUGH.md`.
