import { StrictMode, useMemo, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type LineItem = {
  item_id: number;
  english_name: string;
  parent: string | null;
};

type LineItemChartResponse =
  | Array<{ item_id: number; name_en?: string; english_name?: string }>
  | Record<string, LineItem>;

type BalanceSheetItem = {
  reporting_date: string;
  item_id: number;
  amount: number | string | null;
};

type ReportItem = BalanceSheetItem & {
  english_name: string;
};

type GroupedReportItem = {
  item_id: number;
  english_name: string;
  amountsByDate: Map<string, BalanceSheetItem["amount"]>;
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

function App() {
  const [stockCode, setStockCode] = useState("");
  const [report, setReport] = useState<ReportItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [amountUnit, setAmountUnit] = useState<AmountUnit>("元");
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
          english_name: item.english_name,
          amountsByDate: new Map([[item.reporting_date, item.amount]]),
        });
      }
    }

    return Array.from(grouped.values());
  }, [report]);

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

      const chartItems = Array.isArray(chart)
        ? chart.map((item) => ({
            item_id: item.item_id,
            english_name: item.name_en ?? item.english_name ?? "Unknown item",
          }))
        : Object.values(chart);
      const chartById = new Map(
        chartItems.map((item) => [item.item_id, item]),
      );

      setReport(
        balanceSheetItems.map((item) => ({
          ...item,
          english_name: chartById.get(item.item_id)?.english_name ?? "Unknown item",
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
            <span className="item-count">{groupedReport.length} items</span>
          </div>
        </div>

        {groupedReport.length === 0 && !loading ? (
          <div className="empty-state">Enter a stock code to load its report.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Item ID</th>
                  <th>Financial item</th>
                  {reportingDates.map((date) => (
                    <th className="amount-column" key={date}>{date} ({amountUnit})</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {groupedReport.map((item) => (
                  <tr key={item.item_id}>
                    <td>{item.item_id}</td>
                    <td className="item-name">{item.english_name}</td>
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
