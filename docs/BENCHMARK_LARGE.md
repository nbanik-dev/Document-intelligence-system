# Large-document benchmark

_Measured by `python -m evaluation.bench_large`. Single CPU core, synthetic invoices._

## Digital PDF, 1000 pages (0.9 MB)

| Metric | Value |
|---|---|
| Status | needs_review |
| Pipeline wall time | **3.3 s** (301 pages/s) |
| Invoices extracted | 1000 of 1000 |
| Invoice numbers found / totals correct | 1000 / 1000 (of 1000) |
| Flagged for review | 24 |
| Peak RSS (whole process) | 257 MB (baseline before processing 218 MB) |

## Scanned (image-only) PDF, 40 pages measured

| Metric | Value |
|---|---|
| Status | needs_review |
| Wall time (OCR on every page) | **19.5 s** = 0.49 s/page |
| Invoices with correct number + total | 40 / 40 |
| **Extrapolated** to 1000 pages (linear, 1 core) | ~8 min on 1 core; ~2 min if split over 4 cores/workers |
| Peak RSS (whole process) | 888 MB |

The scanned figure above is an extrapolation, not a measurement of a 1000-page scan.
