// Thin API client. Set VITE_API_KEY if the backend has API_KEY enabled.
const BASE = import.meta.env.VITE_API_URL || "";
const KEY = import.meta.env.VITE_API_KEY || "";

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, {
    ...opts,
    headers: { ...(KEY ? { "X-API-Key": KEY } : {}), ...(opts.headers || {}) },
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

const qs = (o) =>
  Object.entries(o)
    .filter(([, v]) => v !== "" && v !== null && v !== undefined)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");

export const api = {
  upload(files) {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    return req("/api/documents", { method: "POST", body: fd });
  },
  list: (params) => req(`/api/documents?${qs(params)}`),
  get: (id) => req(`/api/documents/${id}`),
  text: (id) => req(`/api/documents/${id}/text`),
  search: (params) => req(`/api/search?${qs(params)}`),
  correct: (extId, fields, reviewer) =>
    req(`/api/extractions/${extId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields, reviewer }),
    }),
  approve: (extId) => req(`/api/extractions/${extId}/approve`, { method: "POST" }),
  reprocess: (id, force = false) => req(`/api/documents/${id}/reprocess?force=${force}`, { method: "POST" }),
  reprocessFailed: () => req("/api/documents/reprocess-failed", { method: "POST" }),
  remove: (id) => req(`/api/documents/${id}`, { method: "DELETE" }),
  metrics: () => req("/api/metrics"),
};
