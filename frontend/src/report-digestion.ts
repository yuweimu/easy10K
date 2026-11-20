import "./reports-admin.css";
import "./report-digestion.css";

type Report = Record<string, string | null> & { pdf_hash: string; digestion_stage: string };
type ReportPage = { items: Report[]; page: number; page_size: number; total: number; total_pages: number };
const rows = document.querySelector<HTMLTableSectionElement>("#reports")!;
const previous = document.querySelector<HTMLButtonElement>("#previous")!;
const next = document.querySelector<HTMLButtonElement>("#next")!;
const refresh = document.querySelector<HTMLButtonElement>("#refresh")!;
const size = document.querySelector<HTMLSelectElement>("#page-size")!;
const reportTabs = Array.from(document.querySelectorAll<HTMLButtonElement>('.report-tabs [role="tab"]'));
const reportPanel = document.querySelector<HTMLElement>("#report-panel")!;
const searchForm = document.querySelector<HTMLFormElement>("#report-search")!;
const searchFields = document.querySelector<HTMLFieldSetElement>("#search-fields")!;
const searchHash = document.querySelector<HTMLInputElement>("#search-pdf-hash")!;
const searchStock = document.querySelector<HTMLInputElement>("#search-stock-code")!;
const searchStage = document.querySelector<HTMLSelectElement>("#search-stage")!;
let searchFilters: Record<string, string> = {};
let reportGroup = "InProgress";
const summary = document.querySelector<HTMLElement>("#summary")!;
const label = document.querySelector<HTMLElement>("#page-label")!;
const error = document.querySelector<HTMLElement>("#error")!;
let page = 1, totalPages = 1, loading = false;
const stages = ["Downloaded", "PreviewSuccess", "PreviewError", "ExtractionSuccess", "ExtractionError",
  "AuditPass", "AuditIssues", "ManualPass", "Deprecated", "NoStatements", "BalanceSheetOK", "BalanceSheetErr",
  "IncomStatementOK", "IncomeStatementErr", "CashFlowStatementOK", "CashFlowStatementErr"];
for (const stage of stages) {
  searchStage.add(new Option(stage, stage));
}

function stageEditor(report: Report): HTMLDivElement {
  const editor = document.createElement("div");
  editor.className = "stage-editor";
  const select = document.createElement("select");
  select.setAttribute("aria-label", `Stage for report ${report.pdf_hash}`);
  for (const value of stages) {
    const option = document.createElement("option");
    option.value = option.textContent = value;
    select.append(option);
  }
  select.value = report.digestion_stage;
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save";
  save.disabled = true;
  save.setAttribute("aria-label", `Save stage for report ${report.pdf_hash}`);
  const feedback = document.createElement("span");
  feedback.setAttribute("role", "status");
  feedback.className = "stage-feedback";
  select.addEventListener("change", () => {
    save.disabled = select.value === report.digestion_stage;
    feedback.textContent = "";
  });
  save.addEventListener("click", async () => {
    select.disabled = save.disabled = true;
    feedback.textContent = "Saving…";
    try {
      const response = await fetch(`/api/report-digestion/${encodeURIComponent(report.pdf_hash)}/stage`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ digestion_stage: select.value }),
      });
      if (!response.ok) throw new Error(`Unable to save stage (HTTP ${response.status}).`);
      const updated: Pick<Report, "digestion_stage" | "updated_at"> = await response.json();
      Object.assign(report, updated);
      await load();
      feedback.textContent = "Saved";
      summary.textContent += " · Stage saved";
    } catch (reason) {
      feedback.textContent = reason instanceof Error ? reason.message : "Unable to save stage.";
    } finally {
      select.disabled = false;
      save.disabled = select.value === report.digestion_stage;
    }
  });
  editor.append(select, save, feedback);
  return editor;
}

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
    const copyControl = document.createElement("span");
    copyControl.className = "copy-control";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "copy-path";
    copy.textContent = "Copy";
    copy.title = `Copy ${title.toLowerCase().replace(/^view /, "")}`;
    copy.setAttribute("aria-label", copy.title);
    const feedback = document.createElement("span");
    feedback.className = "copy-feedback";
    feedback.setAttribute("role", "status");
    let feedbackTimer: ReturnType<typeof setTimeout> | undefined;
    copy.addEventListener("click", async event => {
      event.preventDefault();
      event.stopPropagation();
      clearTimeout(feedbackTimer);
      copy.disabled = true;
      feedback.textContent = "";
      try {
        await navigator.clipboard.writeText(value);
        feedback.textContent = "Copied";
      } catch {
        feedback.textContent = "Copy failed";
      } finally {
        copy.disabled = false;
        feedbackTimer = setTimeout(() => { feedback.textContent = ""; }, 2000);
      }
    });
    copyControl.append(copy, feedback);
    heading.append(copyControl);
    const text = document.createElement("pre");
    text.textContent = value;
    details.append(heading, text);
    td.replaceChildren(details);
  }
  return td;
}

