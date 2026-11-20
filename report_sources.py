"""Locate statement rows and cache lightweight PDF page images."""

from contextlib import redirect_stdout
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import pymupdf

from digest_balance_sheet import clean_cell, parse_amount, UNITS
from line_item_chart import LineItemChart


def normalized_bbox(page, bbox):
    """Convert unrotated PDF coordinates to normalized displayed-page coordinates."""
    rect = pymupdf.Rect(bbox) * page.rotation_matrix
    bounds = page.rect
    return [max(0, min(1, value)) for value in (
        rect.x0 / bounds.width, rect.y0 / bounds.height,
        rect.x1 / bounds.width, rect.y1 / bounds.height,
    )]


@lru_cache(maxsize=256)
def _verify_pdf(path, size, modified, expected_hash):
    with Path(path).open("rb") as source:
        actual = hashlib.file_digest(source, lambda: hashlib.md5(usedforsecurity=False)).hexdigest()
    if actual != expected_hash:
        raise ValueError("Source PDF no longer matches the registered PDF hash")


def verify_pdf(path, pdf_hash):
    stat = path.stat()
    _verify_pdf(str(path), stat.st_size, stat.st_mtime_ns, pdf_hash)


def render_page(path, pdf_hash, page_number, cache_dir, *, width=960, top=0, bottom=1):
    """Cache an unannotated full page or excerpt; highlights remain UI overlays."""
    if not re.fullmatch(r"[a-f0-9]{32}", pdf_hash):
        raise ValueError("Invalid PDF hash")
    if width not in (960, 1600) or page_number < 1 or not 0 <= top < bottom <= 1:
        raise ValueError("Invalid page image parameters")
    verify_pdf(path, pdf_hash)
    directory = Path(cache_dir) / pdf_hash
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{page_number}-{width}-{top:.4f}-{bottom:.4f}.png"
    if destination.is_file():
        return destination
    with pymupdf.open(path) as document:
        if page_number > len(document):
            raise ValueError("Source page is outside the PDF")
        page = document[page_number - 1]
        clip = pymupdf.Rect(0, page.rect.height * top, page.rect.width, page.rect.height * bottom)
        scale = min(width / page.rect.width, 2600 / page.rect.height)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".png", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            pixmap.save(temporary_path)
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
    return destination


def _source_rows(path, statement, pdf_hash, cache_dir):
    if statement.get("source_rows"):
        return statement["source_rows"]
    # Older extractions retain page numbers but not row boxes. Build their index once.
    directory = Path(cache_dir) / pdf_hash
    index = directory / "balance-sheet-rows.json"
    if index.is_file():
        return json.loads(index.read_text(encoding="utf-8"))
    pages = statement.get("pages") or []
    rows = []
    with pymupdf.open(path) as document:
        for number in pages:
            if not isinstance(number, int) or not 1 <= number <= len(document):
                continue
            page = document[number - 1]
            with redirect_stdout(sys.stderr):
                tables = page.find_tables(strategy="lines_strict")
            for table in tables.tables:
                for row, cells in zip(table.rows, table.extract()):
                    rows.append({"page_number": number, "bbox": normalized_bbox(page, row.bbox),
                                 "cells": [cell or "" for cell in cells]})
    directory.mkdir(parents=True, exist_ok=True)
    # Concurrent readers always see a complete JSON index.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as temporary:
        json.dump(rows, temporary, ensure_ascii=False)
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, index)
    finally:
        temporary_path.unlink(missing_ok=True)
    return rows


def locate_source(record, item_id, path, cache_dir):
    """Require a unique row matching both the chart item and stored amount."""
    verify_pdf(path, record["pdf_hash"])
    extraction = json.loads(record.get("extraction_report") or "{}")
    statement = extraction.get("balance_sheet") or {}
    units = set(re.findall(r"单位[：:（(]?(?:均为|为)?(?:人民币)?(百万元|亿元|万元|千元|元)",
                           clean_cell(record.get("balance_sheet") or "")))
    if len(units) != 1:
        raise ValueError("The source statement's currency unit cannot be verified")
    unit = next(iter(units))
    target = Decimal(str(record["amount"])) / UNITS[unit]
    chart = LineItemChart()
    matches = {}
    for row in _source_rows(path, statement, record["pdf_hash"], cache_dir):
        cells = row.get("cells") or []
        while cells and not clean_cell(cells[0]):
            cells = cells[1:]
        if not cells:
            continue
        label = clean_cell(re.sub(r"【[^】]*(?:】|$)", "", cells[0])).replace("(", "（").replace(")", "）")
        label = re.sub(r"^(?:[一二三四五六七八九十\d]+[、.．]|[（(][一二三四五六七八九十\d]+[）)])", "", label).rstrip("：:")
        item = chart.get(label)
        if item is None or item.item_id != item_id:
            continue
        for cell in cells[1:]:
            try:
                amount = parse_amount(cell)
            except ValueError:
                continue
            if amount == target:
                key = (row["page_number"], tuple(row["bbox"]))
                matches[key] = row
                break
    if len(matches) != 1:
        raise ValueError("Source row not uniquely located; open the original PDF to verify this amount")
    row = next(iter(matches.values()))
    bbox = row["bbox"]
    top, bottom = max(0, bbox[1] - 0.08), min(1, bbox[3] + 0.08)
    return {"pdf_hash": record["pdf_hash"], "page_number": row["page_number"], "bbox": bbox,
            "excerpt_top": top, "excerpt_bottom": bottom, "amount_unit": unit,
            "statement_amount": str(target), "report_creation_time": record["report_creation_time"]}


def warm_statement_previews(result):
    """Render only statement pages during extraction, never the whole report."""
    cache_dir = Path(__file__).resolve().parent / "reports" / ".source-cache"
    pages = {page for name in ("balance_sheet", "income_statement", "cash_flow_statement")
             if (statement := getattr(result, name)) is not None for page in statement.pages}
    for number in sorted(pages):
        try:
            render_page(Path(result.pdf_file), result.pdf_hash, number, cache_dir)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Unable to pre-render source page {number}: {error}", file=sys.stderr)
