"""Field-level OCR confidence: an extracted value is only as trustworthy as the OCR words it came from.
On the evaluation set every mis-read invoice number had a word confidence <= 68 while the median correct one
scored 91, so low word confidence is a strong, cheap signal for 'send to a human'. The default threshold (70) was
chosen on that same synthetic set - re-tune it on your own documents (OCR_FIELD_MIN_CONFIDENCE)."""
from .normalize import norm_key

TEXT_FIELDS = ("invoice_number", "supplier_name", "customer_name")
NUM_FIELDS = ("subtotal", "tax", "total")


def _token_conf(token: str, words) -> float | None:
    k = norm_key(token)
    if not k:
        return None
    hits = [c for w, c in words if (wk := norm_key(w)) and (wk == k or (len(wk) >= 3 and (k in wk or wk in k)))]
    return min(hits) if hits else None


def apply_ocr_word_confidence(data: dict, conf: dict, words: list, threshold: float) -> list[str]:
    """Lower `conf[field]` to 0.5 (=> LOW_CONFIDENCE => manual review) when the OCR words behind the value are
    below `threshold` (0-100). Mutates conf; returns human-readable notes."""
    notes = []
    if not words:
        return notes
    for f in TEXT_FIELDS + NUM_FIELDS:
        v = data.get(f)
        if v is None:
            continue
        if f in NUM_FIELDS:
            cands = [f"{v:,.2f}", f"{v:.2f}"]
            confs = [c for w, c in words if any(x in w.replace(" ", "") for x in cands)]
            mc = min(confs) if confs else None
        else:
            cs = [c for c in (_token_conf(t, words) for t in str(v).split()) if c is not None]
            mc = min(cs) if cs else None
        if mc is not None and mc < threshold:
            conf[f] = min(conf.get(f, 1.0), 0.5)
            notes.append(f"{f}: low OCR word confidence ({mc:.0f}) - verify against the original")
    return notes
