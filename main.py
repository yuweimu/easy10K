# main.py
import importlib
import logging
import os
from decimal import Decimal
from html import escape
from pathlib import Path

import markdown
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from line_item_chart import LineItemChart

# 1. Initialize the FastAPI application
app = FastAPI()
logger = logging.getLogger(__name__)
line_item_chart = LineItemChart()
REPORTS_DIR = Path(__file__).resolve().parent / "markdown"

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

    content = markdown.markdown(
        report_path.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code"],
    )
    title = escape(f"{stock_code} · {reporting_date}")
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 1100px;
            margin: 40px auto; padding: 0 20px; line-height: 1.7; color: #24283d; }}
    table {{ border-collapse: collapse; display: block; max-width: 100%; overflow-x: auto; }}
    th, td {{ border: 1px solid #ddd; padding: 8px 12px; }}
    th {{ background: #f4f3f9; }}
    img {{ max-width: 100%; }}
    pre {{ overflow-x: auto; padding: 16px; background: #f4f3f9; }}
    h1, h2, h3 {{ line-height: 1.3; }}
  </style>
</head>
<body><main>{content}</main></body>
</html>""",
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'",
            "X-Content-Type-Options": "nosniff",
        },
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
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning(f"DATABASE_URL is not configured")
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    logger.info(f"get_balance_sheet_items {stock_code}")
    try:
        psycopg = importlib.import_module("psycopg")
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT reporting_date, item_id,
                           amount
                    FROM balance_sheet_items
                    WHERE stock_code = %s
                    ORDER BY reporting_date, item_id, report_creation_time
                    """,
                    (stock_code,),
                )
                rows = cursor.fetchall()
    except Exception as exc:
        logger.warning(f"database connect error:{exc}")
        raise HTTPException(
            status_code=503,
            detail="Unable to fetch balance-sheet items",
        ) from exc

    fields = (
        "reporting_date",
        "item_id",
        "amount",
    )
    return [dict(zip(fields, row)) for row in rows]

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
