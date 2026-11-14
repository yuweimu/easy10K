"""Add index and section anchors and contents jump links to report copies.

Run from any directory:
    python mdpreprocess.py
    python mdpreprocess.py --input-dir markdown --preprocessed-dir preprocessed

Source reports are never modified. Full report copies are saved in preprocessed
with an invisible __reportanchor-index anchor before the table of contents, if found.
Linked contents entries are also copied into a hidden REPORT_NAVIGATION comment
immediately after the original PDF creation-date metadata, when present.
"""

import argparse
import re
from dataclasses import dataclass
from html import escape, unescape
from pathlib import Path

from line_item_chart import LineItemChart

PROJECT_DIR = Path(__file__).resolve().parent
SECTION_ANCHORS = {
    "董事长致辞": "__reportanchor-chairman-statement",
    "致股东的信": "__reportanchor-shareholder-letter",
    "释义": "__reportanchor-definitions",
    "重要提示、目录和释义": "__reportanchor-important-notices",
    "公司简介和主要财务指标": "__reportanchor-company-profile",
    "公司简介及主要财务指标": "__reportanchor-company-profile",
    "董事局报告": "__reportanchor-board-report",
    "董事会报告": "__reportanchor-board-report",
    "管理层讨论与分析": "__reportanchor-management-discussion",
    "公司治理": "__reportanchor-corporate-governance",
    "公司治理及企业管治报告": "__reportanchor-corporate-governance",
    "公司治理、环境和社会": "__reportanchor-governance-environment-social",
    "环境与社会责任": "__reportanchor-environment-social",
    "环境和社会责任": "__reportanchor-environment-social",
    "重要事项": "__reportanchor-significant-events",
    "股份变动及股东情况": "__reportanchor-shares-shareholders",
    "优先股相关情况": "__reportanchor-preference-shares",
    "债券相关情况": "__reportanchor-bonds",
    "财务报告": "__reportanchor-financial-statements",
    "五年业绩摘要": "__reportanchor-five-year-summary",
}


