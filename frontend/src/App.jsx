import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";

const STATUS_LABEL = {
  queued: "Queued", processing: "Processing", completed: "Completed", needs_review: "Needs review", failed: "Failed",
};
const STAGE_LABEL = {
  queued: "Waiting", extracting_text: "Reading text / OCR", extracting_fields: "Extracting fields",
  validating: "Validating", indexing: "Indexing", done: "Done",
};

const fmtMoney = (v, cur) => (v == null ? "—" : `${cur ? cur + " " : ""}${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2 })}`);
const fmtBytes = (n) => (n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " KB");

function Badge({ status }) {
  return <span className={`badge ${status}`}>{STATUS_LABEL[status] || status}</span>;
}

function Progress({ doc }) {
  if (!["queued", "processing"].includes(doc.status)) return null;
  const pct = Math.round((doc.progress || 0) * 100);
  return (
    <div className="progress" title={`${pct}%`}>
      <div className="bar" style={{ width: `${Math.max(pct, 4)}%` }} />
      <span>{STAGE_LABEL[doc.stage] || doc.stage} · {pct}%</span>
    </div>
  );
}

/* ---------------------------------------------------------------- Upload */
function Upload({ onUploaded }) {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const input = useRef();

  const send = async (fileList) => {
    const files = [...fileList];
    if (!files.length) return;
    setBusy(true);
    setMsg(null);
    try {
      const out = await api.upload(files);
      const rejected = out.documents.filter((d) => d.status === "rejected");
      setMsg({
        ok: rejected.length === 0,
        text: `${out.documents.length - rejected.length} queued` + (rejected.length ? `, ${rejected.length} rejected: ` + rejected.map((r) => `${r.filename} (${r.error})`).join("; ") : ""),
      });
      onUploaded();
    } catch (e) {
      setMsg({ ok: false, text: e.message });
    } finally {
      setBusy(false);
      input.current.value = "";
    }
  };

  return (
    <div
      className={`dropzone ${over ? "over" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); send(e.dataTransfer.files); }}
      onClick={() => input.current.click()}
    >
      <input ref={input} type="file" multiple hidden accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.csv" onChange={(e) => send(e.target.files)} />
      <strong>{busy ? "Uploading…" : "Drop files here or click to browse"}</strong>
      <small>PDF · JPG · PNG · TIFF · XLSX · XLS · CSV — multiple files supported</small>
      {msg && <div className={msg.ok ? "ok" : "err"}>{msg.text}</div>}
    </div>
  );
}

/* ---------------------------------------------------------------- List */
function DocList({ docs, selected, onSelect, searchMode }) {
  if (!docs.length) return <div className="empty">{searchMode ? "No matching documents." : "No documents yet. Upload some to get started."}</div>;
  return (
    <ul className="doclist">
      {docs.map((d) => (
        <li key={d.id} className={selected === d.id ? "sel" : ""} onClick={() => onSelect(d.id)}>
          <div className="row1">
            <span className="fname" title={d.filename}>{d.filename}</span>
            <Badge status={d.status} />
          </div>
          <div className="row2">
            <span>{d.file_type.toUpperCase()} · {fmtBytes(d.size_bytes)}</span>
            {d.invoice_number && <span>#{d.invoice_number}</span>}
            {d.supplier_name && <span>{d.supplier_name}</span>}
            {d.total != null && <span>{fmtMoney(d.total, d.currency)}</span>}
            {d.invoice_count > 1 && <span>{d.invoice_count} invoices</span>}
            {d.issue_count > 0 && <span className="warn">⚠ {d.issue_count}</span>}
            {d.score != null && <span className="score">score {Number(d.score).toFixed(2)}</span>}
          </div>
          {d.snippet && <div className="snippet">{d.snippet}</div>}
          <Progress doc={d} />
          {d.status === "failed" && <div className="err small">{d.error_message}</div>}
        </li>
      ))}
    </ul>
  );
}

/* ---------------------------------------------------------------- Detail */
const HEADER_FIELDS = [
  ["invoice_number", "Invoice number", "text"], ["invoice_date", "Invoice date", "date"],
  ["supplier_name", "Supplier", "text"], ["customer_name", "Customer", "text"],
  ["subtotal", "Subtotal", "number"], ["tax", "Tax", "number"], ["total", "Total", "number"], ["currency", "Currency", "text"],
];

function ConfidenceDot({ c }) {
  if (c == null) return null;
  const cls = c >= 0.85 ? "hi" : c >= 0.6 ? "mid" : "lo";
  return <i className={`dot ${cls}`} title={`confidence ${(c * 100).toFixed(0)}%`} />;
}

function InvoiceEditor({ ex, onChanged }) {
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setForm({ ...ex.data, line_items: (ex.data.line_items || []).map((l) => ({ ...l })) });
    setErr(null);
  }, [ex.id, ex.data]);

  if (!form) return null;
  const dirty = JSON.stringify(form) !== JSON.stringify(ex.data);
  const setF = (k, v) => setForm((f) => ({ ...f, [k]: v === "" ? null : v }));
  const setLine = (i, k, v) => setForm((f) => ({ ...f, line_items: f.line_items.map((l, j) => (j === i ? { ...l, [k]: v === "" ? null : v } : l)) }));

  const save = async () => {
    setSaving(true); setErr(null);
    const changed = {};
    for (const [k] of HEADER_FIELDS) if (form[k] !== ex.data[k]) changed[k] = form[k];
    if (JSON.stringify(form.line_items) !== JSON.stringify(ex.data.line_items)) changed.line_items = form.line_items;
    try { await api.correct(ex.id, changed, "web-user"); onChanged(); } catch (e) { setErr(e.message); } finally { setSaving(false); }
  };
  const approve = async () => { try { await api.approve(ex.id); onChanged(); } catch (e) { setErr(e.message); } };

  return (
    <section className={`invoice ${ex.needs_review ? "review" : ""}`}>
      <header>
        <h3>Invoice {ex.seq + 1} <small>{ex.source_ref} · {ex.method}</small></h3>
        <div>
          {ex.manually_corrected && <span className="tag">corrected</span>}
          {ex.reviewed && <span className="tag ok">approved</span>}
          {ex.needs_review && <span className="tag warn">needs review</span>}
        </div>
      </header>

      {ex.issues.length > 0 && (
        <ul className="issues">
          {ex.issues.map((i) => (
            <li key={i.id} className={i.severity}>
              <b>{i.code}</b> {i.message}
            </li>
          ))}
        </ul>
      )}

      <div className="grid">
        {HEADER_FIELDS.map(([k, label, type]) => (
          <label key={k} className={form[k] == null ? "missing" : ""}>
            <span>{label} <ConfidenceDot c={ex.field_confidence?.[k]} /></span>
            <input
              type={type === "date" ? "text" : type === "number" ? "number" : "text"} step="0.01"
              placeholder={type === "date" ? "YYYY-MM-DD" : "(not found)"}
              value={form[k] ?? ""}
              onChange={(e) => setF(k, type === "number" && e.target.value !== "" ? parseFloat(e.target.value) : e.target.value)}
            />
          </label>
        ))}
      </div>

      <table className="lines">
        <thead><tr><th>Description</th><th>Qty</th><th>Unit price</th><th>Amount</th><th /></tr></thead>
        <tbody>
          {form.line_items.map((l, i) => (
            <tr key={i}>
              <td><input value={l.description ?? ""} onChange={(e) => setLine(i, "description", e.target.value)} /></td>
              {["quantity", "unit_price", "amount"].map((k) => (
                <td key={k}><input type="number" step="0.01" value={l[k] ?? ""} onChange={(e) => setLine(i, k, e.target.value === "" ? "" : parseFloat(e.target.value))} /></td>
              ))}
              <td><button className="ghost" onClick={() => setForm((f) => ({ ...f, line_items: f.line_items.filter((_, j) => j !== i) }))}>✕</button></td>
            </tr>
          ))}
          {form.line_items.length === 0 && <tr><td colSpan="5" className="empty">No line items extracted</td></tr>}
        </tbody>
      </table>
      <button className="ghost" onClick={() => setForm((f) => ({ ...f, line_items: [...f.line_items, { description: "", quantity: null, unit_price: null, amount: null }] }))}>+ Add line</button>

      {err && <div className="err">{err}</div>}
      <div className="actions">
        <button className="primary" disabled={!dirty || saving} onClick={save}>{saving ? "Saving…" : "Save corrections & re-validate"}</button>
        {ex.needs_review && !dirty && <button onClick={approve}>Approve as is</button>}
        {dirty && <button className="ghost" onClick={() => setForm({ ...ex.data, line_items: ex.data.line_items.map((l) => ({ ...l })) })}>Reset</button>}
      </div>
    </section>
  );
}

function Detail({ id, onChanged }) {
  const [doc, setDoc] = useState(null);
  const [tab, setTab] = useState("fields");
  const [text, setText] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try { setDoc(await api.get(id)); setErr(null); } catch (e) { setErr(e.message); }
  }, [id]);

  useEffect(() => { setDoc(null); setText(null); setTab("fields"); load(); }, [id, load]);
  useEffect(() => {
    if (!doc || !["queued", "processing"].includes(doc.status)) return;
    const t = setInterval(load, 1000);
    return () => clearInterval(t);
  }, [doc?.status, load]);
  useEffect(() => { if (tab === "text" && text === null) api.text(id).then((r) => setText(r.extracted_text)).catch(() => setText("")); }, [tab, id, text]);

  if (err) return <div className="err">{err}</div>;
  if (!doc) return <div className="empty">Loading…</div>;
  const changed = () => { load(); onChanged(); };

  return (
    <div className="detail">
      <header className="dhead">
        <div>
          <h2>{doc.filename}</h2>
          <div className="meta">
            <Badge status={doc.status} /> {doc.file_type.toUpperCase()} · {fmtBytes(doc.size_bytes)} · {doc.page_count ?? "?"} page(s)
            {doc.extraction_method && <> · via {doc.extraction_method}</>}
            {doc.ocr_confidence != null && <> · OCR {doc.ocr_confidence}%</>}
            {doc.processing_ms != null && <> · {(doc.processing_ms / 1000).toFixed(1)}s</>}
          </div>
        </div>
        <div className="actions">
          {doc.status === "failed" && <button className="primary" onClick={async () => { await api.reprocess(doc.id); changed(); }}>Reprocess</button>}
          {["completed", "needs_review"].includes(doc.status) && <button className="ghost" title="Re-run extraction (discards manual corrections)" onClick={async () => { if (confirm("Re-run processing? Manual corrections will be lost.")) { await api.reprocess(doc.id, true); changed(); } }}>Re-run</button>}
          <button className="ghost danger" onClick={async () => { if (confirm("Delete this document?")) { await api.remove(doc.id); onChanged(true); } }}>Delete</button>
        </div>
      </header>

      <Progress doc={doc} />
      {doc.status === "failed" && <div className="err">Processing failed: {doc.error_message}</div>}
      {doc.document_issues.map((i) => <div key={i.id} className="err">{i.code}: {i.message}</div>)}

      <nav className="tabs">
        {[["fields", `Extracted data (${doc.extractions.length})`], ["text", "Raw text"], ["history", "Processing history"]].map(([k, l]) => (
          <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{l}</button>
        ))}
      </nav>

      {tab === "fields" && (doc.extractions.length ? doc.extractions.map((e) => <InvoiceEditor key={e.id} ex={e} onChanged={changed} />) : <div className="empty">No structured data yet.</div>)}
      {tab === "text" && <pre className="rawtext">{text ?? "Loading…"}</pre>}
      {tab === "history" && (
        <table className="history">
          <thead><tr><th>Time</th><th>Stage</th><th>Status</th><th>Detail</th><th>ms</th></tr></thead>
          <tbody>{doc.events.map((e, i) => (
            <tr key={i} className={e.status}><td>{new Date(e.created_at).toLocaleTimeString()}</td><td>{e.stage}</td><td>{e.status}</td><td>{e.message}</td><td>{e.duration_ms ?? ""}</td></tr>
          ))}</tbody>
        </table>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- App */
export default function App() {
  const [docs, setDocs] = useState([]);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState(null);
  const [q, setQ] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [filters, setFilters] = useState({ status: "", file_type: "", needs_review: "" });
  const [metrics, setMetrics] = useState(null);
  const [error, setError] = useState(null);
  const searching = q.trim().length > 0;

  const refresh = useCallback(async () => {
    try {
      const f = { ...filters };
      const out = searching
        ? await api.search({ q, mode, status: f.status, file_type: f.file_type, needs_review: f.needs_review })
        : await api.list({ ...f, limit: 100 });
      setDocs(out.items); setTotal(out.total ?? out.items.length); setError(null);
      setMetrics(await api.metrics());
    } catch (e) { setError(e.message); }
  }, [q, mode, filters, searching]);

  useEffect(() => { const t = setTimeout(refresh, searching ? 250 : 0); return () => clearTimeout(t); }, [refresh, searching]);
  const active = docs.some((d) => ["queued", "processing"].includes(d.status));
  useEffect(() => { if (!active) return; const t = setInterval(refresh, 1500); return () => clearInterval(t); }, [active, refresh]);

  const pending = docs.filter((d) => ["queued", "processing"].includes(d.status)).length;

  return (
    <div className="app">
      <header className="top">
        <h1>📄 Document Intelligence</h1>
        {metrics && (
          <div className="stats">
            <span>{metrics.total} docs</span>
            <span>{metrics.by_status?.needs_review || 0} need review</span>
            <span className={metrics.by_status?.failed ? "warn" : ""}>{metrics.by_status?.failed || 0} failed</span>
            {metrics.success_rate != null && <span>{(metrics.success_rate * 100).toFixed(0)}% success</span>}
            {pending > 0 && <span className="live">● {pending} in progress</span>}
          </div>
        )}
      </header>
      {error && <div className="err banner">API error: {error}</div>}
      <main>
        <aside>
          <Upload onUploaded={refresh} />
          <div className="searchbar">
            <input placeholder="Search supplier, invoice #, text…" value={q} onChange={(e) => setQ(e.target.value)} />
            <select value={mode} onChange={(e) => setMode(e.target.value)} title="Search mode">
              <option value="hybrid">Hybrid</option><option value="keyword">Keyword</option><option value="semantic">Semantic</option>
            </select>
          </div>
          <div className="filters">
            <select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}>
              <option value="">Any status</option>{Object.entries(STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <select value={filters.file_type} onChange={(e) => setFilters({ ...filters, file_type: e.target.value })}>
              <option value="">Any type</option>{["pdf", "image", "xlsx", "xls", "csv"].map((t) => <option key={t} value={t}>{t.toUpperCase()}</option>)}
            </select>
            <select value={filters.needs_review} onChange={(e) => setFilters({ ...filters, needs_review: e.target.value })}>
              <option value="">Any review state</option><option value="true">Needs review</option><option value="false">No review needed</option>
            </select>
          </div>
          <div className="listhead">
            <span>{total} result(s){searching ? ` · ${mode} search` : ""}</span>
            <button className="ghost" onClick={async () => { const r = await api.reprocessFailed(); alert(`${r.requeued.length} failed document(s) re-queued`); refresh(); }}>Retry all failed</button>
          </div>
          <DocList docs={docs} selected={selected} onSelect={setSelected} searchMode={searching} />
        </aside>
        <section className="content">
          {selected ? <Detail id={selected} onChanged={(deleted) => { if (deleted) setSelected(null); refresh(); }} /> : <div className="empty big">Select a document to inspect extracted data, validation errors and history.</div>}
        </section>
      </main>
    </div>
  );
}
