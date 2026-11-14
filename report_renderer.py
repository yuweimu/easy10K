"""Render the self-contained report viewer without coupling it to API routing."""

from functools import lru_cache
from html import escape
from pathlib import Path
import secrets
from string import Template

import markdown
from fastapi.responses import HTMLResponse


VIEW_DIR = Path(__file__).resolve().parent / "report_view"


@lru_cache(maxsize=1)
def _load_assets() -> tuple[Template, str, str]:
    """Load trusted viewer assets once per server process."""
    return (
        Template((VIEW_DIR / "report.html").read_text(encoding="utf-8")),
        (VIEW_DIR / "report.css").read_text(encoding="utf-8"),
        (VIEW_DIR / "report.js").read_text(encoding="utf-8"),
    )


def render_report(text: str, *, title: str) -> HTMLResponse:
    """Render report Markdown with the viewer and nonce-protected script."""
    content = markdown.markdown(text, extensions=["tables", "fenced_code"])
    template, styles, script = _load_assets()
    nonce = secrets.token_urlsafe(24)
    html = template.substitute(
        title=escape(title),
        content=content,
        styles=styles,
        script=script,
        script_nonce=nonce,
    )
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; "
                "style-src 'unsafe-inline'; img-src 'self' data:; "
                "base-uri 'none'; form-action 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
