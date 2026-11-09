# main.py
import importlib
import logging
import os
from decimal import Decimal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# 1. Initialize the FastAPI application
app = FastAPI()
logger = logging.getLogger(__name__)

# 2. Define the structure of the data we expect (Pydantic Model)
class WordInput(BaseModel):
    """Defines the structure for the data we expect from the user."""
    word_input: str

# --- API Endpoints ---

@app.get("/balance_sheet_items/{stock_code}")
def get_balance_sheet_items(stock_code: str) -> list[dict]:
    """Return all balance-sheet items stored for a stock code."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning(f"DATABASE_URL is not configured")
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    try:
        psycopg = importlib.import_module("psycopg")
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT stock_code, reporting_date, item_id,
                           report_creation_time, amount
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
        "stock_code",
        "reporting_date",
        "item_id",
        "report_creation_time",
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
