# main.py
import logging
import json
import re
from html import escape
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from item_ids import EPS_DILUTED_ID, EPS_ID
from line_item_chart import LineItemChart
from line_item_storage import LineItemStorage, ReportDigestionStage
from report_renderer import render_report
from report_sources import locate_source, render_page

# 1. Initialize the FastAPI application
app = FastAPI()
logger = logging.getLogger(__name__)
line_item_chart = LineItemChart()
REPORTS_DIR = Path(__file__).resolve().parent / "preprocessed"
PROJECT_DIR = Path(__file__).resolve().parent
SOURCE_CACHE_DIR = PROJECT_DIR / "reports" / ".source-cache"
PUBLIC_STATEMENTS = {
    "balance-sheet": ("balance_sheet", "Balance Sheet", "资产负债表"),
    "income-statement": ("income_statement", "Income Statement", "利润表"),
    "cash-flow": ("cash_flow_statement", "Cash Flow Statement", "现金流量表"),
    "cash-flow-statement": ("cash_flow_statement", "Cash Flow Statement", "现金流量表"),
}
PUBLIC_AMOUNT_UNITS = {"元": 1, "千元": 1_000, "万元": 10_000, "百万元": 1_000_000, "亿元": 100_000_000}
HIDDEN_BALANCE_SHEET_ITEM_IDS = {110110, 210108}

# 2. Define the structure of the data we expect (Pydantic Model)
class WordInput(BaseModel):
    """Defines the structure for the data we expect from the user."""
    word_input: str

# --- API Endpoints ---


class ReportStageUpdate(BaseModel):
    digestion_stage: ReportDigestionStage


