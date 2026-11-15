import "./reports-admin.css";

type Report = {
  stock_code: string | null;
  reporting_date: string | null;
  pdf_creation_time: string;
  digestion_status: string;
  updated_at: string | null;
  pdf_hash: string;
  pdf_download_url: string | null;
  pdf_local_path: string | null;
  markdown_file: string | null;
  preprocessed_file: string | null;
};
type RegistryPage = { items: Report[]; page: number; page_size: number; total: number; total_pages: number };

const rows = document.querySelector<HTMLTableSectionElement>("#reports")!;
const previous = document.querySelector<HTMLButtonElement>("#previous")!;
const next = document.querySelector<HTMLButtonElement>("#next")!;
const refresh = document.querySelector<HTMLButtonElement>("#refresh")!;
const size = document.querySelector<HTMLSelectElement>("#page-size")!;
const summary = document.querySelector<HTMLElement>("#summary")!;
const label = document.querySelector<HTMLElement>("#page-label")!;
const error = document.querySelector<HTMLElement>("#error")!;
let page = 1;
let totalPages = 1;
let loading = false;

function cell(value: string | null, mono = false): HTMLTableCellElement {
  const td = document.createElement("td");
  td.textContent = value || "—";
  if (value) td.title = value;
  if (mono) td.className = "mono";
  return td;
}

function timestamp(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function render(items: Report[]): void {
  rows.replaceChildren();
  for (const report of items) {
    const tr = document.createElement("tr");
    const status = cell(null);
    const badge = document.createElement("span");
    badge.className = `badge ${["PENDING", "ERROR", "DONE"].includes(report.digestion_status) ? report.digestion_status.toLowerCase() : ""}`;
    badge.textContent = report.digestion_status;
    status.replaceChildren(badge);
    const download = cell(report.pdf_download_url);
    if (report.pdf_download_url) {
      try {
        const url = new URL(report.pdf_download_url);
        if (["https:", "http:"].includes(url.protocol)) {
          const link = document.createElement("a");
          link.href = url.href;
          link.textContent = report.pdf_download_url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          download.replaceChildren(link);
        }
      } catch { /* Local markers are displayed as plain text. */ }
    }
    const hash = cell(report.pdf_hash, true);
    hash.textContent = report.pdf_hash.slice(0, 6);
    const markdown = cell(report.markdown_file, true);
    if (report.markdown_file) markdown.textContent = report.markdown_file.split(/[\\/]/).pop() || report.markdown_file;
    const pdf = cell(report.pdf_local_path, true);
    if (report.pdf_local_path) {
      const filename = report.pdf_local_path.split(/[\\/]/).pop() || report.pdf_local_path;
      pdf.textContent = filename.length > 16 ? `${filename.slice(0, 16)}…` : filename;
    }
    tr.append(hash, cell(report.stock_code, true), cell(report.reporting_date, true),
      status,
      download, pdf, markdown,
      cell(report.preprocessed_file, true), cell(timestamp(report.updated_at)));
    rows.append(tr);
  }
  if (!items.length) {
    const td = cell("No reports registered yet.");
    td.colSpan = 9;
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
    const response = await fetch(`/api/report-registry?page=${requestedPage}&page_size=${size.value}`);
    if (!response.ok) throw new Error(`Unable to load report registry (HTTP ${response.status}). Check that the API and database are available, then refresh.`);
    const data: RegistryPage = await response.json();
    page = data.page;
    totalPages = data.total_pages;
    render(data.items);
    const start = data.total ? (page - 1) * data.page_size + 1 : 0;
    const end = start ? start + data.items.length - 1 : 0;
    summary.textContent = `${start}–${end} of ${data.total.toLocaleString()} reports`;
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
document.addEventListener("keydown", (event) => {
  if (event.target instanceof HTMLElement && event.target.closest("input, select, textarea, [contenteditable]")) return;
  if (event.key === "PageDown" && !next.disabled) { event.preventDefault(); void load(page + 1); }
  if (event.key === "PageUp" && !previous.disabled) { event.preventDefault(); void load(page - 1); }
});
void load();
