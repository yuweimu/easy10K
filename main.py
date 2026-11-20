# main.py
import logging
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
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
    report_group: Literal["InProgress", "ExtractionSuccess", "AuditPass", "ManualPass", "Deprecated", "Search"] | None = None,
    pdf_hash: str | None = Query(default=None, max_length=128),
    stock_code: str | None = Query(default=None, max_length=12),
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
                digestion_stage=digestion_stage)
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
