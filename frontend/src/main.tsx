import { StrictMode, useMemo, useState, type FormEvent } from "react";
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

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

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
): string {
  if (amount === null || amount === "") return "—";
  const numericAmount = Number(amount) / AMOUNT_UNITS[unit];
  return Number.isFinite(numericAmount)
    ? numericAmount.toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
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

function App() {
  const [stockCode, setStockCode] = useState("605499");
  const [report, setReport] = useState<ReportItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [amountUnit, setAmountUnit] = useState<AmountUnit>("元");
  const [collapsedItemIds, setCollapsedItemIds] = useState<Set<number>>(
    () => new Set(),
  );
  const reportingDates = useMemo(
    () => Array.from(new Set(report.map((item) => item.reporting_date))),
    [report],
  );
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
      const parentId = Math.floor(item.item_id / 1000);
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
      let parentId = Math.floor(item.item_id / 1000);
      while (parentId > 0) {
        if (collapsedItemIds.has(parentId)) return false;
        parentId = Math.floor(parentId / 1000);
      }
      return true;
    });
  }, [collapsedItemIds, groupedReport]);
  const itemIdsWithChildren = useMemo(
    () =>
      new Set(
        groupedReport
          .map((item) => Math.floor(item.item_id / 1000))
          .filter((itemId) => itemId > 0),
      ),
    [groupedReport],
  );

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

    setLoading(true);
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
            parent_item_id: Math.floor(item.item_id / 1000) || null,
          }))
        : Object.values(chart).map((item) => ({
            item_id: item.item_id,
            label: item.label,
            parent_item_id: Math.floor(item.item_id / 1000) || null,
          }));
      const chartById = new Map(
        chartItems.map((item) => [item.item_id, item]),
      );

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
          Search a company and view its balance-sheet items with English names.
        </p>
      </header>

      <form className="search-bar" onSubmit={loadReport}>
        <label htmlFor="stock-code">Company stock code</label>
        <div className="search-controls">
          <input
            id="stock-code"
            value={stockCode}
            onChange={(event) => setStockCode(event.target.value)}
            placeholder="e.g. 600519"
            autoComplete="off"
          />
          <button type="submit" disabled={loading || stockCode.trim().length != 6}>
            {loading ? "Loading…" : "Load report"}
          </button>
        </div>
      </form>

      {error && <p className="error-message">{error}</p>}

      <section className="report-card" aria-live="polite">
        <div className="report-heading">
          <div>
            <p className="eyebrow">BALANCE SHEET</p>
            <h2>{stockCode.trim() || "Company report"}</h2>
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
          </div>
        </div>

        {groupedReport.length === 0 && !loading ? (
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
                  <tr key={item.item_id}>
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
                    </td>
                    {reportingDates.map((date) => (
                      <td className="amount-column" key={date}>
                        {formatAmount(item.amountsByDate.get(date) ?? null, amountUnit)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
