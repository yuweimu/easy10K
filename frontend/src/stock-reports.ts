import "./stock-reports.css";

type Stock = {
  stock_code: string;
  stock_name: string | null;
  report_count: number;
  enrollment: string | null;
  has_missing_reporting_dates: boolean;
};

type Report = {
  pdf_hash: string;
  stock_code: string | null;
  reporting_date: string | null;
  pdf_creation_date: string | null;
  local_path: string | null;
  digestion_stage: string;
};

type ReportPage = { items: Report[]; total: number; total_pages: number };
type Statement = "all" | "balance_sheet" | "income_statement" | "cash_flow_statement";
type StatementItem = { reporting_date: string };
const STATEMENT_ENDPOINTS: Record<Exclude<Statement, "all">, string> = {
  balance_sheet: "/api/balance_sheet_items",
  income_statement: "/api/income_statement_items",
  cash_flow_statement: "/api/cash_flow_statement_items",
};

const refresh = document.querySelector<HTMLButtonElement>("#refresh")!;
const stocksElement = document.querySelector<HTMLDivElement>("#stocks")!;
const stockCount = document.querySelector<HTMLElement>("#stock-count")!;
const reportTotal = document.querySelector<HTMLElement>("#report-total")!;
const selectedTitle = document.querySelector<HTMLElement>("#selected-stock-title")!;
const selectedSummary = document.querySelector<HTMLElement>("#selected-summary")!;
const reportDates = document.querySelector<HTMLElement>("#report-dates")!;
const dateChips = document.querySelector<HTMLDivElement>("#date-chips")!;
const rows = document.querySelector<HTMLTableSectionElement>("#reports")!;
const error = document.querySelector<HTMLElement>("#error")!;
const card = document.querySelector<HTMLElement>(".stock-report-card")!;
const statementTabs = Array.from(document.querySelectorAll<HTMLButtonElement>("#statement-tabs [role=tab]"));

let stocks: Stock[] = [];
let selectedCode = "";
const enrollmentEditorOpen = new Set<string>();
let requestId = 0;
let activeStatement: Statement = "all";
let selectedReports: Report[] = [];
let statementDates: Record<Statement, Set<string>> = {
  all: new Set(), balance_sheet: new Set(), income_statement: new Set(), cash_flow_statement: new Set(),
};
const REPORTING_DATE_END = "2026-09-30";

function fileName(path: string | null): string {
  if (!path) return "—";
  return path.split(/[\\/]/).pop() || path;
}

function fullPath(path: string | null): string | null {
  if (!path) return null;
  if (path.startsWith("/")) return path;
  const projectDirectory = import.meta.env.VITE_PROJECT_DIR as string;
  return `${projectDirectory}${path.replace(/^\.?[\\/]*/, "")}`;
}

function stageClass(stage: string): string {
  if (stage.endsWith("Err") || stage === "AuditErr") return "stage-error";
  if (stage.endsWith("OK") || stage === "AuditOK" || stage === "ManualPass") return "stage-done";
  return "stage-pending";
}

function copyButton(value: string | null, label: string): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy-button";
  button.textContent = "Copy";
  button.title = value ? `${label}: ${value}` : `No ${label.toLowerCase()} available`;
  button.disabled = !value;
  button.addEventListener("click", async () => {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      button.textContent = "Copied";
      window.setTimeout(() => { button.textContent = "Copy"; }, 1500);
    } catch {
      button.textContent = "Copy failed";
      window.setTimeout(() => { button.textContent = "Copy"; }, 1500);
    }
  });
  return button;
}

function quarterEndsFrom(start: string, end: string): string[] {
  const [startYear, startMonth] = start.split("-").map(Number);
  const [endYear, endMonth] = end.split("-").map(Number);
  const dates: string[] = [];
  for (let year = startYear; year <= endYear; year += 1) {
    for (const [month, day] of [[3, 31], [6, 30], [9, 30], [12, 31]]) {
      if ((year === startYear && month < startMonth) ||
          (year === endYear && month > endMonth)) continue;
      dates.push(`${year}-${String(month).padStart(2, "0")}-${day}`);
    }
  }
  return dates;
}

