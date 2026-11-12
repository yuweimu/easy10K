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
  labelCn: string;
  labelEn: string;
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
  labelCn: string;
  labelEn: string;
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
const TAB_LABELS = {
  en: {
    "Balance Sheet": "Balance Sheet",
    "Income Statement": "Income Statement",
    "Cash Flow Statement": "Cash Flow Statement",
  },
  zh: {
    "Balance Sheet": "资产负债表",
    "Income Statement": "利润表",
    "Cash Flow Statement": "现金流量表",
  },
} as const;
type Language = "en" | "zh";

const UI_TEXT = {
  en: {
    financialReport: "Financial report",
    subtitle: "Search a company and explore its financial statements.",
    stockCode: "Stock code",
    loadReport: "Load report",
    loading: "Loading…",
    amountUnit: "Amount unit",
    showDecimals: "Show decimals",
    financialStatements: "Financial statements",
    companyReport: "Company report",
    comingSoon: "This report is coming soon.",
    enterStockCode: "Enter a stock code to load its report.",
    financialItem: "Financial item",
    recentSearches: "Recent searches",
    closeChart: "Close chart",
    selectColumn: "Select a column to see its date and amount.",
    showChildren: "Show",
    hideChildren: "Hide",
    childrenOf: "children of",
    showTrend: "Show amount trend",
    hideTrend: "Hide amount trend",
    amountTrendFor: "amount trend for",
    copyRow: "Copy row as JSON",
    copied: "Copied",
    copyFailed: "Copy failed. Please try again.",
    unknownItem: "Unknown item",
    loadFailed: "Unable to load the financial report",
    language: "Language",
    english: "English",
    chinese: "中文",
  },
  zh: {
    financialReport: "财务报告",
    subtitle: "搜索公司并查看其财务报表。",
    stockCode: "股票代码",
    loadReport: "加载报告",
    loading: "加载中…",
    amountUnit: "金额单位",
    showDecimals: "显示小数",
    financialStatements: "财务报表",
    companyReport: "公司报告",
    comingSoon: "该报表即将推出。",
    enterStockCode: "请输入股票代码以加载报告。",
    financialItem: "财务项目",
    recentSearches: "最近搜索",
    closeChart: "关闭图表",
    selectColumn: "选择一列以查看日期和金额。",
    showChildren: "显示",
    hideChildren: "隐藏",
    childrenOf: "的子项目",
    showTrend: "显示金额趋势",
    hideTrend: "隐藏金额趋势",
    amountTrendFor: "的金额趋势",
    copyRow: "复制此行 JSON",
    copied: "已复制",
    copyFailed: "复制失败，请重试。",
    unknownItem: "未知项目",
    loadFailed: "无法加载财务报告",
    language: "语言",
    english: "English",
    chinese: "中文",
  },
} as const;

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

function AmountTrend({ item, dates, unit, showDecimals, language, expanded = false, onClose }: {
  item: GroupedReportItem;
  dates: string[];
  unit: AmountUnit;
  showDecimals: boolean;
  language: Language;
  expanded?: boolean;
  onClose?: () => void;
}) {
  const text = UI_TEXT[language];
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const labelWidth = Math.max(80, ...dates.map((date) =>
    formatAmount(item.amountsByDate.get(date) ?? null, unit, showDecimals).length * 7 + 24));
  const width = expanded ? Math.max(640, dates.length * labelWidth + 96) : 96;
  const height = expanded ? 336 : 24;
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
      role="img" aria-label={`${item.label} ${language === "zh" ? "金额趋势" : "amount trend"}, ${dates[0]} ${language === "zh" ? "至" : "to"} ${dates[dates.length - 1]}`}>
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
      aria-label={text.closeChart} title={text.closeChart} onClick={onClose}>×</button>}
    </span>
    {expanded && <p className="trend-value" aria-live="polite">
      {selectedDate ? `${selectedDate}: ${formatAmount(item.amountsByDate.get(selectedDate) ?? null, unit, showDecimals)} ${unit}` : text.selectColumn}
    </p>}
    </>
  );
}

