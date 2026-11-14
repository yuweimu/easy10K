# main.py
import logging
import secrets
from html import escape
from pathlib import Path

import markdown
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from line_item_chart import LineItemChart
from line_item_storage import LineItemStorage

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

    content = markdown.markdown(
        report_path.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code"],
    )
    title = escape(f"{stock_code} · {reporting_date}")
    script_nonce = secrets.token_urlsafe(24)
    scrollbar_script = """
    const viewport = document.querySelector('.report-scroll');
    const content = document.querySelector('.report-content');
    const rail = document.querySelector('.report-scrollbar');
    const thumb = document.querySelector('.report-scrollbar-thumb');
    let thumbHeight = 0;
    let drag = null;
    function updateScrollbar() {
      const maxScroll = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
      thumbHeight = Math.min(rail.clientHeight, Math.max(48,
        rail.clientHeight * viewport.clientHeight / Math.max(1, viewport.scrollHeight)));
      const travel = rail.clientHeight - thumbHeight;
      thumb.style.height = thumbHeight + 'px';
      thumb.style.top = (maxScroll ? viewport.scrollTop / maxScroll * travel : 0) + 'px';
      rail.setAttribute('aria-valuemax', String(maxScroll));
      rail.setAttribute('aria-valuenow', String(Math.round(viewport.scrollTop)));
    }
    rail.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const maxScroll = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
      const travel = rail.clientHeight - thumbHeight;
      if (event.target !== thumb && travel > 0) {
        viewport.scrollTop = (event.clientY - rail.getBoundingClientRect().top
          - thumbHeight / 2) / travel * maxScroll;
      }
      drag = { y: event.clientY, scrollTop: viewport.scrollTop };
      rail.setPointerCapture(event.pointerId);
    });
    rail.addEventListener('pointermove', (event) => {
      if (!drag) return;
      const travel = rail.clientHeight - thumbHeight;
      if (travel > 0) viewport.scrollTop = drag.scrollTop + (event.clientY - drag.y)
        / travel * (viewport.scrollHeight - viewport.clientHeight);
    });
    rail.addEventListener('lostpointercapture', () => { drag = null; });
    rail.addEventListener('pointerup', () => { drag = null; });
    rail.addEventListener('pointercancel', () => { drag = null; });
    rail.addEventListener('keydown', (event) => {
      const moves = { ArrowDown: 40, ArrowUp: -40,
        PageDown: viewport.clientHeight, PageUp: -viewport.clientHeight,
        Home: -viewport.scrollHeight, End: viewport.scrollHeight };
      if (Object.hasOwn(moves, event.key)) {
        event.preventDefault();
        viewport.scrollTop += moves[event.key];
      }
    });
    rail.addEventListener('wheel', (event) => {
      event.preventDefault();
      viewport.scrollTop += event.deltaY * (event.deltaMode === 1 ? 16
        : event.deltaMode === 2 ? viewport.clientHeight : 1);
    }, { passive: false });
    viewport.addEventListener('scroll', updateScrollbar, { passive: true });
    const observer = new ResizeObserver(updateScrollbar);
    observer.observe(viewport);
    observer.observe(content);
    window.addEventListener('load', updateScrollbar);
    updateScrollbar();
    """
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    html, body {{ margin: 0; height: 100%; overflow: hidden; }}
    body {{ font-family: system-ui, sans-serif; line-height: 1.7; color: #24283d; }}
    .report-scroll {{ height: 100%; width: calc(100% - 22px);
                      overflow-y: scroll; overflow-x: auto; scrollbar-width: none; }}
    .report-content {{ max-width: 1100px; margin: 40px auto; padding: 0 20px;
                       white-space: nowrap; }}
    .report-scroll::-webkit-scrollbar {{ display: none; }}
    .report-scrollbar {{ position: fixed; top: 0; right: 0; bottom: 0; width: 22px;
      background: #e4e2ee; border-left: 1px solid #b4acd4; touch-action: none;
      user-select: none; cursor: pointer; }}
    .report-scrollbar-thumb {{ position: absolute; left: 3px; right: 3px; top: 0;
      height: 48px; border-radius: 10px; background: #51408f; cursor: grab; }}
    .report-scrollbar-thumb:hover {{ background: #30205f; }}
    .report-scrollbar:active .report-scrollbar-thumb {{ cursor: grabbing; }}
    .report-scrollbar:focus-visible {{ outline: 2px solid #30205f; outline-offset: -2px; }}
    table {{ border-collapse: collapse; display: block; max-width: 100%; overflow-x: auto; }}
    th, td {{ border: 1px solid #ddd; padding: 8px 12px; }}
    th {{ background: #f4f3f9; }}
    img {{ max-width: 100%; }}
    pre {{ overflow-x: auto; padding: 16px; background: #f4f3f9; }}
    h1, h2, h3 {{ line-height: 1.3; }}
  </style>
</head>
<body>
  <div class="report-scroll" id="report-document" tabindex="0"><main class="report-content">{content}</main></div>
  <div class="report-scrollbar" role="scrollbar" tabindex="0" aria-label="Report scroll position"
       aria-controls="report-document" aria-orientation="vertical" aria-valuemin="0"
       aria-valuemax="0" aria-valuenow="0"><div class="report-scrollbar-thumb"></div></div>
  <script nonce="{script_nonce}">{scrollbar_script}</script>
</body>
</html>""",
        headers={
            "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{script_nonce}'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'",
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
