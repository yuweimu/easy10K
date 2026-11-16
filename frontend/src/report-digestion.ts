import "./reports-admin.css";
import "./report-digestion.css";

type Report = Record<string, string | null> & { pdf_hash: string; digestion_stage: string };
type ReportPage = { items: Report[]; page: number; page_size: number; total: number; total_pages: number };
const rows = document.querySelector<HTMLTableSectionElement>("#reports")!;
const previous = document.querySelector<HTMLButtonElement>("#previous")!;
const next = document.querySelector<HTMLButtonElement>("#next")!;
const refresh = document.querySelector<HTMLButtonElement>("#refresh")!;
const size = document.querySelector<HTMLSelectElement>("#page-size")!;
const summary = document.querySelector<HTMLElement>("#summary")!;
const label = document.querySelector<HTMLElement>("#page-label")!;
const error = document.querySelector<HTMLElement>("#error")!;
let page = 1, totalPages = 1, loading = false;

function cell(value: string | null | undefined, mono = false): HTMLTableCellElement {
  const td = document.createElement("td");
  td.textContent = value || "—";
  if (value) td.title = value;
  if (mono) td.className = "mono";
  return td;
}

function textDetails(value: string | null | undefined, title: string): HTMLTableCellElement {
  const td = cell(null);
  td.className = "digestion-content";
  if (value) {
    const details = document.createElement("details");
    const heading = document.createElement("summary");
    heading.textContent = title;
    const text = document.createElement("pre");
    text.textContent = value;
    details.append(heading, text);
    td.replaceChildren(details);
  }
  return td;
}

function render(items: Report[]): void {
  rows.replaceChildren();
  for (const report of items) {
    const tr = document.createElement("tr");
    const hash = cell(report.pdf_hash, true);
    hash.textContent = report.pdf_hash.slice(0, 6);
    const stage = cell(report.digestion_stage);
    const badge = document.createElement("span");
    badge.className = `badge ${report.digestion_stage.endsWith("Error") ? "error" : report.digestion_stage.endsWith("Success") ? "done" : "pending"}`;
    badge.textContent = report.digestion_stage;
    stage.replaceChildren(badge);
    const download = cell(report.download_url);
    if (report.download_url) {
      try {
        const url = new URL(report.download_url);
        if (["http:", "https:"].includes(url.protocol)) {
          const link = document.createElement("a");
          link.href = url.href;
          link.textContent = "Download PDF ↗";
          link.title = report.download_url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          download.replaceChildren(link);
        }
      } catch { /* Invalid URLs remain plain text. */ }
    }
    const local = cell(report.local_path, true);
    if (report.local_path) {
      const filename = report.local_path.split(/[\\/]/).pop() || report.local_path;
      local.textContent = filename.length > 16 ? `${filename.slice(0, 16)}…` : filename;
      const open = document.createElement("a");
      const localUrl = new URL("file:///");
      localUrl.pathname = report.local_path.startsWith("/")
        ? report.local_path
        : `${import.meta.env.VITE_PROJECT_DIR}${report.local_path}`;
      open.href = localUrl.href;
      open.textContent = local.textContent;
      open.title = report.local_path;
      open.target = "_blank";
      open.rel = "noopener noreferrer";
      local.replaceChildren(open);
      const path = decodeURIComponent(localUrl.pathname);
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-path";
      copy.textContent = "Copy";
      copy.title = "Copy full PDF local path";
      copy.setAttribute("aria-label", `Copy PDF local path: ${path}`);
      const feedback = document.createElement("span");
      feedback.className = "copy-feedback";
      feedback.setAttribute("role", "status");
      const copyControl = document.createElement("span");
      copyControl.className = "copy-control";
      copyControl.append(copy, feedback);
      let feedbackTimer: ReturnType<typeof setTimeout> | undefined;
      copy.addEventListener("click", async () => {
        clearTimeout(feedbackTimer);
        copy.disabled = true;
        feedback.textContent = "";
        try {
          await navigator.clipboard.writeText(path);
          feedback.textContent = "Copied";
        } catch {
          feedback.textContent = "Copy failed";
        } finally {
          copy.disabled = false;
          feedbackTimer = setTimeout(() => { feedback.textContent = ""; }, 2000);
        }
      });
      local.classList.add("pdf-local-path");
      local.append(copyControl);
    }
    const content = cell(null);
    content.className = "digestion-content";
    const fields = [
      ["balance_sheet", "Balance sheet"], ["balance_sheet_ex", "Balance sheet (extended)"],
      ["income_statement", "Income statement"], ["income_statement_ex", "Income statement (extended)"],
      ["cash_flow_statement", "Cash flow statement"], ["cash_flow_statement_ex", "Cash flow statement (extended)"],
    ];
    for (const [key, title] of fields) {
      // Keep the three main statements discoverable even before extraction.
      if (!report[key] && key.endsWith("_ex")) continue;
      if (!content.querySelector("details")) content.replaceChildren();
      const details = document.createElement("details");
      const heading = document.createElement("summary");
      heading.textContent = title;
      const text = document.createElement("pre");
      text.textContent = report[key] || "Not extracted yet.";
      details.append(heading, text);
      content.append(details);
    }
    const date = report.updated_at ? new Date(report.updated_at) : null;
    tr.append(hash, stage, download, local,
      cell(report.stock_code, true), cell(report.stock_name), cell(report.reporting_date, true),
      cell(report.pdf_creation_date, true),
      textDetails(report.extraction_report, "View report"),
      textDetails(report.extraction_audit, "View audit"), content,
      cell(date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : report.updated_at));
    rows.append(tr);
  }
  if (!items.length) {
    const td = cell("No report digestion records yet.");
    td.colSpan = 12;
    td.className = "empty";
    const tr = document.createElement("tr");
    tr.append(td);
    rows.append(tr);
  }
}

