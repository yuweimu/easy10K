"""Shared Markdown table preparation for statement digesters."""

import re


def split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def table_width_issues(rows: list[list[str]]) -> list[tuple[int, int, int]]:
    """Return rows whose width differs from their table segment's width.

    Extraction keeps the raw rows from each detected table.  A statement may
    contain multiple tables with different note-column layouts, so the width
    is reset whenever another ``项目``/``科目`` header appears.  If a table
    has no recognizable header, its first row establishes the expected width.
    The returned row numbers are one-based.
    """
    expected_width = None
    issues = []
    for row_number, row in enumerate(rows, 1):
        label = row[0].strip() if row else ""
        if label in ("项目", "科目"):
            expected_width = len(row)
            continue
        if expected_width is None:
            expected_width = len(row)
            continue
        if len(row) != expected_width:
            issues.append((row_number, expected_width, len(row)))
    return issues