@app.patch("/report-digestion/{pdf_hash}/stage")
def update_report_digestion_stage(pdf_hash: str, data: ReportStageUpdate) -> dict:
    """Manually set the stage of an existing report selected by PDF hash."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            result = storage.update_report_digestion_stage(pdf_hash, data.digestion_stage)
        except Exception as exc:
            logger.warning("Unable to update report digestion stage", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to update report stage") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Report not found")
        return result


@app.get("/report-digestion")
def get_report_digestion(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    show_passed: bool = False,
    only_manual_pass: bool = False,
    report_group: Literal["InProgress", "FileErr", "ExtractOK", "ExtractErr", "AuditOK", "AuditErr",
                          "BalanceErr", "IncomeErr", "CashFlowErr", "CashFlowOK",
                          "ManualPass", "Deprecated", "Search"] | None = None,
    pdf_hash: str | None = Query(default=None, max_length=128),
    stock_code: str | None = Query(default=None, max_length=12),
    reporting_date: str | None = Query(default=None, max_length=10),
    digestion_stage: ReportDigestionStage | None = None,
) -> dict:
    """Return a paginated, read-only view of report digestion records."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.get_report_digestion(
                page, page_size, show_passed=show_passed, only_manual_pass=only_manual_pass,
                report_group=report_group, pdf_hash=pdf_hash, stock_code=stock_code,
                reporting_date=reporting_date, digestion_stage=digestion_stage)
        except Exception as exc:
            logger.warning("Unable to fetch report digestion", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch report digestion") from exc


@app.get("/report-digestion/file")
def get_digestion_pdf(pdf_hash: str = Query(min_length=1)) -> FileResponse:
    """Serve a registered local PDF inline, restricted to the reports directory."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            local_path = storage.get_digestion_pdf_path(pdf_hash)
        except Exception as exc:
            logger.warning("Unable to fetch digestion PDF path", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch registered PDF") from exc
    if not local_path:
        raise HTTPException(status_code=404, detail="Registered PDF not found")
    path = (PROJECT_DIR / local_path).resolve()
    if not path.is_relative_to((PROJECT_DIR / "reports").resolve()):
        raise HTTPException(status_code=403, detail="File is outside report directories")
    if path.suffix.lower() != ".pdf" or not path.is_file():
        raise HTTPException(status_code=404, detail="Report PDF not found")
    return FileResponse(path, media_type="application/pdf", filename=path.name,
                        content_disposition_type="inline", headers={"X-Content-Type-Options": "nosniff"})


@app.get("/report-registry")
def get_report_registry(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> dict:
    """Return a paginated, read-only view of registered reports."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.get_report_registry(page, page_size)
        except Exception as exc:
            logger.warning("Unable to fetch report registry", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch report registry") from exc


@app.get("/report-registry/file/{file_kind}")
def get_registry_file(
    file_kind: Literal["pdf_local_path", "markdown_file", "preprocessed_file"],
    pdf_hash: str = Query(min_length=1),
    pdf_creation_time: str = Query(min_length=1),
) -> Response:
    """Open a registered PDF inline or render its Markdown in the report viewer."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            files = storage.get_report_files(pdf_hash, pdf_creation_time)
        except Exception as exc:
            logger.warning("Unable to fetch registered file", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch registered file") from exc
    if not files or not files.get(file_kind):
        raise HTTPException(status_code=404, detail="Registered file not found")
    # Resolve only database-selected paths, never an arbitrary path from the URL.
    path = (PROJECT_DIR / files[file_kind]).resolve()
    allowed_roots = [(PROJECT_DIR / folder).resolve() for folder in ("reports", "markdown", "preprocessed")]
    if not any(path.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="File is outside report directories")
    expected_suffix = ".pdf" if file_kind == "pdf_local_path" else ".md"
    if path.suffix.lower() != expected_suffix or not path.is_file():
        raise HTTPException(status_code=404, detail="Report file not found")
    if file_kind == "pdf_local_path":
        return FileResponse(path, media_type="application/pdf", filename=path.name,
                            content_disposition_type="inline", headers={"X-Content-Type-Options": "nosniff"})
    return render_report(path.read_text(encoding="utf-8"), title=path.name)


@app.get("/reports/{stock_code}/{reporting_date}", response_class=HTMLResponse)
def get_report(stock_code: str, reporting_date: str) -> HTMLResponse:
    """Render the Markdown report as a browser-readable HTML page."""
    report_path = (REPORTS_DIR / stock_code / f"{reporting_date}.md").resolve()
    reports_dir = REPORTS_DIR.resolve()
    if not report_path.is_relative_to(reports_dir):
        raise HTTPException(status_code=400, detail="Invalid report path")

    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")

    return render_report(
        report_path.read_text(encoding="utf-8"),
        title=f"{stock_code} · {reporting_date}",
    )


@app.get("/stocks/{stock_code}/{statement_slug}", response_class=HTMLResponse)
def get_public_statement_page(
    stock_code: str,
    statement_slug: str,
    language: Literal["zh", "en"] = Query(default="zh", alias="lang"),
) -> HTMLResponse:
    """Render an indexable HTML statement page for one stock and statement."""
    # When called directly in tests, FastAPI's Query default is not resolved.
    if language not in ("zh", "en"):
        language = "zh"
    if not re.fullmatch(r"\d{6}", stock_code):
        raise HTTPException(status_code=404, detail="Stock not found")
    statement_info = PUBLIC_STATEMENTS.get(statement_slug)
    if statement_info is None:
        raise HTTPException(status_code=404, detail="Statement not found")

    statement, title_en, title_zh = statement_info
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            items = storage.get_statement_items(statement, stock_code)
            available_stocks = storage.get_statement_stock_codes(statement)
            stock = next(
                (entry for entry in available_stocks
                 if entry["stock_code"] == stock_code),
                None,
            )
        except Exception as exc:
            logger.warning("Unable to render public statement page", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to render statement") from exc

    if not items:
        raise HTTPException(status_code=404, detail="Statement not found")

    stock_name = (stock or {}).get("stock_name") or stock_code
    dates = sorted({str(item["reporting_date"]) for item in items}, reverse=True)
    latest_items: dict[tuple[str, int], dict] = {}
    for item in items:
        latest_items[(str(item["reporting_date"]), int(item["item_id"]))] = item
    hidden_item_ids = HIDDEN_BALANCE_SHEET_ITEM_IDS if statement == "balance_sheet" else set()
    item_ids = sorted({item_id for _, item_id in latest_items if item_id not in hidden_item_ids})
    available_item_ids = set(item_ids)
    children_by_parent: dict[int, list[int]] = {}
    root_item_ids: list[int] = []
    for item_id in item_ids:
        parent_id = item_id // 100
        if parent_id > 0 and parent_id in available_item_ids:
            children_by_parent.setdefault(parent_id, []).append(item_id)
        else:
            root_item_ids.append(item_id)
    ordered_item_ids: list[int] = []
    indent_levels: dict[int, int] = {}

    def append_item(item_id: int, indent_level: int) -> None:
        ordered_item_ids.append(item_id)
        indent_levels[item_id] = indent_level
        for child_id in sorted(children_by_parent.get(item_id, [])):
            append_item(child_id, indent_level + 1)

    for root_id in root_item_ids:
        append_item(root_id, 0)
    chart_items = {item.item_id: item for item in line_item_chart._items_by_id.values()}

    def amount_text(value: object, *, per_share: bool) -> str:
        if value is None or str(value).strip() == "":
            return "—"
        try:
            number = Decimal(str(value))
            if not per_share:
                number /= Decimal(PUBLIC_AMOUNT_UNITS["百万元"])
            formatted = f"{number:,.4f}" if per_share else f"{number:,.2f}"
            if per_share:
                integer, _, fraction = formatted.partition(".")
                fraction = fraction.rstrip("0")
                fraction += "0" * max(0, 2 - len(fraction))
                formatted = f"{integer}.{fraction}"
            return escape(formatted)
        except (InvalidOperation, ValueError):
            return escape(str(value))

    rows = []
    for item_id in ordered_item_ids:
        chart_item = chart_items.get(item_id)
        label_cn = chart_item.name if chart_item else f"Item {item_id}"
        label_en = chart_item.english_name if chart_item else f"Item {item_id}"
        item_label = label_cn if language == "zh" else label_en
        item_language = "zh-CN" if language == "zh" else "en"
        per_share = statement == "income_statement" and item_id in {EPS_ID, EPS_DILUTED_ID}
        cells = "".join(
            f"<td class=\"amount-column\" data-reporting-date=\"{escape(date, quote=True)}\" data-amount=\"{escape(str(latest_items.get((date, item_id), {}).get('amount') if latest_items.get((date, item_id), {}).get('amount') is not None else ''), quote=True)}\" data-per-share=\"{'true' if per_share else 'false'}\">"
            f"{amount_text(latest_items.get((date, item_id), {}).get('amount'), per_share=per_share)}</td>"
            for date in dates
        )
        toggle = (
            f'<button type="button" class="item-toggle" data-item-toggle="{item_id}" '
            f'aria-label="{escape(("展开" if language == "zh" else "Show") + " " + item_label)}" '
            f'aria-expanded="true">▼</button>'
            if item_id in children_by_parent
            else '<span class="item-toggle-placeholder" aria-hidden="true"></span>'
        )
        group_class = " item-row-group" if item_id in children_by_parent else ""
        rows.append(
            f'<tr class="item-row{group_class}" data-item-id="{item_id}"><th scope="row" class="item-name" style="padding-left: {10 + indent_levels[item_id] * 24}px">{toggle}'
            f"<span lang=\"{item_language}\">{escape(item_label)}</span>"
            f"</th>{cells}</tr>"
        )

    page_url = f"/stocks/{stock_code}/{statement_slug}"
    localized_title = title_zh if language == "zh" else title_en
    localized_description = (
        f"{title_zh}及{stock_name}（{stock_code}）各报告期数据。"
        if language == "zh" else
        f"{title_en} and reporting-period data for {stock_name} ({stock_code})."
    )
    title = f"{stock_name} ({stock_code}) {localized_title} | Easy10K"
    description = localized_description
    table_item_label = "财务项目" if language == "zh" else "Financial item"
    reporting_periods_label = "个报告期" if language == "zh" else "reporting periods"
    language_label = "语言" if language == "zh" else "Language"
    home_label = "首页" if language == "zh" else "Dashboard"
    amount_unit_label = "金额单位" if language == "zh" else "Amount unit"
    show_decimals_label = "显示小数" if language == "zh" else "Show decimals"
    stock_search_options = "".join(
        f'<button type="button" class="stock-suggestion" role="option" data-stock-code="{escape(str(entry.get("stock_code") or ""), quote=True)}" data-stock-name="{escape(str(entry.get("stock_name") or entry.get("stock_code") or ""), quote=True)}"><span>{escape(str(entry.get("stock_code") or ""))}</span><span>{escape(str(entry.get("stock_name") or entry.get("stock_code") or ""))}</span></button>'
        for entry in available_stocks
        if re.fullmatch(r"\d{6}", str(entry.get("stock_code") or ""))
    )
    stock_directory = {
        str(entry.get("stock_code")): str(entry.get("stock_name") or entry.get("stock_code"))
        for entry in available_stocks
        if re.fullmatch(r"\d{6}", str(entry.get("stock_code") or ""))
    }
    stock_directory_json = json.dumps(stock_directory, ensure_ascii=False).replace("<", "\\u003c")
    recommendation_title = "推荐股票" if language == "zh" else "Recommended stocks"
    recommendation_subtitle = "最近访问" if language == "zh" else "Recently visited"
    recommendation_hide = "收起" if language == "zh" else "Hide"
    recommendation_show = "展开" if language == "zh" else "Show"
    period_caption_labels = (
        {"03": "一季报", "06": "半年报", "09": "三季报", "12": "年报"}
        if language == "zh" else
        {"03": "Q1", "06": "Semiannual", "09": "Q3", "12": "Annual"}
    )

    def reporting_date_caption(date: str) -> str:
        match = re.fullmatch(r"(\d{4})-(03|06|09|12)-(30|31)", date)
        if not match:
            return date
        year, month, _day = match.groups()
        label = period_caption_labels[month]
        return f"{year}年{label}" if language == "zh" else f"{label} {year}"

    amount_unit_options = "".join(
        f"<option value=\"{unit}\"{' selected' if unit == '百万元' else ''}>{unit if language == 'zh' else unit + ' CNY'}</option>"
        for unit in PUBLIC_AMOUNT_UNITS
    )
    statement_tabs = (
        ("balance-sheet", "资产负债表", "Balance Sheet"),
        ("income-statement", "利润表", "Income Statement"),
        ("cash-flow-statement", "现金流量表", "Cash Flow Statement"),
    )
    period_filters = (
        ("", "全部", "All"),
        ("12-31", "年报", "Annual"),
        ("06-30", "半年报", "Semiannual"),
        ("03-31", "一季报", "Q1"),
        ("09-30", "三季报", "Q3"),
    )
    statement_tabs_label = "财务报表" if language == "zh" else "Financial statements"
    period_filters_label = "报告期筛选" if language == "zh" else "Reporting period filter"
    statement_tab_links = "".join(
        f'<a class="{"active" if current_slug == statement_slug else ""}" href="/stocks/{escape(stock_code)}/{current_slug}?lang={language}" aria-current="{"page" if current_slug == statement_slug else "false"}">{escape(label_zh if language == "zh" else label_en)}</a>'
        for current_slug, label_zh, label_en in statement_tabs
    )
    period_filter_buttons = "".join(
        f'<button type="button" class="{"active" if ending == "" else ""}" data-period-ending="{ending}" aria-pressed="{"true" if ending == "" else "false"}">{escape(label_zh if language == "zh" else label_en)}</button>'
        for ending, label_zh, label_en in period_filters
    )
    structured_data = json.dumps({
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": title,
        "description": description,
        "url": page_url,
        "temporalCoverage": f"{dates[-1]}/{dates[0]}",
    }, ensure_ascii=False).replace("<", "\\u003c")
    html = f"""<!doctype html>
<html lang="{'zh-CN' if language == 'zh' else 'en'}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <meta name="description" content="{escape(description)}">
  <link rel="canonical" href="{page_url}">
  <script type="application/ld+json">{structured_data}</script>
  <style>
    @view-transition {{ navigation: auto; }}
    ::view-transition-old(root), ::view-transition-new(root) {{ animation-duration: 180ms; }}
    html {{ font-size: 90%; }}
    body {{ margin: 0; color: #20283b; background: #f5f6fa; font: 14.4px system-ui, sans-serif; }}
    main {{ max-width: 1400px; margin: 0 auto; padding: 32px 20px 64px; }}
    h1 {{ margin: 0 0 8px; font-size: 1.8rem; }}
    p {{ color: #5f687b; }}
    .site-logo {{ margin-bottom: 0; color: #51488e; font-size: 2.2rem; font-weight: 850; letter-spacing: -.02em; line-height: 1; }}
    .site-logo strong {{ background: linear-gradient(110deg, #51488e 0%, #7567c5 52%, #9b75d1 100%); -webkit-background-clip: text; background-clip: text; color: transparent; }}
    .site-logo span {{ margin-left: 8px; color: #737c8e; font-size: 1.35rem; font-weight: 700; letter-spacing: .04em; }}
    .brand-line {{ display: flex; align-items: center; gap: 12px; }}
    .brand-icon {{ display: inline-flex; width: 34px; height: 34px; flex: 0 0 34px; align-items: center; justify-content: center; padding: 7px; box-sizing: border-box; border-radius: 10px; background: linear-gradient(135deg, #51488e, #8877d0); color: #fff; box-shadow: 0 4px 12px #51488e26; }}
    .brand-icon svg {{ display: block; width: 100%; height: 100%; }}
    .breadcrumb {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; color: #737c8e; font-size: .8rem; font-weight: 650; }}
    .breadcrumb a {{ color: #6860ad; text-decoration: none; }}
    .breadcrumb a:hover {{ text-decoration: underline; }}
    .breadcrumb-separator {{ color: #b0b4c2; }}
    .page-header {{ position: relative; display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 24px; }}
    .page-header > div:first-child {{ width: 100%; }}
    .title-row {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; width: 100%; }}
    .page-tools {{ position: absolute; top: 0; right: 0; display: flex; align-items: flex-start; justify-content: flex-end; gap: 12px; }}
    .stock-search {{ position: relative; display: flex; width: 206px; align-items: center; flex: 0 0 206px; gap: 6px; margin-left: auto; transform: translateY(-4px); }}
    .stock-search input {{ width: 168px; height: 32px; padding: 0 9px; box-sizing: border-box; border: 1px solid #dce0e9; border-radius: 8px; background: #fff; color: #24283d; font: inherit; font-size: calc(.8rem + 2px); }}
    .stock-search input:focus {{ outline: 2px solid #bcb4ed; outline-offset: 1px; border-color: #6860ad; }}
    .stock-search > button {{ display: inline-flex; width: 32px; height: 32px; align-items: center; justify-content: center; padding: 0; border: 0; border-radius: 8px; background: #6860ad; color: #fff; cursor: pointer; }}
    .stock-search > button svg {{ width: 16px; height: 16px; }}
    .stock-search > button:hover {{ background: #51488e; }}
    .stock-suggestions {{ position: absolute; z-index: 10; top: calc(100% + 6px); right: 0; width: 206px; max-height: 280px; overflow-y: auto; padding: 4px; box-sizing: border-box; border: 1px solid #dce0e9; border-radius: 9px; background: #fff; box-shadow: 0 8px 24px #20283b1a; }}
    .stock-suggestions[hidden] {{ display: none; }}
    .stock-suggestion {{ display: flex; width: 100%; height: auto; align-items: center; justify-content: space-between; padding: 8px 10px; border: 0; border-radius: 6px; background: transparent; color: #343b50; cursor: pointer; font: inherit; font-size: calc(.8rem + 2px); text-align: left; }}
    .stock-suggestion[hidden] {{ display: none; }}
    .stock-suggestion:hover, .stock-suggestion:focus {{ background: #f0eef9; outline: none; }}
    .stock-suggestion span:last-child {{ margin-left: 16px; color: #737c8e; }}
    .recommendation-panel {{ margin: 0 0 16px; padding: 14px 16px 16px; border: 1px solid #e0e3ec; border-radius: 12px; background: #f0eef9; }}
    .recommendation-header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }}
    .recommendation-heading {{ display: flex; align-items: baseline; gap: 10px; margin: 0; }}
    .recommendation-heading h2 {{ margin: 0; color: #343b50; font-size: .95rem; font-weight: 800; }}
    .recommendation-heading span {{ color: #737c8e; font-size: .78rem; }}
    .recommendation-toggle {{ padding: 4px 8px; border: 0; border-radius: 6px; background: transparent; color: #6860ad; cursor: pointer; font: inherit; font-size: .76rem; font-weight: 700; }}
    .recommendation-toggle:hover {{ background: #e8e5f7; }}
    .recommendation-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }}
    .recommendation-card {{ display: flex; min-width: 0; flex-direction: column; gap: 3px; padding: 9px 10px; border: 1px solid #dfdcef; border-radius: 8px; background: #fff; color: #343b50; text-decoration: none; }}
    .recommendation-card:hover {{ border-color: #aaa0e1; background: #faf9ff; }}
    .recommendation-card strong {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: .82rem; }}
    .recommendation-card span {{ overflow: hidden; color: #737c8e; font-size: .76rem; text-overflow: ellipsis; white-space: nowrap; }}
    .home-link {{ display: inline-flex; align-items: center; gap: 5px; padding: 7px 10px; border: 1px solid #e0e3ec; border-radius: 8px; background: #fff; color: #6860ad; font-size: .78rem; font-weight: 700; text-decoration: none; white-space: nowrap; }}
    .home-link:hover {{ background: #f0eef9; border-color: #cfc9ef; }}
    .language-switcher {{ display: flex; align-items: center; gap: 8px; flex-shrink: 0; }}
    .language-switcher span {{ color: #737c8e; font-size: .78rem; }}
    .language-toggle {{ display: flex; gap: 2px; padding: 3px; background: #fff; border: 1px solid #e0e3ec; border-radius: 9px; box-shadow: 0 1px 2px #20283b05; }}
    .language-toggle a {{ padding: 6px 10px; border-radius: 6px; color: #6860ad; font-size: .72rem; font-weight: 700; text-decoration: none; }}
    .language-toggle a.active {{ background: #6860ad; color: #fff; }}
    .report-tools {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding-left: 16px; border-left: 1px solid #dfe2eb; }}
    .report-tools label {{ margin: 0; color: #343b50; font-size: .78rem; font-weight: 700; white-space: nowrap; }}
    .statement-navigation {{ display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 0; border-bottom: 1px solid #e0e3ec; }}
    .filter-row {{ display: flex; align-items: center; justify-content: flex-end; gap: 16px; margin: 0 0 4px auto; }}
    .statement-tabs {{ display: flex; flex-shrink: 0; gap: 8px; margin: 0; padding: 0; }}
    .statement-tabs a {{ padding: 12px 20px; border-radius: 9px 9px 0 0; background: #e8e5f7; color: #51488e; font-size: 1rem; font-weight: 700; text-decoration: none; }}
    .statement-tabs a:hover {{ background: #f0eef9; }}
    .statement-tabs a.active {{ margin-bottom: -1px; color: #fff; background: #6860ad; }}
    .period-filters {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 0; margin: 0 0 0 auto; }}
    .period-filters button {{ padding: 8px 14px; border: 0; border-radius: 0; background: #e8e5f7; color: #514d91; cursor: pointer; font: inherit; font-size: .82rem; font-weight: 700; }}
    .period-filters button:first-child {{ border-radius: 9px 0 0 9px; }}
    .period-filters button:last-child {{ border-radius: 0 9px 9px 0; }}
    .period-filters button:hover {{ background: #ded9f2; }}
    .period-filters button.active {{ background: #6860ad; color: #fff; }}
    .amount-unit-control {{ display: flex; align-items: center; gap: 6px; }}
    select {{ height: 30px; padding: 0 28px 0 9px; border: 1px solid #dce0e9; border-radius: 8px; background: #fafbfe; color: #24283d; font: inherit; font-size: .8rem; }}
    .decimal-toggle {{ display: flex; align-items: center; gap: 5px; }}
    .decimal-toggle input {{ accent-color: #6860ad; }}
    .table-wrap {{ overflow-x: auto; background: #fff; border: 1px solid #e0e3ec; border-top-left-radius: 0; border-top-right-radius: 0; box-shadow: 0 2px 6px #20283b04, 0 12px 32px #20283b03; }}
    table {{ border-collapse: collapse; min-width: 100%; table-layout: fixed; width: 100%; }}
    th, td {{ border-top: 1px solid #eceef4; padding: 11px 10px; text-align: left; white-space: nowrap; }}
    thead th:first-child, td.item-name {{ box-sizing: border-box; width: 260px; min-width: 260px; max-width: 260px; overflow: hidden; text-overflow: ellipsis; }}
    col.item-name-column {{ width: 260px; }}
    th.amount-column, td.amount-column {{ min-width: 110px; padding-left: 6px; padding-right: 6px; }}
    col.amount-column {{ width: 120px; }}
    th {{ background: #30364d; color: #f4f2fc; border-top-color: #3e455f; font-size: .72rem; font-weight: 550; letter-spacing: .04em; text-transform: uppercase; }}
    thead th, thead th.amount-column {{ text-align: center; }}
    td {{ color: #505a70; font-size: .84rem; }}
    .item-name {{ color: #fff; font-size: 0.88rem; font-weight: 500; text-transform: none; }}
    .item-row {{ transition: background-color 120ms ease; }}
    .item-row:hover {{ background: #f0eef9; }}
    .item-row-group {{ background: #f7f8fc; }}
    .item-row-group > th {{ color: #fff; font-weight: 750; }}
    .item-toggle {{ width: 16px; margin-right: 8px; padding: 0; border: 0; background: transparent; color: #d8d3ff; cursor: pointer; font-size: .72rem; }}
    .item-toggle:hover {{ color: #fff; background: transparent; }}
    .item-toggle-placeholder {{ display: inline-block; width: 16px; margin-right: 8px; }}
    thead th:first-child, td.item-name {{ border-right: 1px solid #d6d3e4; }}
    thead th:first-child {{ border-right-color: #504d70; }}
    .amount-column {{ font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1; text-align: right; }}
    @media (prefers-reduced-motion: reduce) {{ ::view-transition-old(root), ::view-transition-new(root) {{ animation-duration: 0ms; }} }}
    @media (max-width: 900px) {{ .statement-navigation {{ align-items: flex-start; flex-direction: column; gap: 8px; }} .filter-row {{ align-items: flex-start; flex-direction: column; width: 100%; margin: 0 0 12px; }} .period-filters {{ margin-left: 0; justify-content: flex-start; }} .report-tools {{ justify-content: flex-start; flex-wrap: wrap; padding-left: 0; border-left: 0; }} .recommendation-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}
    @media (max-width: 600px) {{ .page-header {{ flex-direction: column; gap: 12px; }} .page-header > div:first-child {{ width: auto; }} .title-row {{ align-items: flex-start; flex-direction: column; gap: 10px; }} .stock-search {{ align-self: flex-end; width: min(100%, 206px); flex-basis: min(100%, 206px); transform: none; }} .stock-suggestions {{ width: 100%; }} .page-tools {{ position: static; align-items: stretch; flex-wrap: wrap; width: 100%; }} .stock-search input {{ flex: 1; width: auto; }} .language-switcher {{ margin-left: auto; }} }}
  </style>
</head>
<body><main>
  <div class="page-header">
    <div>
      <div class="brand-line">
        <span class="brand-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M4 19V5M4 19h16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="m7 15 3-4 3 2 5-7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </span>
        <div class="site-logo" aria-label="ValuePaper 上市公司财报助手"><strong>ValuePaper</strong><span>财报数据助手</span></div>
      </div>
      <nav class="breadcrumb">
        <span>{escape(stock_name)}</span>
        <span class="breadcrumb-separator" aria-hidden="true">·</span>
        <span>{escape(localized_title)}</span>
      </nav>
      <div class="title-row">
        <h1>{escape(stock_name)} ({escape(stock_code)}) · {escape(localized_title)}</h1>
        <form class="stock-search" id="stock-search" role="search">
          <input id="stock-search-code" name="stock_code" type="search" autocomplete="off" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" placeholder="{'股票代码' if language == 'zh' else 'Stock code'}" aria-label="{'搜索股票代码' if language == 'zh' else 'Search stock code'}" aria-controls="stock-search-suggestions" aria-expanded="false" required>
          <button type="submit" aria-label="{'搜索' if language == 'zh' else 'Search'}" title="{'搜索' if language == 'zh' else 'Search'}">
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.3" stroke="currentColor" stroke-width="2"/><path d="m16 16 4.2 4.2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          </button>
          <div class="stock-suggestions" id="stock-search-suggestions" role="listbox" hidden>{stock_search_options}</div>
        </form>
      </div>
      <p lang="{'zh-CN' if language == 'zh' else 'en'}">{escape(title_zh if language == 'zh' else title_en)} · {len(dates)} {reporting_periods_label}</p>
    </div>
    <div class="page-tools">
      <a class="home-link" href="/" aria-label="{escape(home_label)}">⌂ {escape(home_label)}</a>
      <nav class="language-switcher" aria-label="{escape(language_label)}">
        <div class="language-toggle">
          <a class="{'active' if language == 'zh' else ''}" href="{page_url}?lang=zh" lang="zh-CN" aria-current="{'page' if language == 'zh' else 'false'}">中文</a>
          <a class="{'active' if language == 'en' else ''}" href="{page_url}?lang=en" lang="en" aria-current="{'page' if language == 'en' else 'false'}">EN</a>
        </div>
      </nav>
    </div>
  </div>
  <section class="recommendation-panel" aria-labelledby="recommendation-title">
    <div class="recommendation-header">
      <div class="recommendation-heading"><h2 id="recommendation-title">{escape(recommendation_title)}</h2><span>{escape(recommendation_subtitle)}</span></div>
      <button type="button" class="recommendation-toggle" id="recommendation-toggle" aria-expanded="true">{escape(recommendation_hide)}</button>
    </div>
    <div class="recommendation-grid" id="recent-stock-recommendations"></div>
  </section>
  <div class="statement-navigation">
    <nav class="statement-tabs" aria-label="{escape(statement_tabs_label)}">{statement_tab_links}</nav>
    <div class="filter-row">
      <div class="period-filters" role="group" aria-label="{escape(period_filters_label)}">{period_filter_buttons}</div>
      <div class="report-tools">
        <label class="amount-unit-control" for="amount-unit"><span>{escape(amount_unit_label)}</span>
          <select id="amount-unit">{amount_unit_options}</select>
        </label>
        <label class="decimal-toggle"><input id="show-decimals" type="checkbox" checked> {escape(show_decimals_label)}</label>
      </div>
    </div>
  </div>
  <div class="table-wrap"><table data-date-count="{len(dates)}" style="--date-column-count: {len(dates)}; --date-column-min-width: 110px">
    <colgroup><col class="item-name-column">{''.join(f'<col class="amount-column" data-reporting-date="{escape(date, quote=True)}">' for date in dates)}</colgroup>
    <thead><tr><th scope="col">{table_item_label}</th>{''.join(f'<th scope="col" data-reporting-date="{escape(date)}">{escape(reporting_date_caption(date))}</th>' for date in dates)}</tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
</main>
<script>
  const units = {json.dumps(PUBLIC_AMOUNT_UNITS, ensure_ascii=False)};
  const unitSelect = document.getElementById('amount-unit');
  const decimalsToggle = document.getElementById('show-decimals');
  const periodButtons = document.querySelectorAll('.period-filters button');
  const itemRows = document.querySelectorAll('[data-item-id]');
  const itemToggles = document.querySelectorAll('[data-item-toggle]');
  const amountUnitPreferenceKey = 'easy10k.amountUnit';
  const showDecimalsPreferenceKey = 'easy10k.showDecimals';
  const periodEndingPreferenceKey = 'easy10k.periodEnding';
  const statementScrollPreferenceKey = 'easy10k.statementScrollY';
  const recentStocksPreferenceKey = 'easy10k.recentStocks';
  const stockDirectory = {stock_directory_json};
  const recommendationGrid = document.getElementById('recent-stock-recommendations');
  const recommendationToggle = document.getElementById('recommendation-toggle');
  const currentStockCode = '{escape(stock_code, quote=True)}';
  function loadRecentStocks() {{
    try {{
      const stored = JSON.parse(localStorage.getItem(recentStocksPreferenceKey) || '[]');
      return Array.isArray(stored) ? stored.filter((code) => Object.prototype.hasOwnProperty.call(stockDirectory, code)) : [];
    }} catch (_) {{
      return [];
    }}
  }}
  function renderRecentStocks() {{
    const recentStocks = loadRecentStocks();
    recommendationGrid.replaceChildren();
    recentStocks.slice(0, 10).forEach((code) => {{
      const link = document.createElement('a');
      link.className = 'recommendation-card';
      link.href = `/stocks/${{encodeURIComponent(code)}}/{statement_slug}?lang={language}`;
      const codeLabel = document.createElement('strong');
      codeLabel.textContent = code;
      const nameLabel = document.createElement('span');
      nameLabel.textContent = stockDirectory[code];
      link.append(codeLabel, nameLabel);
      recommendationGrid.append(link);
    }});
  }}
  const recentStocks = loadRecentStocks().filter((code) => code !== currentStockCode);
  recentStocks.unshift(currentStockCode);
  localStorage.setItem(recentStocksPreferenceKey, JSON.stringify(recentStocks.slice(0, 10)));
  renderRecentStocks();
  recommendationToggle.addEventListener('click', () => {{
    const expanded = recommendationToggle.getAttribute('aria-expanded') === 'true';
    recommendationGrid.hidden = expanded;
    recommendationToggle.setAttribute('aria-expanded', String(!expanded));
    recommendationToggle.textContent = expanded ? '{escape(recommendation_show)}' : '{escape(recommendation_hide)}';
  }});
  const stockSearch = document.getElementById('stock-search');
  const stockSearchCode = document.getElementById('stock-search-code');
  const stockSuggestions = document.getElementById('stock-search-suggestions');
  const stockSuggestionButtons = Array.from(stockSuggestions.querySelectorAll('.stock-suggestion'));
  function renderStockSuggestions() {{
    const query = stockSearchCode.value.trim().toLowerCase();
    let visible = 0;
    stockSuggestionButtons.forEach((suggestion) => {{
      const matches = !query || suggestion.dataset.stockCode.includes(query) || suggestion.dataset.stockName.toLowerCase().includes(query);
      suggestion.hidden = !matches;
      if (matches) visible += 1;
    }});
    stockSuggestions.hidden = visible === 0;
    stockSearchCode.setAttribute('aria-expanded', String(visible > 0));
  }}
  stockSearchCode.addEventListener('focus', renderStockSuggestions);
  stockSearchCode.addEventListener('input', renderStockSuggestions);
  stockSuggestionButtons.forEach((suggestion) => {{
    suggestion.addEventListener('click', () => {{
      stockSearchCode.value = suggestion.dataset.stockCode;
      stockSuggestions.hidden = true;
      stockSearchCode.setAttribute('aria-expanded', 'false');
      stockSearchCode.focus();
    }});
  }});
  document.addEventListener('click', (event) => {{
    if (!stockSearch.contains(event.target)) {{
      stockSuggestions.hidden = true;
      stockSearchCode.setAttribute('aria-expanded', 'false');
    }}
  }});
  stockSearch.addEventListener('submit', (event) => {{
    event.preventDefault();
    const code = stockSearchCode.value.trim();
    if (!/^[0-9]{{6}}$/.test(code)) {{
      stockSearchCode.reportValidity();
      return;
    }}
    window.location.href = `/stocks/${{encodeURIComponent(code)}}/balance-sheet?lang={language}`;
  }});
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  function resizeDateColumns() {{
    const table = document.querySelector('.table-wrap table');
    if (!table) return;
    const count = table.querySelectorAll('col.amount-column:not([hidden])').length;
    if (!count) return;
    const minimumWidth = Number.parseFloat(getComputedStyle(table).getPropertyValue('--date-column-min-width')) || 120;
    const availableWidth = Math.max(table.parentElement.clientWidth, 260 + count * minimumWidth);
    const dateColumnWidth = (availableWidth - 260) / count;
    table.style.width = '100%';
    table.style.minWidth = `${{availableWidth}}px`;
    table.querySelectorAll('col.amount-column').forEach((column) => {{
      column.style.width = `${{dateColumnWidth}}px`;
    }});
  }}
  function renderPeriods(ending) {{
    document.querySelectorAll('[data-reporting-date]').forEach((cell) => {{
      cell.hidden = Boolean(ending) && !cell.dataset.reportingDate.endsWith(ending);
    }});
    periodButtons.forEach((button) => {{
      const active = button.dataset.periodEnding === ending;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    }});
    resizeDateColumns();
  }}
  function renderItemHierarchy() {{
    itemRows.forEach((row) => {{
      let parentId = Math.floor(Number(row.dataset.itemId) / 100);
      let hidden = false;
      while (parentId > 0) {{
        if (collapsedItems.has(parentId)) {{
          hidden = true;
          break;
        }}
        parentId = Math.floor(parentId / 100);
      }}
      row.hidden = hidden;
    }});
    itemToggles.forEach((toggle) => {{
      const itemId = Number(toggle.dataset.itemToggle);
      const collapsed = collapsedItems.has(itemId);
      toggle.textContent = collapsed ? '▶' : '▼';
      toggle.setAttribute('aria-expanded', String(!collapsed));
    }});
  }}
  const collapsedItems = new Set();
  itemToggles.forEach((toggle) => {{
    toggle.addEventListener('click', () => {{
      const itemId = Number(toggle.dataset.itemToggle);
      if (collapsedItems.has(itemId)) collapsedItems.delete(itemId);
      else collapsedItems.add(itemId);
      renderItemHierarchy();
    }});
  }});
  function renderAmounts() {{
    const unit = unitSelect.value;
    const showDecimals = decimalsToggle.checked;
    const dateColumnWidth = showDecimals && unit === '元' ? 140 : (unit === '百万元' || unit === '亿元' ? 110 : 120);
    const table = document.querySelector('.table-wrap table');
    if (table) table.style.setProperty('--date-column-min-width', `${{dateColumnWidth}}px`);
    resizeDateColumns();
    document.querySelectorAll('[data-amount]').forEach((cell) => {{
      const raw = Number(cell.dataset.amount);
      if (!Number.isFinite(raw)) return;
      const perShare = cell.dataset.perShare === 'true';
      const value = raw / (perShare ? 1 : units[unit]);
      cell.textContent = value.toLocaleString(undefined, {{
        minimumFractionDigits: showDecimals ? 2 : 0,
        maximumFractionDigits: perShare ? 4 : showDecimals ? 2 : 0,
      }});
    }});
  }}
  periodButtons.forEach((button) => {{
    button.addEventListener('click', () => {{
      const ending = button.dataset.periodEnding;
      localStorage.setItem(periodEndingPreferenceKey, ending);
      renderPeriods(ending);
    }});
  }});
  document.querySelectorAll('.statement-tabs a').forEach((link) => {{
    link.addEventListener('click', () => {{
      sessionStorage.setItem(statementScrollPreferenceKey, String(window.scrollY));
    }});
    link.addEventListener('pointerenter', () => {{
      if (navigator.connection && navigator.connection.saveData) return;
      if (link.dataset.prefetched) return;
      link.dataset.prefetched = 'true';
      const prefetch = document.createElement('link');
      prefetch.rel = 'prefetch';
      prefetch.href = link.href;
      document.head.appendChild(prefetch);
    }}, {{ once: true }});
  }});
  function restoreStatementScroll() {{
    const storedScroll = sessionStorage.getItem(statementScrollPreferenceKey);
    if (storedScroll === null) return;
    const scrollY = Number(storedScroll);
    sessionStorage.removeItem(statementScrollPreferenceKey);
    if (!Number.isFinite(scrollY)) return;
    requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo(0, scrollY)));
  }}
  window.addEventListener('pageshow', restoreStatementScroll);
  restoreStatementScroll();
  unitSelect.addEventListener('change', () => {{
    localStorage.setItem(amountUnitPreferenceKey, unitSelect.value);
    renderAmounts();
  }});
  decimalsToggle.addEventListener('change', () => {{
    localStorage.setItem(showDecimalsPreferenceKey, String(decimalsToggle.checked));
    renderAmounts();
  }});
  const storedUnit = localStorage.getItem(amountUnitPreferenceKey);
  if (storedUnit && Object.prototype.hasOwnProperty.call(units, storedUnit)) {{
    unitSelect.value = storedUnit;
  }}
  const storedDecimals = localStorage.getItem(showDecimalsPreferenceKey);
  if (storedDecimals === 'true' || storedDecimals === 'false') {{
    decimalsToggle.checked = storedDecimals === 'true';
  }}
  const storedEnding = localStorage.getItem(periodEndingPreferenceKey);
  const validEnding = Array.from(periodButtons).some((button) => button.dataset.periodEnding === storedEnding);
  renderPeriods(validEnding ? storedEnding : '');
  renderItemHierarchy();
  renderAmounts();
  window.addEventListener('resize', resizeDateColumns);
</script>
</body></html>"""
    return HTMLResponse(html, headers={"X-Content-Type-Options": "nosniff"})

@app.get("/line-item-chart")
def get_line_item_chart() -> list[dict[str, int | str | None]]:
    """Return the financial statement line-item chart as JSON."""
    return [
        {
            "item_id": item.item_id,
            "name_cn": item.name,
            "name_en": item.english_name,
        }

        for item in line_item_chart._items_by_id.values()
    ]

@app.get("/balance_sheet_items")
def get_balance_sheet_stock_codes() -> list[dict]:
    """List stocks that have stored balance-sheet items and their report counts."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.get_balance_sheet_stock_codes()
        except Exception as exc:
            logger.warning("Unable to fetch balance-sheet stock codes", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch stock codes") from exc


@app.get("/balance_sheet_items/{stock_code}")
def get_balance_sheet_items(stock_code: str) -> list[dict]:
    """Return all balance-sheet items stored for a stock code."""
    storage = LineItemStorage()
    if not storage.database_url:
        logger.warning(f"DATABASE_URL is not configured")
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    logger.info(f"get_balance_sheet_items {stock_code}")
    try:
        with storage:
            return storage.get_balance_sheet_items(stock_code)
    except Exception as exc:
        logger.warning(f"database connect error:{exc}")
        raise HTTPException(
            status_code=503,
            detail="Unable to fetch balance-sheet items",
        ) from exc

@app.get("/income_statement_items")
def get_income_statement_stock_codes() -> list[dict]:
    return get_statement_stock_codes("income_statement")


@app.get("/cash_flow_statement_items")
def get_cash_flow_statement_stock_codes() -> list[dict]:
    return get_statement_stock_codes("cash_flow_statement")


def get_statement_stock_codes(statement: str) -> list[dict]:
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.get_statement_stock_codes(statement)
        except Exception as exc:
            logger.warning("Unable to fetch statement stock codes", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch stock codes") from exc


@app.get("/income_statement_items/{stock_code}")
def get_income_statement_items(stock_code: str) -> list[dict]:
    return get_statement_items("income_statement", stock_code)


@app.get("/cash_flow_statement_items/{stock_code}")
def get_cash_flow_statement_items(stock_code: str) -> list[dict]:
    return get_statement_items("cash_flow_statement", stock_code)


def get_statement_items(statement: str, stock_code: str) -> list[dict]:
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.get_statement_items(statement, stock_code)
        except Exception as exc:
            logger.warning("Unable to fetch statement items", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch statement items") from exc


def source_pdf_path(local_path):
    if not local_path:
        raise HTTPException(status_code=404, detail="Source PDF is unavailable")
    path = (PROJECT_DIR / local_path).resolve()
    if not path.is_relative_to((PROJECT_DIR / "reports").resolve()):
        raise HTTPException(status_code=403, detail="Source is outside report directories")
    if path.suffix.lower() != ".pdf" or not path.is_file():
        raise HTTPException(status_code=404, detail="Source PDF is unavailable")
    return path


@app.get("/balance_sheet_items/{stock_code}/{reporting_date}/{item_id}/source")
def get_item_source(stock_code: str, reporting_date: str, item_id: int,
                    report_creation_time: str = Query(min_length=1)):
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            record = storage.get_balance_sheet_source(stock_code, reporting_date, item_id, report_creation_time)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            logger.warning("Unable to resolve source report", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to resolve source report") from error
    if not record:
        raise HTTPException(status_code=404, detail="No source PDF registered for this report version")
    download_url = record.get("download_url")
    parsed_url = urlsplit(download_url or "")
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        download_url = None
    fallback = {"pdf_hash": record["pdf_hash"], "download_url": download_url}
    try:
        path = source_pdf_path(record["local_path"])
        source = locate_source(record, item_id, path, SOURCE_CACHE_DIR)
    except HTTPException as error:
        raise HTTPException(status_code=error.status_code, detail={"message": error.detail, **fallback}) from error
    except (OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail={"message": str(error), **fallback}) from error
    page_url = f"/report-source/{source['pdf_hash']}/page/{source['page_number']}.png"
    source["page_image_url"] = page_url
    source["image_url"] = f"{page_url}?top={source['excerpt_top']}&bottom={source['excerpt_bottom']}"
    source["download_url"] = download_url
    return source


@app.get("/report-source/{pdf_hash}/page/{page_number}.png")
def get_source_image(pdf_hash: str, page_number: int,
                     width: int = Query(default=960, ge=960, le=1600),
                     top: float = Query(default=0, ge=0, le=1),
                     bottom: float = Query(default=1, ge=0, le=1)):
    if not re.fullmatch(r"[a-f0-9]{32}", pdf_hash) or page_number < 1 or top >= bottom or width not in (960, 1600):
        raise HTTPException(status_code=422, detail="Invalid source image parameters")
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            local_path = storage.get_digestion_pdf_path(pdf_hash)
        except Exception as error:
            logger.warning("Unable to fetch source PDF path", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to load source image") from error
    path = source_pdf_path(local_path)
    try:
        image_path = render_page(path, pdf_hash, page_number, SOURCE_CACHE_DIR,
                                 width=width, top=top, bottom=bottom)
    except (OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(image_path, media_type="image/png", headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/submit/")
def submit_word(data: WordInput):
    """
    API endpoint to receive a word and process it.
    This simulates receiving data from a client (like a web form).
    """
    # The data is automatically validated by Pydantic before this line runs.
    word = data.word_input
    
    logger.info("Server received the word: %s", word)

    # Simulate successful processing
    return {
        "status": "success",
        "message": f"The word '{word}' was successfully received and processed!"
    }

# Optional: A basic root endpoint to check if the server is running
@app.get("/")
def read_root():
    return {"message": "Welcome to the Word Input API. Use /submit/ to send a word."}

@app.get("/health")
def health():
    return {"status": "healthy"}