async function load(requestedPage = page): Promise<void> {
  if (loading) return;
  loading = true;
  previous.disabled = next.disabled = refresh.disabled = size.disabled = true;
  error.hidden = true;
  summary.textContent = "Loading reports…";
  rows.closest("table")!.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(`/api/report-digestion?page=${requestedPage}&page_size=${size.value}`);
    if (!response.ok) throw new Error(`Unable to load report digestion (HTTP ${response.status}). Check the API, database, and schema.`);
    const data: ReportPage = await response.json();
    page = data.page;
    totalPages = data.total_pages;
    render(data.items);
    const start = data.total ? (page - 1) * data.page_size + 1 : 0;
    summary.textContent = `${start}–${start ? start + data.items.length - 1 : 0} of ${data.total.toLocaleString()} reports`;
    label.textContent = `Page ${page} of ${totalPages}`;
  } catch (reason) {
    error.textContent = reason instanceof Error ? reason.message : "Unable to load reports.";
    error.hidden = false;
    summary.textContent = rows.children.length ? "Showing previously loaded reports" : "Reports unavailable";
  } finally {
    loading = false;
    previous.disabled = page <= 1;
    next.disabled = page >= totalPages;
    refresh.disabled = size.disabled = false;
    rows.closest("table")!.setAttribute("aria-busy", "false");
  }
}
previous.addEventListener("click", () => void load(page - 1));
next.addEventListener("click", () => void load(page + 1));
refresh.addEventListener("click", () => void load());
size.addEventListener("change", () => void load(1));
document.addEventListener("keydown", event => {
  if (event.target instanceof HTMLElement && event.target.closest("input, select, textarea, [contenteditable]")) return;
  if (event.key === "PageDown" && !next.disabled) { event.preventDefault(); void load(page + 1); }
  if (event.key === "PageUp" && !previous.disabled) { event.preventDefault(); void load(page - 1); }
});
void load();
