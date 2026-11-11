import { Fragment, StrictMode, useEffect, useMemo, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type LineItem = {
  item_id: number;
  label: string;
  parent: string | null;
};

type LineItemChartResponse =
  | Array<{ item_id: number; name_en?: string; name_cn?: string }>
  | Record<string, LineItem>;

type BalanceSheetItem = {
  reporting_date: string;
  item_id: number;
  amount: number | string | null;
};

type ReportItem = BalanceSheetItem & {
  label: string;
  indentLevel: number;
};

type GroupedReportItem = {
  item_id: number;
  label: string;
  indentLevel: number;
  amountsByDate: Map<string, BalanceSheetItem["amount"]>;
};

type ChartItem = {
  item_id: number;
  label: string;
  parent_item_id: number | null;
};

const AMOUNT_UNITS = {
  元: 1,
  千元: 1_000,
  万元: 10_000,
  百万元: 1_000_000,
  亿元: 100_000_000,
} as const;

type AmountUnit = keyof typeof AMOUNT_UNITS;
const REPORT_TABS = ["Balance Sheet", "Income Statement", "Cash Flow Statement"] as const;

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";
const RECENT_SEARCHES_KEY = "easy10k.recent-searches";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

function formatAmount(
  amount: BalanceSheetItem["amount"],
  unit: AmountUnit,
  showDecimals: boolean,
): string {
  if (amount === null || amount === "") return "—";
  const numericAmount = Number(amount) / AMOUNT_UNITS[unit];
  return Number.isFinite(numericAmount)
    ? numericAmount.toLocaleString(undefined, {
        minimumFractionDigits: showDecimals ? 2 : 0,
        maximumFractionDigits: showDecimals ? 2 : 0,
      })
    : String(amount);
}

function getIndentLevel(itemId: number, chartById: Map<number, ChartItem>): number {
  let level = 0;
  let parentId = chartById.get(itemId)?.parent_item_id ?? null;

  while (parentId !== null && parentId > 0 && chartById.has(parentId)) {
    level += 1;
    parentId = chartById.get(parentId)?.parent_item_id ?? null;
  }

  return level;
}

function AmountTrend({ item, dates, unit, showDecimals, expanded = false, onClose }: {
  item: GroupedReportItem;
  dates: string[];
  unit: AmountUnit;
  showDecimals: boolean;
  expanded?: boolean;
  onClose?: () => void;
}) {
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const labelWidth = Math.max(80, ...dates.map((date) =>
    formatAmount(item.amountsByDate.get(date) ?? null, unit, showDecimals).length * 7 + 24));
  const width = expanded ? Math.max(640, dates.length * labelWidth + 96) : 96;
  const height = expanded ? 280 : 24;
  const padding = expanded ? 48 : 3;
  const bottom = expanded ? height - 30 : height - padding;
  const axisTop = bottom - (bottom - (padding - 24)) * 1.1;
  const axisRight = width - padding + 24 + 5;
  const viewWidth = expanded ? Math.max(width, axisRight + 24) : width;
  const values = dates.map((date) => {
    const raw = item.amountsByDate.get(date);
    return raw == null || String(raw).trim() === "" || !Number.isFinite(Number(raw))
      ? null : Number(raw);
  });
  const valid = values.filter((value): value is number => value !== null);
  if (!valid.length) return <span className="trend-empty">—</span>;
  const min = Math.min(...valid);
  const max = Math.max(...valid);
  const axisMin = Math.min(0, min);
  const axisMax = Math.max(0, max);
  const range = axisMax - axisMin || 1;
  const yFor = (value: number) => bottom - ((value - axisMin) / range) * (bottom - padding);
  const columnSpacing = (width - padding * 2) / dates.length;
  const columnWidth = Math.min(expanded ? 32 : 4, columnSpacing * 0.6) * 0.4;
  const points = values.map((value, index) => value === null ? null : ({
    x: padding + (index + 0.5) * columnSpacing,
    y: yFor(value),
  }));
  return (<>
    <span className={expanded ? "trend-plot" : undefined}>
    <svg className={expanded ? "trend-chart" : "sparkline"} viewBox={`0 0 ${viewWidth} ${height}`}
      role="img" aria-label={`${item.label} amount trend, ${dates[0]} to ${dates[dates.length - 1]}`}>
      {expanded && <>
        <text x={padding + 8} y={20}>{unit}</text>
        <line className="trend-axis" x1={padding} x2={padding} y1={axisTop} y2={bottom} />
        <line className="trend-axis" x1={padding} x2={axisRight} y1={yFor(0)} y2={yFor(0)} />
        <text x={padding - 10} y={yFor(0) + 4} textAnchor="end">0</text>
        {dates.map((date, index) => (
          <text key={date} x={padding + (index + 0.5) * columnSpacing}
            y={height - 8} textAnchor="middle">{date}</text>
        ))}
      </>}
      {points.map((point, index) => point && (
        <g key={dates[index]}>
        {expanded && <text className="trend-point-label" x={point.x}
          y={point.y + ((values[index] ?? 0) < 0 ? 14 : -10)} textAnchor="middle">
          {formatAmount(values[index], unit, showDecimals)}
        </text>}
        <rect className="trend-bar" x={point.x - columnWidth / 2}
          y={Math.min(point.y, yFor(0))} width={columnWidth}
          height={Math.max(Math.abs(point.y - yFor(0)), 1)}
          fill="currentColor" tabIndex={expanded ? 0 : undefined}
          onMouseEnter={expanded ? () => setSelectedDate(dates[index]) : undefined}
          onFocus={expanded ? () => setSelectedDate(dates[index]) : undefined}
          onClick={expanded ? () => setSelectedDate(dates[index]) : undefined}
          aria-label={`${dates[index]}: ${formatAmount(values[index], unit, showDecimals)} ${unit}`}>
          <title>{dates[index]}: {formatAmount(values[index], unit, showDecimals)} {unit}</title>
        </rect>
        </g>
      ))}
    </svg>
    {expanded && onClose && <button type="button" className="trend-close"
      aria-label="Close chart" title="Close chart" onClick={onClose}>×</button>}
    </span>
    {expanded && <p className="trend-value" aria-live="polite">
      {selectedDate ? `${selectedDate}: ${formatAmount(item.amountsByDate.get(selectedDate) ?? null, unit, showDecimals)} ${unit}` : "Select a column to see its date and amount."}
    </p>}
    </>
  );
}

function App() {
  const [activeTab, setActiveTab] = useState<typeof REPORT_TABS[number]>("Balance Sheet");
  const [stockCode, setStockCode] = useState("605499");
  const [recentSearches, setRecentSearches] = useState<string[]>([]);
  const [searchFocused, setSearchFocused] = useState(false);
  const [report, setReport] = useState<ReportItem[]>([]);
  const [reportStockCode, setReportStockCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [amountUnit, setAmountUnit] = useState<AmountUnit>("百万元");
  const [showDecimals, setShowDecimals] = useState(true);
  const [expandedTrendId, setExpandedTrendId] = useState<number | null>(null);
  const [copyStatus, setCopyStatus] = useState<{ itemId: number; message: string } | null>(null);
  useEffect(() => {
    try {
      const stored = JSON.parse(localStorage.getItem(RECENT_SEARCHES_KEY) || "null");
      if (Array.isArray(stored)) {
        setRecentSearches(stored.filter((value): value is string => typeof value === "string").slice(0, 3));
      }
    } catch {
      // Ignore malformed or unavailable local storage.
    }
  }, []);
  const searchSuggestions = useMemo(() => {
    const query = stockCode.trim();
    return recentSearches.filter((value) => !query || value.includes(query));
  }, [recentSearches, stockCode]);
  useEffect(() => {
    if (copyStatus?.message !== "Copied") return;
    const timeout = window.setTimeout(() => setCopyStatus(null), 2_000);
    return () => window.clearTimeout(timeout);
  }, [copyStatus]);
  const [collapsedItemIds, setCollapsedItemIds] = useState<Set<number>>(
    () => new Set(),
  );
  const reportingDates = useMemo(
    () => Array.from(new Set(report.map((item) => item.reporting_date))).sort().reverse(),
    [report],
  );
  const trendDates = useMemo(() => [...reportingDates].sort(), [reportingDates]);
  const groupedReport = useMemo(() => {
    const grouped = new Map<number, GroupedReportItem>();

    for (const item of report) {
      const existing = grouped.get(item.item_id);
      if (existing) {
        existing.amountsByDate.set(item.reporting_date, item.amount);
      } else {
        grouped.set(item.item_id, {
          item_id: item.item_id,
          label: item.label,
          indentLevel: item.indentLevel,
          amountsByDate: new Map([[item.reporting_date, item.amount]]),
        });
      }
    }

    const items = Array.from(grouped.values());
    const itemsById = new Map(items.map((item) => [item.item_id, item]));
    const childrenByParent = new Map<number, GroupedReportItem[]>();
    const roots: GroupedReportItem[] = [];

    for (const item of items) {
      const parentId = Math.floor(item.item_id / 100);
      if (parentId > 0 && itemsById.has(parentId)) {
        const children = childrenByParent.get(parentId) ?? [];
        children.push(item);
        childrenByParent.set(parentId, children);
      } else {
        roots.push(item);
      }
    }

    const byItemId = (left: GroupedReportItem, right: GroupedReportItem) =>
      left.item_id - right.item_id;
    const ordered: GroupedReportItem[] = [];
    const appendWithChildren = (item: GroupedReportItem) => {
      ordered.push(item);
      const children = childrenByParent.get(item.item_id) ?? [];
      children.sort(byItemId).forEach(appendWithChildren);
    };

    roots.sort(byItemId).forEach(appendWithChildren);
    return ordered;
  }, [report]);
  const visibleReport = useMemo(() => {
    return groupedReport.filter((item) => {
      let parentId = Math.floor(item.item_id / 100);
      while (parentId > 0) {
        if (collapsedItemIds.has(parentId)) return false;
        parentId = Math.floor(parentId / 100);
      }
      return true;
    });
  }, [collapsedItemIds, groupedReport]);
  const itemIdsWithChildren = useMemo(
    () =>
      new Set(
        groupedReport
          .map((item) => Math.floor(item.item_id / 100))
          .filter((itemId) => itemId > 0),
      ),
    [groupedReport],
  );

  async function copyItem(item: GroupedReportItem) {
    try {
      await navigator.clipboard.writeText(JSON.stringify({
        stock_code: reportStockCode,
        label: item.label,
        amount_unit: "元",
        amounts_by_date: Object.fromEntries(reportingDates.map((date) => [
          date, item.amountsByDate.get(date) ?? null,
        ])),
      }, null, 2));
      setCopyStatus({ itemId: item.item_id, message: "Copied" });
    } catch {
      setCopyStatus({ itemId: item.item_id, message: "Copy failed. Please try again." });
    }
  }

  function toggleItem(itemId: number) {
    setCollapsedItemIds((current) => {
      const next = new Set(current);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  }

  async function loadReport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const code = stockCode.trim();
    if (!code) return;

    setRecentSearches((current) => {
      const next = [code, ...current.filter((value) => value !== code)].slice(0, 3);
      try {
        localStorage.setItem(RECENT_SEARCHES_KEY, JSON.stringify(next));
      } catch {
        // Recent searches are a convenience and do not block loading reports.
      }
      return next;
    });

    setLoading(true);
    setExpandedTrendId(null);
    setCopyStatus(null);
    setError(null);
    try {
      const [chart, balanceSheetItems] = await Promise.all([
        getJson<LineItemChartResponse>("/line-item-chart"),
        getJson<BalanceSheetItem[]>(
          `/balance_sheet_items/${encodeURIComponent(code)}`,
        ),
      ]);

      const chartItems: ChartItem[] = Array.isArray(chart)
        ? chart.map((item) => ({
            item_id: item.item_id,
            label: item.name_cn ?? item.name_en ?? "Unknown item",
            parent_item_id: Math.floor(item.item_id / 100) || null,
          }))
        : Object.values(chart).map((item) => ({
            item_id: item.item_id,
            label: item.label,
            parent_item_id: Math.floor(item.item_id / 100) || null,
          }));
      const chartById = new Map(
        chartItems.map((item) => [item.item_id, item]),
      );

      setReportStockCode(code);
      setReport(
        balanceSheetItems.map((item) => ({
          ...item,
          label: chartById.get(item.item_id)?.label ?? "Unknown item",
          indentLevel: getIndentLevel(item.item_id, chartById),
        })),
      );
    } catch (requestError) {
      setReport([]);
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Unable to load the financial report",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="page-shell">
      <header className="page-header">
        <p className="eyebrow">EASY10K</p>
        <h1>Financial report</h1>
        <p className="subtitle">
          Search a company and explore its financial statements.
        </p>
      </header>

      <form className="search-bar" onSubmit={loadReport}>
        <div className="search-controls">
          <label htmlFor="stock-code">Stock code</label>
          <div className="search-input-wrap">
            <input
              id="stock-code"
              value={stockCode}
              onChange={(event) => setStockCode(event.target.value)}
              onFocus={() => setSearchFocused(true)}
              onBlur={() => setSearchFocused(false)}
              placeholder="e.g. 600519"
              autoComplete="off"
              role="combobox"
              aria-autocomplete="list"
              aria-expanded={searchFocused && searchSuggestions.length > 0}
              aria-controls="recent-searches"
            />
            {searchFocused && searchSuggestions.length > 0 && (
              <div id="recent-searches" className="search-suggestions" role="listbox" aria-label="Recent searches">
                {searchSuggestions.map((value) => (
                  <button
                    key={value}
                    type="button"
                    role="option"
                    aria-selected={value === stockCode.trim()}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => {
                      setStockCode(value);
                      setSearchFocused(false);
                    }}
                  >
                    {value}
                  </button>
                ))}
              </div>
            )}
          </div>
          <button type="submit" disabled={loading || stockCode.trim().length != 6}>
            {loading ? "Loading…" : "Load report"}
          </button>
        </div>
          <div className="report-tools">
            <label htmlFor="amount-unit">Amount unit</label>
            <select
              id="amount-unit"
              value={amountUnit}
              onChange={(event) => setAmountUnit(event.target.value as AmountUnit)}
            >
              {Object.keys(AMOUNT_UNITS).map((unit) => (
                <option value={unit} key={unit}>{unit}</option>
              ))}
            </select>
            <label className="decimal-toggle">
              <input
                type="checkbox"
                checked={showDecimals}
                onChange={(event) => setShowDecimals(event.target.checked)}
              />
              Show decimals
            </label>
          </div>
      </form>

      {error && <p className="error-message">{error}</p>}

      <section className="report-card" aria-live="polite">
        <div className="report-tabs" role="tablist" aria-label="Financial statements">
          {REPORT_TABS.map((tab, index) => (
            <button key={tab} type="button" role="tab" id={`report-tab-${index}`}
              aria-selected={activeTab === tab} aria-controls="report-panel"
              tabIndex={activeTab === tab ? 0 : -1}
              onClick={() => setActiveTab(tab)}
              onKeyDown={(event) => {
                let nextIndex = index;
                if (event.key === "ArrowRight") nextIndex = (index + 1) % REPORT_TABS.length;
                else if (event.key === "ArrowLeft") nextIndex = (index + REPORT_TABS.length - 1) % REPORT_TABS.length;
                else if (event.key === "Home") nextIndex = 0;
                else if (event.key === "End") nextIndex = REPORT_TABS.length - 1;
                else return;
                event.preventDefault();
                setActiveTab(REPORT_TABS[nextIndex]);
                document.getElementById(`report-tab-${nextIndex}`)?.focus();
              }}>{tab}</button>
          ))}
        </div>
        <div id="report-panel" role="tabpanel" aria-labelledby={`report-tab-${REPORT_TABS.indexOf(activeTab)}`} tabIndex={0}>
        <div className="report-heading">
          <div>
            <p className="eyebrow">{activeTab}</p>
            <h2>{stockCode.trim() || "Company report"}</h2>
          </div>

        </div>

        {activeTab !== "Balance Sheet" ? (
          <div className="empty-state statement-placeholder">
            <h3>{activeTab}</h3>
            <p>This report is coming soon.</p>
          </div>
        ) : groupedReport.length === 0 && !loading ? (
          <div className="empty-state">Enter a stock code to load its report.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Financial item</th>
                  {reportingDates.map((date) => (
                    <th className="amount-column" key={date}>{date}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visibleReport.map((item) => (
                  <Fragment key={item.item_id}>
                  <tr key={item.item_id}
                    className={`item-row${itemIdsWithChildren.has(item.item_id) ? " item-row-group" : ""}${expandedTrendId === item.item_id ? " item-row-expanded" : ""}`}
                    onDoubleClick={(event) => {
                    if ((event.target as HTMLElement).closest("button")) return;
                    setExpandedTrendId((current) => current === item.item_id ? null : item.item_id);
                  }}>
                    <td
                      className="item-name"
                      style={{ paddingLeft: `${24 + item.indentLevel * 24}px` }}
                    >
                      {itemIdsWithChildren.has(item.item_id) ? (
                        <button
                          type="button"
                          className="item-toggle"
                          onClick={() => toggleItem(item.item_id)}
                          aria-label={`${collapsedItemIds.has(item.item_id) ? "Show" : "Hide"} children of ${item.label}`}
                          aria-expanded={!collapsedItemIds.has(item.item_id)}
                        >
                          {collapsedItemIds.has(item.item_id) ? "▶" : "▼"}
                        </button>
                      ) : (
                        <span className="item-toggle-placeholder" />
                      )}
                      {item.label}
                      <button type="button" className="trend-toggle"
                        title="Show amount trend"
                        aria-label={`${expandedTrendId === item.item_id ? "Hide" : "Show"} amount trend for ${item.label}`}
                        aria-expanded={expandedTrendId === item.item_id}
                        aria-controls={`trend-${item.item_id}`}
                        onClick={() => setExpandedTrendId(expandedTrendId === item.item_id ? null : item.item_id)}>
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                          <path d="M2 2v12h12" stroke="currentColor" strokeWidth="1.2" />
                          <path d="M5 11V8m4 3V5m4 6V3" stroke="currentColor" strokeWidth="2" />
                        </svg>
                      </button>
                      <span className="copy-action">
                      <button type="button" className="trend-toggle"
                        title="Copy row as JSON" aria-label={`Copy ${item.label} as JSON`}
                        onClick={() => void copyItem(item)}>
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                          <rect x="5" y="5" width="8" height="9" rx="1" stroke="currentColor" strokeWidth="1.2" />
                          <path d="M10 5V2H2v9h3" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
                        </svg>
                      </button>
                      {copyStatus?.itemId === item.item_id && <span className="copy-status" role="status">{copyStatus.message}</span>}
                      </span>
                    </td>
                    {reportingDates.map((date) => (
                      <td className="amount-column" key={date}>
                        {formatAmount(
                          item.amountsByDate.get(date) ?? null,
                          amountUnit,
                          showDecimals,
                        )}
                      </td>
                    ))}
                  </tr>
                  {expandedTrendId === item.item_id && (
                    <tr className="trend-detail-row" id={`trend-${item.item_id}`}>
                      <td colSpan={reportingDates.length + 1}>
                        <div className="trend-detail">
                          <div className="trend-chart-scroll">
                            <AmountTrend item={item} dates={trendDates} unit={amountUnit} showDecimals={showDecimals} expanded onClose={() => setExpandedTrendId(null)} />
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
        </div>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
