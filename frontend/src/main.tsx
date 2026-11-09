import { StrictMode, useState, type FormEvent } from "react";
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
  report_creation_time: string;
  amount: number | string | null;
};

type ReportItem = BalanceSheetItem & {
  english_name: string;
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

function formatAmount(amount: BalanceSheetItem["amount"]): string {
  if (amount === null || amount === "") return "—";
  const numericAmount = Number(amount);
  return Number.isFinite(numericAmount)
    ? numericAmount.toLocaleString(undefined, { maximumFractionDigits: 2 })
    : String(amount);
}

function App() {
  const [stockCode, setStockCode] = useState("");
  const [report, setReport] = useState<ReportItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
          <button type="submit" disabled={loading || !stockCode.trim()}>
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
          <span className="item-count">{report.length} items</span>
        </div>

        {report.length === 0 && !loading ? (
          <div className="empty-state">Enter a stock code to load its report.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Reporting date</th>
                  <th>Financial item</th>
                  <th>Report created</th>
                  <th className="amount-column">Amount</th>
                </tr>
              </thead>
              <tbody>
                {report.map((item, index) => (
                  <tr key={`${item.reporting_date}-${item.item_id}-${index}`}>
                    <td>{item.reporting_date}</td>
                    <td className="item-name">{item.english_name}</td>
                    <td>{item.report_creation_time}</td>
                    <td className="amount-column">{formatAmount(item.amount)}</td>
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
