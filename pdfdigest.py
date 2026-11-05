import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

import pymupdf


NUMBER_PATTERN = re.compile(
    r"""
    (?P<number>
        \(?-?
        \d[\d,]*
        (?:\.\d+)?
        \)?
    )
    \s*$
    """,
    re.VERBOSE,
)


class TableParsingStage(StrEnum):
    HEADER_DETECTION = "header_detection"
    HEADER_EXTRACTION = "header_extraction"
    TABLE_DETECTION = "table_detection"
    PERIOD_EXTRACTION = "period_extraction"
    ROW_EXTRACTION = "row_extraction"
    COMPLETED = "completed"
    FAILED = "failed"


class PDFDigest:
    def __init__(self, pdf_path: str | Path) -> None:
        self.pdf_path = Path(pdf_path)
        self.stage = TableParsingStage.HEADER_DETECTION

    @staticmethod
    def parse_number(raw: str) -> Decimal | None:
        value = raw.strip().replace(",", "")
        negative = value.startswith("(") and value.endswith(")")
        value = value.strip("()")

        try:
            number = Decimal(value)
            return -number if negative else number
        except InvalidOperation:
            return None

    @staticmethod
    def match_dates(line: str) -> list[str]:
        pattern = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
        return [
            f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            for year, month, day in re.findall(pattern, line)
        ]

    @classmethod
    def parse_financial_lines(cls, page_text: str) -> list[dict]:
        facts = []

        for line in page_text.splitlines():
            line = " ".join(line.split())
            if not line:
                continue

            match = NUMBER_PATTERN.search(line)
            if not match:
                continue

            raw_number = match.group("number")
            label = line[:match.start()].strip(" .:-")
            if len(label) < 2:
                continue

            number = cls.parse_number(raw_number)
            if number is None:
                continue

            facts.append(
                {
                    "source_label": label,
                    "source_value": raw_number,
                    "numeric_value": number,
                }
            )

        return facts

    def process_line(self, line_no: int, line: str) -> bool:
        if line_no < 0 or not line.strip():
            return True

        if self.stage == TableParsingStage.HEADER_DETECTION:
            if "合并资产负债表" in line and "编制单位" in line:
                print(f"Found header in line {line_no}: {line}")
                dates = self.match_dates(line)
                if not dates or dates.count(dates[0]) > 1:
                    print(
                        f"Warning: Found multiple or no dates in line "
                        f"{line_no}: {line}"
                    )
                    self.stage = TableParsingStage.FAILED
                    return False

                print(dates)
                self.stage = TableParsingStage.TABLE_DETECTION
                return True

        print(f"Processing line {line_no}: {line}")
        date_pattern = r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
        matches = re.findall(date_pattern, line)

        if matches:
            print(f"Found date(s) in line {line_no}: {matches}")
            for match in matches:
                print(f"Extracted date: {match}")
                for year, month, day in re.findall(
                    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
                    match,
                ):
                    print(f"Formatted date: {year}-{month.zfill(2)}-{day.zfill(2)}")

        return True

    def process_page(self, page: pymupdf.Page) -> None:
        current_block_no = -1
        current_line = ""

        for word in page.get_text("words", sort=True):
            block_no = word[5]
            if block_no != current_block_no:
                self.process_line(current_block_no, current_line)
                current_block_no = block_no
                current_line = word[4]
            else:
                current_line += " " + word[4]

            if current_block_no > 5:
                break

    def process_document(self) -> None:
        with pymupdf.open(self.pdf_path) as document:
            print(f"Total pages: {document.page_count}")
            for page in document:
                self.process_page(page)


def main() -> None:
    digest = PDFDigest("reports/test.pdf")
    digest.process_document()


if __name__ == "__main__":
    main()