def normalize_section_title(title: str) -> str:
    title = re.sub(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*[节章]\s*", "", title)
    return re.sub(r"[\s*`_#、，,:：。．.（）()]", "", title).casefold()

def indent_balance_sheet(text: str, chart: LineItemChart | None = None) -> str:
    """Format balance-sheet names by hierarchy and right-align monospaced numbers."""
    chart = chart if chart is not None else LineItemChart()

    def normalize_name(value: str) -> str:
        value = re.sub(r"<[^>]*>", "", unescape(value))
        return re.sub(r"[\s*`_]", "", value).replace("：", ":").rstrip(":")

    def format_number(cell: str) -> str:
        if 'class="balance-sheet-number"' in cell:
            return cell
        value = cell.strip()
        plain = re.sub(r"[*_]", "", value)
        number = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?"
        if not re.fullmatch(rf"(?:{number}|\({number}\))", plain):
            return cell
        content = escape(plain)
        if (value.startswith("**") and value.endswith("**")) or (value.startswith("__") and value.endswith("__")):
            content = f"<strong>{content}</strong>"
        leading = cell[:len(cell) - len(cell.lstrip())]
        trailing = cell[len(cell.rstrip()):]
        return (
            f'{leading}<span class="balance-sheet-number" '
            'style="display:block;text-align:right;font-family:ui-monospace,monospace;'
            f'font-variant-numeric:tabular-nums">{content}</span>{trailing}'
        )

    item_ids = {normalize_name(name): item.item_id for name, item in chart.items.items()}
    spacer = re.compile(r'<span class="balance-sheet-indent"[^>]*></span>')
    result: list[str] = []
    in_balance_sheet = False
    in_comment = False
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            result.append(line)
            continue
        if fence is not None:
            result.append(line)
            continue
        if "<!--" in line:
            in_comment = True
        if in_comment:
            if "-->" in line:
                in_comment = False
            result.append(line)
            continue

        heading = re.sub(r"^#{1,6}\s*", "", normalize_name(line))
        heading = re.sub(r"^[（(]?[一二三四五六七八九十百零〇\d]+[）)、.．]*", "", heading)
        if re.fullmatch(r"(?:合并|母公司|公司)?资产负债表(?:[（(]续(?:表)?[）)])?", heading):
            in_balance_sheet = True
        elif re.fullmatch(r"(?:合并|母公司|公司)?(?:利润表|现金流量表|所有者权益变动表|股东权益变动表|财务报表附注)", heading):
            in_balance_sheet = False

        if in_balance_sheet and line.lstrip().startswith("|"):
            # Markdown alignment applies to both captions and the body cells.
            if re.fullmatch(r"\s*\|(?:\s*:?-+:?\s*\|)+\s*", line):
                fields = line.split("|")
                fields[1] = ":---"
                for column in range(2, len(fields) - 1):
                    fields[column] = "---:"
                result.append("|".join(fields))
                continue
            cell_start = line.index("|") + 1
            cell_end = line.find("|", cell_start)
            if cell_end != -1:
                label = spacer.sub("", line[cell_start:cell_end])
                name = normalize_name(label)
                item_id = item_ids.get(name)
                if item_id is None:
                    item_id = item_ids.get(re.sub(r"^其中:?", "", name))
                if item_id is not None and item_id > 0:
                    depth = max(0, len(str(item_id)) // 2 - 1)
                    indent = (
                        f'<span class="balance-sheet-indent" data-item-id="{item_id}" '
                        f'data-item-depth="{depth}" aria-hidden="true" '
                        f'style="display:inline-block;width:{depth * 1.5:g}em"></span>'
                    )
                    line = line[:cell_start] + indent + label + line[cell_end:]
                if re.fullmatch(r"负债[和及与].*权益(?:总计|合计)", name):
                    in_balance_sheet = False
                fields = line.split("|")
                for column in range(2, len(fields)):
                    fields[column] = format_number(fields[column])
                line = "|".join(fields)
        result.append(line)
    return "".join(result)

def add_report_navigation(text: str, entries: list[str]) -> str:
    """Copy only section link elements into hidden navigation metadata."""
    if not entries:
        return text
    metadata_pattern = r"(?m)^[ \t]*<!--\s*ORIGINAL_PDF_FILE_CREATION_DATE:[^\r\n]*-->[ \t]*(?:\r?\n|$)"
    if not re.search(metadata_pattern, text):
        return text
    text = re.sub(
        r"(?m)^[ \t]*<!--\s*REPORT_NAVIGATION:[\s\S]*?-->[ \t]*(?:\r?\n|$)",
        "", text,
    )
    links = [
        link
        for entry in entries
        for link in re.findall(r'<a href="#__reportanchor-[^"\r\n]+">.*?</a>', entry)
    ]
    if not links:
        return text
    metadata = re.search(metadata_pattern, text)
    newline = "\r\n" if metadata.group().endswith("\r\n") else "\n"
    block = "<!-- REPORT_NAVIGATION:" + newline
    block += newline.join(links)
    block += newline + "-->" + newline
    prefix = "" if metadata.group().endswith("\n") else newline
    return text[:metadata.end()] + prefix + block + text[metadata.end():]


def add_index_section_anchors(text: str) -> str:
    """Anchor sections and link the full text of their corresponding contents entries."""
    def contents_text(line: str) -> str:
        # Unwrap both legacy icon-only links and full-entry links on reruns.
        return re.sub(
            r'<a href="#__reportanchor-[^"\n]+">(.*?)</a>',
            lambda match: unescape(match.group(1).removesuffix("↗").rstrip()),
            line,
        )

    def link_entry(entry: str, anchor: str) -> str:
        leading = entry[:len(entry) - len(entry.lstrip())]
        trailing = entry[len(entry.rstrip()):]
        return f'{leading}<a href="#{escape(anchor, quote=True)}">{escape(entry.strip())} ↗</a>{trailing}'

    lines = text.splitlines(keepends=True)
    visible: list[tuple[int, str]] = []
    fence: str | None = None
    in_comment = False
    for index, line in enumerate(lines):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        if "<!--" in line:
            in_comment = True
        if not in_comment:
            visible.append((index, line.strip()))
        if "-->" in line:
            in_comment = False

    contents_index = next((index for index, line in visible
                           if normalize_section_title(line) in {"目录", "contents", "tableofcontents", "index"}), None)
    if contents_index is None:
        return text

    entries: set[str] = set()
    contents_lines: dict[int, str] = {}
    for index, line in visible:
        if index <= contents_index:
            continue
        clean = contents_text(line)
        clean = re.sub(r"[*_`]", "", clean).strip()
        # Actual headings (or the supporting-documents section) end the index.
        if clean.startswith("#") and not re.search(r"[.．…·]{2,}", clean):
            break
        if clean.startswith("|"):
            clean = clean.strip("|").split("|")[0].strip()
        title = re.split(r"[.．…·]{2,}", clean, maxsplit=1)[0]
        title = re.sub(r"\s+\d+\s*$", "", title).strip()
        if title == clean and not line.startswith("|"):
            # Bare section headings belong to the body, not the contents.
            if re.match(r"^第.+[节章]", clean):
                break
            continue
        normalized = normalize_section_title(title)
        if normalized:
            entries.add(normalized)
            contents_lines[index] = normalized

    aliases = {normalize_section_title(title): anchor for title, anchor in SECTION_ANCHORS.items()}
    existing = set(re.findall(r'''\b(?:id|name)\s*=\s*["']([^"']+)["']''', text))
    insertions: dict[int, list[str]] = {}
    for index, line in visible:
        if index == contents_index or index in contents_lines or not line or line.startswith("|"):
            continue
        clean = re.sub(r"[*_`]", "", line)
        if re.search(r"[.．…·]{2,}", clean):
            continue
        is_markdown_heading = bool(re.match(r"^#{1,6}\s+", clean))
        clean = re.sub(r"^#{1,6}\s+", "", clean).strip()
        if not is_markdown_heading and not re.match(r"^第.+[节章]", clean):
            continue
        # Some PDF exports combine two consecutive headings on a single line.
        titles = re.split(r"(?=第[一二三四五六七八九十百零〇\d]+[节章])", clean)
        for title in titles:
            normalized = normalize_section_title(title)
            if normalized not in entries:
                continue
            anchor = aliases.get(normalized, f"__reportanchor-section-{normalized}")
            if anchor not in existing:
                insertions.setdefault(index, []).append(anchor)
                existing.add(anchor)

    result = []
    navigation_lines: list[str] = []
    for index, line in enumerate(lines):
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        for anchor in insertions.get(index, []):
            result.append(f'<a id="{anchor}"></a>{newline}{newline}')
        if index in contents_lines:
            title = contents_lines[index]
            anchor = aliases.get(title, f"__reportanchor-section-{title}")
            # Never create a dead link for a section absent from the report.
            if anchor in existing:
                line = contents_text(line)
                if line.lstrip().startswith("|"):
                    # Link the title cell without changing the table's columns.
                    cell_start = line.index("|") + 1
                    cell_end = line.find("|", cell_start)
                    if cell_end != -1:
                        line = line[:cell_start] + link_entry(line[cell_start:cell_end], anchor) + line[cell_end:]
                else:
                    content = line.rstrip("\r\n")
                    line = link_entry(content, anchor) + line[len(content):]
        if index in contents_lines:
            navigation_lines.append(line)
        result.append(line)
    return add_report_navigation("".join(result), navigation_lines)

@dataclass
class CompanyProfile:
    stock_code: str
    reporting_date: str
    stock_name: str
    major_accounting_data: str
    company_name: str = ""


def add_report_index_anchor(text: str) -> str:
    """Anchor the first contents heading; leave other report content unchanged."""
    if re.search(r'''\b(?:id|name)\s*=\s*["']__reportanchor-index["']''', text, re.IGNORECASE):
        return text
    offset = 0
    fence: str | None = None
    in_comment = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
        if "<!--" in stripped:
            in_comment = True
        if fence is None and not marker and not in_comment:
            heading = re.sub(r"^#{1,6}\s*", "", stripped)
            heading = re.sub(r"[\s*`_#]", "", heading).casefold()
            if heading in {"目录", "contents", "tableofcontents", "index"}:
                newline = "\r\n" if line.endswith("\r\n") else "\n"
                anchor = f'<a id="__reportanchor-index"></a>{newline}{newline}'
                return text[:offset] + anchor + text[offset:]
        if "-->" in stripped:
            in_comment = False
        offset += len(line)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=PROJECT_DIR / "markdown")
    parser.add_argument("--preprocessed-dir", type=Path, default=PROJECT_DIR / "preprocessed",
                        help="Destination for full reports with index anchors")
    args = parser.parse_args()
    input_dir = args.input_dir.resolve()
    preprocessed_dir = args.preprocessed_dir.resolve()
    if not input_dir.is_dir():
        parser.error(f"Input directory not found: {input_dir}")
    if preprocessed_dir == input_dir or preprocessed_dir.is_relative_to(input_dir):
        parser.error("Preprocessed directory must be outside the source Markdown directory")

    paths = sorted(input_dir.glob("*/*.md"))
    chart = LineItemChart()
    index_files = 0
    for source in paths:
        text = source.read_text(encoding="utf-8")
        processed = indent_balance_sheet(add_index_section_anchors(add_report_index_anchor(text)), chart)
        relative_path = source.relative_to(input_dir)
        report_destination = preprocessed_dir / relative_path
        report_destination.parent.mkdir(parents=True, exist_ok=True)
        report_destination.write_text(processed, encoding="utf-8")
        has_index = bool(re.search(r'''\b(?:id|name)\s*=\s*["']__reportanchor-index["']''', processed))
        index_files += int(has_index)
        print(f"Preprocessed ({'index anchored' if has_index else 'no index found'}): {report_destination}")
    print(f"Saved {len(paths)} report copies; {index_files} have __reportanchor-index anchors.")


if __name__ == "__main__":
    main()
