# main.py
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from line_item_chart import LineItemChart
from line_item_storage import LineItemStorage
from report_renderer import render_report

# 1. Initialize the FastAPI application
app = FastAPI()
logger = logging.getLogger(__name__)
line_item_chart = LineItemChart()
REPORTS_DIR = Path(__file__).resolve().parent / "preprocessed"

# 2. Define the structure of the data we expect (Pydantic Model)
class WordInput(BaseModel):
    """Defines the structure for the data we expect from the user."""
    word_input: str

# --- API Endpoints ---


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
