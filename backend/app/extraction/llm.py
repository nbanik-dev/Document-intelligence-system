"""LLM extraction (Anthropic tool-use for guaranteed JSON) + grounding checks that remove hallucinations."""
import logging

from ..config import settings
from . import heuristic
from .normalize import (ISO_CURRENCIES, clean_str, new_invoice, norm_key, parse_date, parse_money,
                        parse_number)
from .prompts import SYSTEM_PROMPT, TOOL, USER_TEMPLATE

log = logging.getLogger(__name__)


def call_llm(text: str) -> dict:
    """Single network call - isolated so tests can monkeypatch it."""
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=settings.llm_timeout_s, max_retries=3)
    resp = client.messages.create(
        model=settings.llm_model, max_tokens=4096, temperature=0, system=SYSTEM_PROMPT, tools=[TOOL],
        tool_choice={"type": "tool", "name": TOOL["name"]},
        messages=[{"role": "user", "content": USER_TEMPLATE.format(text=text[: settings.llm_max_chars])}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return dict(block.input)
    raise ValueError("LLM returned no structured output")


def _coerce(raw: dict) -> tuple[dict, dict, str | None]:
    inv = new_invoice()
    conf_in = raw.get("confidence") or {}
    conf = {}
    inv["invoice_number"] = clean_str(raw.get("invoice_number"))
    inv["supplier_name"] = clean_str(raw.get("supplier_name"))
    inv["customer_name"] = clean_str(raw.get("customer_name"))
    inv["invoice_date"], _ = parse_date(raw.get("invoice_date"))
    for f in ("subtotal", "tax", "total"):
        inv[f] = parse_money(raw.get(f))
    cur = clean_str(raw.get("currency"))
    inv["currency"] = cur.upper() if cur and cur.upper() in ISO_CURRENCIES else None
    for li in raw.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        item = {"description": clean_str(li.get("description")), "quantity": parse_number(li.get("quantity")),
                "unit_price": parse_money(li.get("unit_price")), "amount": parse_money(li.get("amount"))}
        if any(v is not None for v in item.values()):
            inv["line_items"].append(item)
    for f in list(inv.keys()):
        if f == "line_items":
            conf[f] = float(conf_in.get(f, 0.8)) if inv[f] else 0.0
        else:
            c = conf_in.get(f)
            conf[f] = 0.0 if inv[f] is None else max(0.0, min(1.0, float(c if isinstance(c, (int, float)) else 0.7)))
    return inv, conf, raw.get("notes")


def _num_in_text(v: float, text_nocomma: str) -> bool:
    forms = {f"{v:.2f}", f"{v:.1f}", f"{v:g}", f"{int(v)}" if float(v).is_integer() else f"{v:.2f}"}
    return any(f in text_nocomma for f in forms)


def ground(inv: dict, conf: dict, text: str) -> list[str]:
    """Verify every LLM value against the source text. Strings not found are dropped (set to None);
    numbers not found keep the value but get low confidence so they land in manual review."""
    notes = []
    tkey = norm_key(text) or ""
    tnc = text.replace(",", "").replace(" ", "")
    for f in ("invoice_number", "supplier_name", "customer_name"):
        if inv[f] and (norm_key(inv[f]) or "~") not in tkey:
            notes.append(f"{f} '{inv[f]}' not found in source text - discarded")
            inv[f], conf[f] = None, 0.0
    for f in ("subtotal", "tax", "total"):
        if inv[f] is not None and not _num_in_text(inv[f], tnc):
            notes.append(f"{f} {inv[f]} not found verbatim in source text")
            conf[f] = min(conf.get(f, 0.4), 0.4)
    if inv["invoice_date"] and inv["invoice_date"][:4] not in text:
        notes.append("invoice_date year not found in source text")
        conf["invoice_date"] = min(conf.get("invoice_date", 0.4), 0.4)
    if inv["currency"] and inv["currency"] not in text and not any(s in text for s in "$€£¥₹"):
        notes.append("currency not supported by source text")
        conf["currency"] = min(conf.get("currency", 0.4), 0.4)
    kept = []
    for li in inv["line_items"]:
        if li["amount"] is not None and not _num_in_text(li["amount"], tnc):
            notes.append(f"line item '{li['description']}' amount {li['amount']} not in source text - dropped")
            continue
        kept.append(li)
    inv["line_items"] = kept
    return notes


def extract_llm(text: str) -> tuple[dict, dict, list[str]]:
    raw = call_llm(text)
    inv, conf, llm_note = _coerce(raw)
    notes = ground(inv, conf, text)
    if llm_note:
        notes.insert(0, f"model note: {llm_note}")
    return inv, conf, notes


def extract_text(text: str) -> tuple[dict, dict, str, list[str]]:
    """Orchestrator: LLM when configured, rule-based otherwise or on any LLM failure."""
    if settings.anthropic_api_key:
        try:
            inv, conf, notes = extract_llm(text)
            return inv, conf, "llm", notes
        except Exception as e:  # noqa: BLE001  network, quota, malformed output ...
            log.warning("LLM extraction failed, falling back to heuristics: %s", e)
            inv, conf = heuristic.extract_invoice(text)
            return inv, conf, "heuristic_fallback", [f"LLM unavailable ({type(e).__name__}); rule-based extraction used"]
    inv, conf = heuristic.extract_invoice(text)
    return inv, conf, "heuristic", []