function renderStocks(): void {
  stocksElement.replaceChildren();
  for (const stock of stocks) {
    const item = document.createElement("div");
    item.className = [
      "stock-chip",
      enrollmentEditorOpen.has(stock.stock_code) ? "enrollment-editor-open" : "",
      stock.stock_code === selectedCode ? "stock-selected" : "",
      stock.has_missing_reporting_dates ? "" : "stock-complete",
      stock.enrollment === "Done" && stock.has_missing_reporting_dates ? "stock-missing-dates" : "",
      `enrollment-${(stock.enrollment || "Unknown").toLowerCase()}`,
    ].filter(Boolean).join(" ");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "stock-select";
    button.setAttribute("aria-pressed", String(stock.stock_code === selectedCode));
    button.setAttribute("aria-expanded", String(enrollmentEditorOpen.has(stock.stock_code)));
    const label = document.createElement("span");
    label.textContent = `${stock.stock_code} · ${stock.stock_name || "Unnamed"} (${stock.report_count})`;
    button.addEventListener("click", () => {
      if (enrollmentEditorOpen.has(stock.stock_code)) {
        enrollmentEditorOpen.clear();
      } else {
        enrollmentEditorOpen.clear();
        enrollmentEditorOpen.add(stock.stock_code);
      }
      void selectStock(stock.stock_code);
    });
    button.append(label);

    const editor = document.createElement("div");
    editor.className = "enrollment-editor";
    const select = document.createElement("select");
    select.setAttribute("aria-label", `Status for ${stock.stock_code}`);
    const currentEnrollment = stock.enrollment || "Pending";
    for (const status of ["Pending", "InProgress", "Error", "Done"]) {
      select.add(new Option(status, status));
    }
    select.value = currentEnrollment;
    const save = document.createElement("button");
    save.type = "button";
    save.className = "enrollment-save";
    save.textContent = "Save";
    save.disabled = true;
    const feedback = document.createElement("small");
    feedback.className = "enrollment-feedback";
    feedback.setAttribute("role", "status");
    select.addEventListener("change", () => {
      save.disabled = select.value === currentEnrollment;
      feedback.textContent = "";
    });
    save.addEventListener("click", async () => {
      select.disabled = save.disabled = true;
      feedback.textContent = "Saving…";
      try {
        const response = await fetch(`/api/stock-metadata/${encodeURIComponent(stock.stock_code)}/enrollment`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enrollment: select.value }),
        });
        if (!response.ok) throw new Error(`Unable to save status (HTTP ${response.status}).`);
        const updated: Pick<Stock, "enrollment" | "stock_name"> = await response.json();
        stock.enrollment = updated.enrollment;
        stock.stock_name = updated.stock_name;
        renderStocks();
      } catch (reason) {
        feedback.textContent = reason instanceof Error ? reason.message : "Unable to save status.";
      } finally {
        select.disabled = false;
        save.disabled = select.value === (stock.enrollment || "Pending");
      }
    });
    editor.append(select, save, feedback);
    item.append(button, editor);
    stocksElement.append(item);
  }
  stockCount.textContent = String(stocks.length);
  reportTotal.textContent = `Total reports: ${stocks.reduce((sum, stock) => sum + stock.report_count, 0).toLocaleString()}`;
}

function renderReports(reports: Report[]): void {
  const actualDates = statementDates[activeStatement];
  const sortedActualDates = [...actualDates].sort();
  const dates = sortedActualDates.length
    ? quarterEndsFrom(sortedActualDates[0], REPORTING_DATE_END)
    : [];
  reportDates.hidden = dates.length === 0;
  dateChips.replaceChildren();
  for (const date of dates) {
    const chip = document.createElement("span");
    const missing = !actualDates.has(date);
    const stages = activeStatement === "all"
      ? [...new Set(reports
        .filter(report => report.reporting_date === date && !["CashFlowOK", "ExCashFlowOK"].includes(report.digestion_stage))
        .map(report => report.digestion_stage))]
      : [];
    chip.textContent = missing
      ? `${date} · Missing`
      : stages.length ? `${date} · ${stages.join(", ")}` : date;
    chip.className = missing ? "missing-date" : "";
    if (stages.length) chip.classList.add("stage-date");
    chip.title = missing
      ? `${date} has no enrolled report`
      : stages.length ? `${date}: ${stages.join(", ")}` : `${date} has a completed cash-flow report`;
    dateChips.append(chip);
  }

  rows.replaceChildren();
  for (const report of reports.sort((left, right) => (left.reporting_date || "").localeCompare(right.reporting_date || ""))) {
    const row = document.createElement("tr");
    const stage = document.createElement("span");
    stage.className = `stage-badge ${stageClass(report.digestion_stage)}`;
    stage.textContent = report.digestion_stage;
    const stageCell = document.createElement("td");
    stageCell.append(stage);
    const fileCell = document.createElement("td");
    fileCell.className = "copyable-cell";
    fileCell.append(document.createTextNode(fileName(report.local_path)), copyButton(fullPath(report.local_path), "full file path"));
    const hash = document.createElement("code");
    hash.textContent = report.pdf_hash.slice(0, 12);
    hash.title = report.pdf_hash;
    const hashCell = document.createElement("td");
    hashCell.className = "copyable-cell";
    hashCell.append(hash, copyButton(report.pdf_hash, "full hash"));
    row.append(
      textCell(report.reporting_date),
      stageCell,
      fileCell,
      hashCell,
    );
    rows.append(row);
  }
  if (!reports.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "empty-cell";
    cell.textContent = "No reports";
    row.append(cell);
    rows.append(row);
  }
}

