import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api.js", () => ({
  api: {
    upload: vi.fn(), list: vi.fn(), get: vi.fn(), text: vi.fn(), search: vi.fn(), correct: vi.fn(),
    approve: vi.fn(), reprocess: vi.fn(), reprocessFailed: vi.fn(), remove: vi.fn(), metrics: vi.fn(),
  },
}));
import { api } from "./api.js";
import App from "./App.jsx";

const summary = (o = {}) => ({
  id: "d1", filename: "inv1.pdf", file_type: "pdf", size_bytes: 2048, status: "needs_review", stage: "done", progress: 1,
  error_message: null, needs_review: true, page_count: 1, extraction_method: "text_layer", ocr_confidence: null,
  attempts: 1, processing_ms: 120, invoice_count: 1, invoice_number: "INV-1", supplier_name: "Acme Ltd", total: 110,
  currency: "USD", issue_count: 1, created_at: "2024-01-01T00:00:00Z", updated_at: "2024-01-01T00:00:00Z", ...o,
});
const detail = (o = {}) => ({
  ...summary(), document_issues: [], events: [{ stage: "processing", status: "ok", message: "1 invoice(s)", duration_ms: 120, created_at: "2024-01-01T00:00:00Z" }],
  extractions: [{
    id: "e1", seq: 0, source_ref: "page 1", method: "heuristic", needs_review: true, reviewed: false, manually_corrected: false,
    field_confidence: { total: 0.9 },
    data: { invoice_number: "INV-1", invoice_date: "2024-01-02", supplier_name: "Acme Ltd", customer_name: null, subtotal: 100, tax: 10, total: 125, currency: "USD",
            line_items: [{ description: "Widget", quantity: 2, unit_price: 50, amount: 100 }] },
    issues: [{ id: 1, code: "TOTAL_MISMATCH", severity: "error", field: "total", message: "Subtotal + tax != total" }],
  }], ...o,
});

beforeEach(() => {
  vi.clearAllMocks();
  window.alert = vi.fn();
  window.confirm = vi.fn(() => true);
  api.list.mockResolvedValue({ total: 1, items: [summary()] });
  api.metrics.mockResolvedValue({ total: 1, by_status: { needs_review: 1 }, success_rate: 1, failure_rate: 0 });
  api.get.mockResolvedValue(detail());
  api.search.mockResolvedValue({ mode: "hybrid", items: [summary({ id: "d2", filename: "found.pdf", score: 0.9, snippet: "…Acme…" })] });
});

describe("Document Intelligence UI", () => {
  it("lists documents with status badge and metrics", async () => {
    render(<App />);
    expect(await screen.findByText("inv1.pdf")).toBeInTheDocument();
    expect(screen.getAllByText("Needs review").length).toBeGreaterThan(0);
    expect(await screen.findByText(/1 need review/)).toBeInTheDocument();
  });

  it("uploads multiple files in one request and reports rejected ones", async () => {
    api.upload.mockResolvedValue({ documents: [{ id: "a", filename: "a.pdf", status: "queued" }, { filename: "x.exe", status: "rejected", error: "Unsupported" }] });
    const { container } = render(<App />);
    await screen.findByText("inv1.pdf");
    const files = [new File(["%PDF"], "a.pdf"), new File(["x"], "x.exe")];
    await userEvent.upload(container.querySelector('input[type="file"]'), files, { applyAccept: false });
    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(1));
    expect(api.upload.mock.calls[0][0]).toHaveLength(2);
    expect(await screen.findByText(/1 queued, 1 rejected/)).toBeInTheDocument();
  });

  it("shows live progress for a processing document", async () => {
    api.list.mockResolvedValue({ total: 1, items: [summary({ status: "processing", stage: "extracting_text", progress: 0.4, needs_review: false })] });
    render(<App />);
    expect(await screen.findByText(/Reading text \/ OCR · 40%/)).toBeInTheDocument();
  });

  it("opens a document, shows validation errors and saves a correction", async () => {
    api.correct.mockResolvedValue({});
    render(<App />);
    await userEvent.click(await screen.findByText("inv1.pdf"));
    expect(await screen.findByText("TOTAL_MISMATCH")).toBeInTheDocument();
    const save = screen.getByRole("button", { name: /save corrections/i });
    expect(save).toBeDisabled();
    const total = screen.getByDisplayValue("125");
    await userEvent.clear(total);
    await userEvent.type(total, "110");
    expect(save).toBeEnabled();
    await userEvent.click(save);
    await waitFor(() => expect(api.correct).toHaveBeenCalledWith("e1", { total: 110 }, "web-user"));
  });

  it("highlights fields that were not found", async () => {
    render(<App />);
    await userEvent.click(await screen.findByText("inv1.pdf"));
    const label = (await screen.findByText(/Customer/, { selector: "span" })).closest("label");
    expect(label).toHaveClass("missing");
  });

  it("approves a flagged invoice as-is", async () => {
    api.approve.mockResolvedValue({});
    render(<App />);
    await userEvent.click(await screen.findByText("inv1.pdf"));
    await userEvent.click(await screen.findByRole("button", { name: /approve as is/i }));
    expect(api.approve).toHaveBeenCalledWith("e1");
  });

  it("searches with the selected mode", async () => {
    render(<App />);
    await screen.findByText("inv1.pdf");
    await userEvent.type(screen.getByPlaceholderText(/search supplier/i), "acme");
    await waitFor(() => expect(api.search).toHaveBeenCalled());
    const last = api.search.mock.calls.at(-1)[0];
    expect(last.q).toBe("acme");
    expect(last.mode).toBe("hybrid");
    expect(await screen.findByText("found.pdf")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByTitle("Search mode"), "semantic");
    await waitFor(() => expect(api.search.mock.calls.at(-1)[0].mode).toBe("semantic"));
  });

  it("failed document shows the error and a working Reprocess button", async () => {
    api.list.mockResolvedValue({ total: 1, items: [summary({ status: "failed", error_message: "Cannot open PDF", needs_review: false })] });
    api.get.mockResolvedValue(detail({ status: "failed", error_message: "Cannot open PDF", extractions: [] }));
    api.reprocess.mockResolvedValue({});
    render(<App />);
    await userEvent.click(await screen.findByText("inv1.pdf"));
    expect(await screen.findByText(/Processing failed: Cannot open PDF/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reprocess" }));
    expect(api.reprocess).toHaveBeenCalledWith("d1");
  });

  it("shows an API error banner when the backend is down", async () => {
    api.list.mockRejectedValue(new Error("Network down"));
    render(<App />);
    expect(await screen.findByText(/API error: Network down/)).toBeInTheDocument();
  });
});