function auditCell(value: string | null | undefined): HTMLTableCellElement {
  const td = cell(null);
  td.className = "digestion-content";
  if (value) {
    const text = document.createElement("pre");
    try {
      const audit: unknown = JSON.parse(value);
      if (audit && typeof audit === "object" && !Array.isArray(audit) && "statements" in audit) {
        const { statements, ...visible } = audit;
        text.textContent = JSON.stringify(visible, null, 2);
        const details = document.createElement("details");
        const heading = document.createElement("summary");
        heading.textContent = "statements";
        const statementText = document.createElement("pre");
        statementText.textContent = JSON.stringify(statements, null, 2);
        details.append(heading, statementText);
        td.replaceChildren(text, details);
        return td;
      }
      text.textContent = JSON.stringify(audit, null, 2);
    } catch {
      text.textContent = value;
    }
    td.replaceChildren(text);
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
    const hasIssues = report.digestion_stage.endsWith("Error") || report.digestion_stage === "AuditIssues"
      || report.digestion_stage.endsWith("Err");
    const passed = report.digestion_stage.endsWith("Success") || report.digestion_stage.endsWith("OK")
      || ["AuditPass", "ManualPass"].includes(report.digestion_stage);
    badge.className = `badge ${hasIssues ? "error" : passed ? "done" : "pending"}`;
    badge.textContent = report.digestion_stage;
    if (report.digestion_stage === "ManualPass") badge.title = "Manually marked as passed";
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
      const projectParts = (import.meta.env.VITE_PROJECT_DIR as string).split("/").filter(Boolean);
      const fileParts = path.split("/").filter(Boolean);
      let shared = 0;
      while (shared < projectParts.length && projectParts[shared] === fileParts[shared]) shared++;
      const relativePath = "../".repeat(projectParts.length - shared) + fileParts.slice(shared).join("/");
      for (const [buttonLabel, copyValue, description] of [
        ["F", path, "Copy full PDF local path"],
        ["R", relativePath, "Copy PDF path relative to project directory"],
      ]) {
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-path";
      copy.textContent = buttonLabel;
      copy.title = `${description}: ${copyValue}`;
      copy.setAttribute("aria-label", `${description}: ${copyValue}`);
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
          await navigator.clipboard.writeText(copyValue);
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
    }
    const files = cell(null);
    files.className = "pdf-files";
    const sourceLine = document.createElement("div");
    sourceLine.className = "pdf-source-line";
    const localLine = document.createElement("div");
    localLine.className = "pdf-local-line";
    sourceLine.append(...Array.from(download.childNodes));
    localLine.append(...Array.from(local.childNodes));
    sourceLine.title = report.download_url || "No download URL";
    localLine.title = report.local_path || "No local PDF path";
    const creationLine = document.createElement("div");
    creationLine.className = "pdf-creation-date";
    creationLine.textContent = `Created: ${report.pdf_creation_date || "—"}`;
    creationLine.title = "PDF creation date";
    files.replaceChildren(sourceLine, localLine, creationLine);
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
      const byteCount = new TextEncoder().encode(report[key] || "").byteLength;
      heading.textContent = `${title}(${byteCount})`;
      heading.title = `${byteCount.toLocaleString()} UTF-8 bytes`;
      const copyControl = document.createElement("span");
      copyControl.className = "copy-control";
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-path";
      copy.textContent = "Copy";
      copy.title = `Copy ${title.toLowerCase()}`;
      copy.setAttribute("aria-label", `Copy ${title.toLowerCase()}`);
      const statement = report[key];
      copy.disabled = !statement;
      const feedback = document.createElement("span");
      feedback.className = "copy-feedback";
      feedback.setAttribute("role", "status");
      let feedbackTimer: ReturnType<typeof setTimeout> | undefined;
      copy.addEventListener("click", async event => {
        event.preventDefault();
        event.stopPropagation();
        if (!statement) return;
        clearTimeout(feedbackTimer);
        copy.disabled = true;
        feedback.textContent = "";
        try {
          await navigator.clipboard.writeText(statement);
          feedback.textContent = "Copied";
        } catch {
          feedback.textContent = "Copy failed";
        } finally {
          copy.disabled = false;
          feedbackTimer = setTimeout(() => { feedback.textContent = ""; }, 2000);
        }
      });
      copyControl.append(copy, feedback);
      heading.append(copyControl);
      const text = document.createElement("pre");
      text.textContent = report[key] || "Not extracted yet.";
      details.append(heading, text);
      content.append(details);
    }
    const date = report.updated_at ? new Date(report.updated_at) : null;
    const updated = cell(report.updated_at);
    if (date && !Number.isNaN(date.getTime())) {
      const dateLine = document.createElement("div");
      dateLine.textContent = date.toLocaleDateString();
      const timeLine = document.createElement("div");
      timeLine.textContent = date.toLocaleTimeString();
      updated.replaceChildren(dateLine, timeLine);
    }
    updated.classList.add("updated-at");
    const identity = cell(null);
    identity.className = "report-identity";
    identity.replaceChildren();
    for (const [title, value] of [
      ["Stock code", report.stock_code],
      ["Reporting date", report.reporting_date],
    ]) {
      const line = document.createElement("div");
      line.textContent = value || "—";
      line.title = `${title}: ${value || "—"}`;
      identity.append(line);
    }
    const hashText = document.createElement("div");
    hashText.textContent = hash.textContent;
    hashText.title = report.pdf_hash;
    const hashCopyControl = document.createElement("span");
    hashCopyControl.className = "copy-control";
    const hashCopy = document.createElement("button");
    hashCopy.type = "button";
    hashCopy.className = "copy-path";
    hashCopy.textContent = "Copy";
    hashCopy.title = "Copy full PDF hash";
    hashCopy.setAttribute("aria-label", `Copy full PDF hash: ${report.pdf_hash}`);
    const hashFeedback = document.createElement("span");
    hashFeedback.className = "copy-feedback";
    hashFeedback.setAttribute("role", "status");
    let hashFeedbackTimer: ReturnType<typeof setTimeout> | undefined;
    hashCopy.addEventListener("click", async () => {
      clearTimeout(hashFeedbackTimer);
      hashCopy.disabled = true;
      hashFeedback.textContent = "";
      try {
        await navigator.clipboard.writeText(report.pdf_hash);
        hashFeedback.textContent = "Copied";
      } catch {
        hashFeedback.textContent = "Copy failed";
      } finally {
        hashCopy.disabled = false;
        hashFeedbackTimer = setTimeout(() => { hashFeedback.textContent = ""; }, 2000);
      }
    });
    hashCopyControl.append(hashCopy, hashFeedback);
    hashText.append(hashCopyControl);
    hash.classList.add("pdf-hash");
    const stageLine = document.createElement("div");
    stageLine.className = "pdf-stage-line";
    stageLine.append(...Array.from(stage.childNodes));
    stageLine.append(stageEditor(report));
    hash.replaceChildren(hashText, stageLine);
    tr.append(hash, files, identity,
      textDetails(report.extraction_report, "View report"),
      auditCell(report.extraction_audit),
      textDetails(report.bs_digestion_report, "View balance-sheet digestion report"),
      textDetails(report.is_digestion_report, "View income-statement digestion report"),
      textDetails(report.cfs_digestion_report, "View cash-flow digestion report"), content,
      updated);
    rows.append(tr);
  }
  if (!items.length) {
    const td = cell("No reports match the selected filter.");
    td.colSpan = 10;
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
  reportTabs.forEach(tab => { tab.disabled = true; });
  searchFields.disabled = true;
  error.hidden = true;
  summary.textContent = "Loading reports…";
  rows.closest("table")!.setAttribute("aria-busy", "true");
  try {
    const parameters = new URLSearchParams({page: String(requestedPage), page_size: size.value, report_group: reportGroup});
    if (reportGroup === "Search") {
      for (const [key, value] of Object.entries(searchFilters)) {
        if (value) parameters.set(key, value);
      }
    }
    const response = await fetch(`/api/report-digestion?${parameters}`);
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
    reportTabs.forEach(tab => { tab.disabled = false; });
    searchFields.disabled = false;
    rows.closest("table")!.setAttribute("aria-busy", "false");
  }
}
previous.addEventListener("click", () => void load(page - 1));
next.addEventListener("click", () => void load(page + 1));
refresh.addEventListener("click", () => void load());
size.addEventListener("change", () => void load(1));
searchForm.addEventListener("submit", event => {
  event.preventDefault();
  if (loading || reportGroup !== "Search") return;
  searchFilters = {pdf_hash: searchHash.value.trim(), stock_code: searchStock.value.trim(), digestion_stage: searchStage.value};
  void load(1);
});
document.querySelector<HTMLButtonElement>("#clear-search")!.addEventListener("click", () => {
  if (loading) return;
  searchForm.reset();
  searchFilters = {};
  void load(1);
});
for (const tab of reportTabs) {
  tab.addEventListener("click", () => {
    if (loading || reportGroup === tab.dataset.group) return;
    reportGroup = tab.dataset.group!;
    searchForm.hidden = reportGroup !== "Search";
    for (const item of reportTabs) {
      item.setAttribute("aria-selected", String(item === tab));
      item.tabIndex = item === tab ? 0 : -1;
    }
    reportPanel.setAttribute("aria-labelledby", tab.id);
    rows.replaceChildren();
    page = totalPages = 1;
    label.textContent = "Page 1 of 1";
    void load(1);
  });
  tab.addEventListener("keydown", event => {
    const index = reportTabs.indexOf(tab);
    const target = event.key === "ArrowRight" ? (index + 1) % reportTabs.length
      : event.key === "ArrowLeft" ? (index + reportTabs.length - 1) % reportTabs.length
      : event.key === "Home" ? 0 : event.key === "End" ? reportTabs.length - 1 : -1;
    if (target < 0) return;
    event.preventDefault();
    reportTabs[target].focus();
    reportTabs[target].click();
  });
}
document.addEventListener("keydown", event => {
  if (event.target instanceof HTMLElement && event.target.closest("input, select, textarea, [contenteditable]")) return;
  if (event.key === "PageDown" && !next.disabled) { event.preventDefault(); void load(page + 1); }
  if (event.key === "PageUp" && !previous.disabled) { event.preventDefault(); void load(page - 1); }
});
void load();
