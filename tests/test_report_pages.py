import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import main
import report_renderer
from fastapi import HTTPException


class ReportPageTests(unittest.TestCase):
    @patch("main.LineItemStorage")
    def test_public_statement_page_is_server_rendered(self, storage_class):
        storage = storage_class.return_value.__enter__.return_value
        storage.database_url = "postgresql://test"
        storage.get_statement_items.return_value = [
            {"reporting_date": "2025-12-31", "item_id": 3110, "amount": "2000.00",
             "report_creation_time": "created", "amount_unit": "元"},
            {"reporting_date": "2025-12-31", "item_id": 311001, "amount": "100.00",
             "report_creation_time": "created", "amount_unit": "元"},
            {"reporting_date": "2025-12-31", "item_id": 110110, "amount": "50.00",
             "report_creation_time": "created", "amount_unit": "元"},
            {"reporting_date": "2025-12-31", "item_id": 210108, "amount": "60.00",
             "report_creation_time": "created", "amount_unit": "元"},
            {"reporting_date": "2025-12-31", "item_id": 311004, "amount": "1234.50",
             "report_creation_time": "created", "amount_unit": "元"},
            {"reporting_date": "2024-12-31", "item_id": 311004, "amount": "1000.00",
             "report_creation_time": "created", "amount_unit": "元"},
        ]
        storage.get_statement_stock_codes.return_value = [
            {"stock_code": "300750", "stock_name": "宁德时代"},
        ]

        response = main.get_public_statement_page("300750", "balance-sheet")

        html = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("宁德时代 (300750) · 资产负债表", html)
        self.assertIn('class="site-logo"', html)
        self.assertIn("ValuePaper", html)
        self.assertIn("上市公司财报助手", html)
        self.assertIn('class="brand-icon"', html)
        self.assertNotIn('class="breadcrumb"', html)
        self.assertIn('class="recommendation-panel"', html)
        self.assertNotIn('role="tablist"', html)
        self.assertIn('class="recommendation-section-title"', html)
        self.assertNotIn("相关财报", html)
        self.assertNotIn("我的收藏", html)
        self.assertNotIn("最近访问", html)
        self.assertNotIn("recommendation-toggle", html)
        self.assertIn("高ROE企业", html)
        self.assertIn("低负债企业", html)
        self.assertIn("高现金流企业", html)
        self.assertIn("营收高增长企业", html)
        self.assertIn("recentStocks", html)
        self.assertIn("宁德时代</span>", html)
        self.assertIn('class="home-link" href="/"', html)
        self.assertIn("首页", html)
        self.assertIn("资本公积", html)
        self.assertNotIn("应收票据及应收账款", html)
        self.assertNotIn("以公允价值计量且其变动计入当期损益的金融负债", html)
        self.assertNotIn("Capital Reserves</span>", html)
        self.assertIn("2025-12-31", html)
        self.assertIn('class="amount-column"', html)
        self.assertIn('<select id="amount-unit">', html)
        self.assertIn('<option value="亿元" selected>亿元</option>', html)
        self.assertIn('<input id="show-decimals" type="checkbox" checked>', html)
        self.assertIn('aria-controls="stock-search-suggestions"', html)
        self.assertIn('<div class="stock-suggestions" id="stock-search-suggestions" role="listbox" hidden>', html)
        self.assertIn('data-stock-code="300750" data-stock-name="宁德时代"', html)
        self.assertIn('data-amount="1234.50"', html)
        self.assertIn('0.00</td>', html)
        self.assertIn("function renderAmounts()", html)
        self.assertIn('class="statement-tabs"', html)
        self.assertIn('href="/stocks/300750/balance-sheet?lang=zh"', html)
        self.assertIn('href="/stocks/300750/income-statement?lang=zh"', html)
        self.assertIn('href="/stocks/300750/cash-flow-statement?lang=zh"', html)
        self.assertIn('class="active" href="/stocks/300750/balance-sheet?lang=zh"', html)
        self.assertIn('class="period-filters"', html)
        self.assertIn('data-period-ending="" aria-pressed="true">全部</button>', html)
        self.assertIn('data-period-ending="12-31" aria-pressed="false">年报</button>', html)
        self.assertIn('data-period-ending="06-30" aria-pressed="false">半年报</button>', html)
        self.assertIn('data-period-ending="03-31" aria-pressed="false">一季报</button>', html)
        self.assertIn('data-period-ending="09-30" aria-pressed="false">三季报</button>', html)
        self.assertIn('function renderPeriods(ending)', html)
        self.assertIn('data-item-id="3110"', html)
        self.assertIn('data-item-toggle="3110"', html)
        self.assertIn('data-trend-toggle="3110"', html)
        self.assertIn('class="trend-detail-row"', html)
        self.assertIn('data-trend-chart="3110"', html)
        self.assertIn("function renderTrendChart(itemId)", html)
        self.assertIn("filter((cell) => !cell.hidden)", html)
        self.assertIn(".filter((cell) => !cell.hidden).reverse()", html)
        self.assertIn("renderExpandedTrendCharts()", html)
        self.assertIn("target.closest('.trend-chart-wrap')", html)
        self.assertIn('style="padding-left: 28px"', html)
        self.assertIn("function renderItemHierarchy()", html)
        self.assertIn("easy10k.amountUnit", html)
        self.assertIn("easy10k.periodEnding", html)
        self.assertIn("easy10k.showDecimals", html)
        self.assertIn("@view-transition { navigation: auto; }", html)
        self.assertIn("prefetch.rel = 'prefetch'", html)
        self.assertIn("sessionStorage.setItem(statementScrollPreferenceKey, String(window.scrollY))", html)
        self.assertIn("window.scrollTo(0, scrollY)", html)
        self.assertIn('background: #2f3a57', html)
        self.assertIn('lang="zh-CN"', html)
        self.assertIn('href="/stocks/300750/balance-sheet?lang=en"', html)
        self.assertIn('name="description"', html)
        self.assertIn('application/ld+json', html)

        english = main.get_public_statement_page("300750", "balance-sheet", "en").body.decode()
        self.assertIn("Balance Sheet", english)
        self.assertIn("Capital Reserves", english)
        self.assertNotIn("资本公积</span>", english)
        self.assertIn('class="home-link" href="/"', english)
        self.assertIn("Dashboard", english)
        self.assertIn('lang="en"', english)

    @patch("main.LineItemStorage")
    def test_public_home_page_is_rendered(self, storage_class):
        storage = storage_class.return_value.__enter__.return_value
        storage.database_url = "postgresql://test"
        storage.get_statement_stock_codes.return_value = [
            {"stock_code": "300750", "stock_name": "宁德时代"},
            {"stock_code": "600519", "stock_name": "贵州茅台"},
        ]

        response = main.read_root()

        html = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("ValuePaper", html)
        self.assertIn("读懂企业", html)
        self.assertIn('id="home-stock-search-form"', html)
        self.assertIn("资产负债表", html)
        self.assertIn("利润表", html)
        self.assertIn("现金流量表", html)
        self.assertIn("宁德时代", html)
        self.assertIn('href="/stocks/300750/balance-sheet"', html)
        self.assertIn('id="home-stock-suggestions"', html)

    def render_report(self, content: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "300750").mkdir()
            (root / "300750/2025-12-31.md").write_text(content, encoding="utf-8")
            # Isolate page tracking from the optional Markdown renderer dependency.
            with patch.object(main, "REPORTS_DIR", root), patch.object(
                report_renderer.markdown, "markdown", return_value=content, create=True,
            ):
                return main.get_report("300750", "2025-12-31").body.decode()

    def test_indicator_markup_and_comment_are_preserved(self) -> None:
        html = self.render_report("<!-- PDF_PAGE: 1/232 -->")
        self.assertIn('class="report-page-indicator" role="status" aria-live="polite" hidden', html)
        self.assertIn("<!-- PDF_PAGE: 1/232 -->", html)

    def test_renderer_preserves_markdown_options_and_security_headers(self) -> None:
        rendered = '<h1 id="__reportanchor-index">Report</h1><p>${title}</p>'
        with patch.object(report_renderer.markdown, "markdown", return_value=rendered, create=True) as renderer:
            response = report_renderer.render_report("# Report", title="<Report & title>")
            second_response = report_renderer.render_report("# Report", title="Report")
        renderer.assert_called_with("# Report", extensions=["tables", "fenced_code"])
        html = response.body.decode()
        self.assertIn("<title>&lt;Report &amp; title&gt;</title>", html)
        self.assertIn(rendered, html)
        nonce = re.search(r'<script nonce="([^"]+)">', html).group(1)
        self.assertIn(f"script-src 'nonce-{nonce}'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertNotEqual(response.headers["content-security-policy"],
                            second_response.headers["content-security-policy"])
        self.assertIn("rgb(244 243 249 / 85%)", html)

    def test_endpoint_keeps_missing_file_and_path_traversal_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "REPORTS_DIR", Path(directory)):
            for stock_code, expected_status in (("missing", 404), ("../..", 400)):
                with self.subTest(stock_code=stock_code), self.assertRaises(HTTPException) as error:
                    main.get_report(stock_code, "2025-12-31")
                self.assertEqual(error.exception.status_code, expected_status)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to check the viewer script")
    def test_viewer_script_syntax(self) -> None:
        subprocess.run(
            ["node", "--check", str(report_renderer.VIEW_DIR / "report.js")],
            capture_output=True, text=True, check=True,
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to test browser page tracking")
    def test_page_tracking_at_boundaries_and_after_jumps(self) -> None:
        self.assertEqual(self.track_pages(
            [" PDF_PAGE: 1/232 ", " PDF_PAGE: 2/232 ", " PDF_PAGE: 3/232 "],
            [-40, 0, 99, 100, 250, 0],
        ), ["第1页/共233页", "第1页/共233页", "第2页/共233页",
            "第2页/共233页", "第3页/共233页", "第1页/共233页"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to test browser page tracking")
    def test_uses_total_from_last_marker_and_ignores_invalid_comments(self) -> None:
        self.assertEqual(self.track_pages(
            [" PDF_PAGE: 1/10 ", "other comment", " PDF_PAGE: 2/232 "], [0],
        ), ["第1页/共233页"])
        self.assertEqual(self.track_pages(["PDF_PAGE: invalid"], [0]), [None])

    def track_pages(self, comments: list[str], positions: list[int]) -> list[str | None]:
        html = self.render_report("")
        script = re.search(r'<script nonce="[^"]+">(.*?)</script>', html, re.S).group(1)
        page_script = script.split("let thumbHeight = 0;", 1)[0]
        harness = r"""
const input = JSON.parse(process.argv[1]);
let scrollTop = 0;
let markerIndex = 0;
const indicator = { hidden: true, textContent: '' };
const fixtureComments = input.comments.map(data => ({ data, parentNode: {
  insertBefore(marker) {
    const top = markerIndex++ * 100;
    marker.getBoundingClientRect = () => ({ top: top - scrollTop });
  }
}}));
const NodeFilter = { SHOW_COMMENT: 128 };
const document = {
  querySelector(selector) {
    if (selector === '.report-page-indicator') return indicator;
    return { getBoundingClientRect: () => ({ top: 0 }) };
  },
  createElement() { return { setAttribute() {} }; },
  createTreeWalker() {
    let index = -1;
    return { nextNode() { return ++index < fixtureComments.length; },
      get currentNode() { return fixtureComments[index]; } };
  }
};
"""
        checks = """
const labels = input.positions.map(position => {
  scrollTop = position;
  updatePageIndicator();
  return indicator.hidden ? null : indicator.textContent;
});
process.stdout.write(JSON.stringify(labels));
"""
        result = subprocess.run(
            ["node", "-e", harness + page_script + checks,
             json.dumps({"comments": comments, "positions": positions})],
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