function textCell(value: string | null): HTMLTableCellElement {
  const cell = document.createElement("td");
  cell.textContent = value || "—";
  return cell;
}

async function loadReports(stockCode: string, currentRequest: number): Promise<void> {
  const allReports: Report[] = [];
  let page = 1;
  let totalPages = 1;
  do {
    const query = new URLSearchParams({
      report_group: "Search",
      stock_code: stockCode,
      page: String(page),
      page_size: "100",
    });
    const response = await fetch(`/api/report-digestion?${query}`);
    if (!response.ok) throw new Error(`Unable to load reports (HTTP ${response.status}).`);
    const data: ReportPage = await response.json();
    allReports.push(...data.items.filter(report => report.digestion_stage !== "Deprecated"));
    totalPages = data.total_pages;
    page += 1;
  } while (page <= totalPages);
  if (currentRequest !== requestId) return;
  selectedReports = allReports;
  const statementResults = await Promise.all(
    (Object.entries(STATEMENT_ENDPOINTS) as [Exclude<Statement, "all">, string][]).map(async ([statement, endpoint]) => {
      const response = await fetch(`${endpoint}/${encodeURIComponent(stockCode)}`);
      if (!response.ok) throw new Error(`Unable to load ${statement.replaceAll("_", " ")} data (HTTP ${response.status}).`);
      const items: StatementItem[] = await response.json();
      return [statement, new Set(items.map(item => item.reporting_date))] as const;
    }),
  );
  statementDates = {
    all: new Set(allReports.map(report => report.reporting_date).filter((date): date is string => Boolean(date))),
    ...Object.fromEntries(statementResults),
  } as Record<Statement, Set<string>>;
  renderStatement();
}

function renderStatement(): void {
  const dates = statementDates[activeStatement];
  const reports = activeStatement === "all"
    ? selectedReports
    : selectedReports.filter(report => report.reporting_date && dates.has(report.reporting_date));
  selectedSummary.textContent = `${reports.length} reports · ${dates.size} reporting periods`;
  renderReports(reports);
}

async function selectStock(stockCode: string): Promise<void> {
  selectedCode = stockCode;
  renderStocks();
  const stock = stocks.find(item => item.stock_code === stockCode);
  selectedTitle.textContent = stock ? `${stock.stock_code} · ${stock.stock_name || "Unnamed"}` : stockCode;
  selectedSummary.textContent = "Loading…";
  rows.replaceChildren();
  reportDates.hidden = true;
  error.hidden = true;
  card.setAttribute("aria-busy", "true");
  const currentRequest = ++requestId;
  try {
    await loadReports(stockCode, currentRequest);
  } catch (reason) {
    if (currentRequest !== requestId) return;
    selectedSummary.textContent = "";
    error.textContent = reason instanceof Error ? reason.message : "Unable to load reports.";
    error.hidden = false;
  } finally {
    if (currentRequest === requestId) card.setAttribute("aria-busy", "false");
  }
}

async function loadStocks(): Promise<void> {
  refresh.disabled = true;
  error.hidden = true;
  try {
    const response = await fetch("/api/balance_sheet_items");
    if (!response.ok) throw new Error(`Unable to load stocks (HTTP ${response.status}).`);
    const data: Stock[] = await response.json();
    stocks = data;
    if (!stocks.length) {
      stocksElement.textContent = "No stocks available.";
      stockCount.textContent = "0";
      reportTotal.textContent = "Total reports: 0";
      return;
    }
    if (!stocks.some(stock => stock.stock_code === selectedCode)) selectedCode = stocks[0].stock_code;
    renderStocks();
    await selectStock(selectedCode);
  } catch (reason) {
    error.textContent = reason instanceof Error ? reason.message : "Unable to load stocks.";
    error.hidden = false;
  } finally {
    refresh.disabled = false;
  }
}

refresh.addEventListener("click", () => void loadStocks());
for (const tab of statementTabs) {
  tab.addEventListener("click", () => {
    activeStatement = tab.dataset.statement as Statement;
    for (const other of statementTabs) other.setAttribute("aria-selected", String(other === tab));
    renderStatement();
  });
}
void loadStocks();