function App() {
  const [language, setLanguage] = useState<Language>(() =>
    localStorage.getItem("easy10k.language") === "zh" ? "zh" : "en",
  );
  const text = UI_TEXT[language];
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
    if (copyStatus?.message !== text.copied) return;
    const timeout = window.setTimeout(() => setCopyStatus(null), 2_000);
    return () => window.clearTimeout(timeout);
  }, [copyStatus, text.copied]);
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
          label: language === "zh" ? item.labelCn : item.labelEn,
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
  }, [language, report]);
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
      setCopyStatus({ itemId: item.item_id, message: text.copied });
    } catch {
      setCopyStatus({ itemId: item.item_id, message: text.copyFailed });
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
            label: (language === "zh" ? item.name_cn ?? item.name_en : item.name_en ?? item.name_cn) ?? text.unknownItem,
            labelCn: item.name_cn ?? item.name_en ?? text.unknownItem,
            labelEn: item.name_en ?? item.name_cn ?? text.unknownItem,
            parent_item_id: Math.floor(item.item_id / 100) || null,
          }))
        : Object.values(chart).map((item) => ({
            item_id: item.item_id,
            label: item.label,
            labelCn: item.label,
            labelEn: item.label,
            parent_item_id: Math.floor(item.item_id / 100) || null,
          }));
      const chartById = new Map(
        chartItems.map((item) => [item.item_id, item]),
      );

      setReportStockCode(code);
      setReport(
        balanceSheetItems.map((item) => ({
          ...item,
          labelCn: chartById.get(item.item_id)?.labelCn ?? text.unknownItem,
          labelEn: chartById.get(item.item_id)?.labelEn ?? text.unknownItem,
          indentLevel: getIndentLevel(item.item_id, chartById),
        })),
      );
    } catch (requestError) {
      setReport([]);
      setError(
        requestError instanceof Error
          ? requestError.message
          : text.loadFailed,
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="page-shell">
      <header className="page-header">
        <div className="language-switcher">
          <div className="language-toggle" role="group" aria-label={text.language}>
            <button
              type="button"
              className={language === "en" ? "active" : ""}
              aria-pressed={language === "en"}
              onClick={() => {
                setLanguage("en");
                localStorage.setItem("easy10k.language", "en");
              }}
            >
              EN
            </button>
            <button
              type="button"
              className={language === "zh" ? "active" : ""}
              aria-pressed={language === "zh"}
              onClick={() => {
                setLanguage("zh");
                localStorage.setItem("easy10k.language", "zh");
              }}
            >
              中文
            </button>
          </div>
        </div>
        <p className="eyebrow">EASY10K</p>
        <h1>{text.financialReport}</h1>
        <p className="subtitle">
          {text.subtitle}
        </p>
      </header>

      <form className="search-bar" onSubmit={loadReport}>
        <div className="search-controls">
          <label htmlFor="stock-code">{text.stockCode}</label>
          <div className="search-input-wrap">
            <input
              id="stock-code"
              value={stockCode}
              onChange={(event) => setStockCode(event.target.value)}
              onFocus={() => setSearchFocused(true)}
              onBlur={() => setSearchFocused(false)}
              placeholder={language === "zh" ? "例如 600519" : "e.g. 600519"}
              autoComplete="off"
              role="combobox"
              aria-autocomplete="list"
              aria-expanded={searchFocused && searchSuggestions.length > 0}
              aria-controls="recent-searches"
            />
            {searchFocused && searchSuggestions.length > 0 && (
              <div id="recent-searches" className="search-suggestions" role="listbox" aria-label={text.recentSearches}>
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
            {loading ? text.loading : text.loadReport}
          </button>
        </div>
          <div className="report-tools">
            <label htmlFor="amount-unit">{text.amountUnit}</label>
            <select
              id="amount-unit"
              value={amountUnit}
              onChange={(event) => setAmountUnit(event.target.value as AmountUnit)}
            >
              {Object.keys(AMOUNT_UNITS).map((unit) => (
                <option value={unit} key={unit}>{language === "zh" ? unit : `${unit} CNY`}</option>
              ))}
            </select>
            <label className="decimal-toggle">
              <input
                type="checkbox"
                checked={showDecimals}
                onChange={(event) => setShowDecimals(event.target.checked)}
              />
              {text.showDecimals}
            </label>
          </div>
      </form>

      {error && <p className="error-message">{error}</p>}

      <section className="report-card" aria-live="polite">
        <div className="report-tabs" role="tablist" aria-label={text.financialStatements}>
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
              }}>{TAB_LABELS[language][tab]}</button>
          ))}
        </div>
        <div id="report-panel" role="tabpanel" aria-labelledby={`report-tab-${REPORT_TABS.indexOf(activeTab)}`} tabIndex={0}>
        <div className="report-heading">
          <div>
            <p className="eyebrow">{TAB_LABELS[language][activeTab]}</p>
            <h2>{stockCode.trim() || text.companyReport}</h2>
          </div>

        </div>

        {activeTab !== "Balance Sheet" ? (
          <div className="empty-state statement-placeholder">
            <h3>{activeTab}</h3>
            <p>{text.comingSoon}</p>
          </div>
        ) : groupedReport.length === 0 && !loading ? (
          <div className="empty-state">{text.enterStockCode}</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{text.financialItem}</th>
                  {reportingDates.map((date) => (
                    <th className="amount-column" key={date}>
                      {date}
                      <a className="report-link"
                        href={`/api/reports/${encodeURIComponent(reportStockCode)}/${encodeURIComponent(date)}`}
                        target="_blank" rel="noopener noreferrer"
                        title={language === "zh" ? "在新标签页打开报告" : "Open report in a new tab"}
                        aria-label={language === "zh" ? `打开 ${date} 的报告（新标签页）` : `Open report for ${date} (new tab)`}>
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                          <path d="M9 2h5v5M14 2 7 9M7 3H3v10h10V9" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      </a>
                    </th>
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
                          aria-label={`${collapsedItemIds.has(item.item_id) ? text.showChildren : text.hideChildren} ${item.label} ${text.childrenOf}`}
                          aria-expanded={!collapsedItemIds.has(item.item_id)}
                        >
                          {collapsedItemIds.has(item.item_id) ? "▶" : "▼"}
                        </button>
                      ) : (
                        <span className="item-toggle-placeholder" />
                      )}
                      {item.label}
                      <button type="button" className="trend-toggle"
                        title={expandedTrendId === item.item_id ? text.hideTrend : text.showTrend}
                        aria-label={`${expandedTrendId === item.item_id ? text.hideTrend : text.showTrend} ${item.label}${language === "zh" ? text.amountTrendFor : ` ${text.amountTrendFor}`}`}
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
                        title={text.copyRow} aria-label={`${text.copyRow}: ${item.label}`}
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
                            <AmountTrend item={item} dates={trendDates} unit={amountUnit} showDecimals={showDecimals} language={language} expanded onClose={() => setExpandedTrendId(null)} />
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
