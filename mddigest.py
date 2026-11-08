import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Callable

import pymupdf
import pymupdf4llm

from financial_statement_line_items import FinancialStatementLineItemDict


class TableParsingStage(StrEnum):
    BS_TITLE_DETECTION = "bs_header_detection"
    BS_HEADER_EXTRACTION = "bs_header_extraction"
    BS_REPORTING_DATE_DETECTION = "bs_date_detection"
    BS_REPORTING_ENTITY_DETECTION = "bs_reporting_entity_detection"
    BS_TABLE_CAPTION_EXTRACTION = "bs_table_caption_extraction"
    BS_TABLE_ITEMS_EXTRACTION = "bs_table_items_extraction"
    BS_TABLE_COMPLETED = "bs_table_completed"
    FAILED = "failed"

class TableParsingStateMachine:
    def __init__(self) -> None:
        self.stage = TableParsingStage.BS_TITLE_DETECTION
        print("Stage changed to BS_TITLE_DETECTION")
        self.reporting_date: str | None = None
        self.reporting_entity: str | None = None
        self.current_reporting_date: str | None = None
        self.previous_reporting_date: str | None = None
        self.has_notes = False
        self.table_caption_line: str | None = None
        self.line_item_dict = FinancialStatementLineItemDict("financial-statement-line-items.txt")

        self._handlers = {
            TableParsingStage.BS_TITLE_DETECTION: self._detect_title,
            TableParsingStage.BS_REPORTING_DATE_DETECTION: self._detect_reporting_date,
            TableParsingStage.BS_REPORTING_ENTITY_DETECTION: self._detect_reporting_entity,
            TableParsingStage.BS_TABLE_CAPTION_EXTRACTION: self._extract_table_caption,
            TableParsingStage.BS_TABLE_ITEMS_EXTRACTION: self._extract_table_item,
        }


    def process_md_line(self, md_line: str) -> bool:
        if not md_line or len(md_line) == 0:
            return True

        handler = self._handlers.get(self.stage)
        if handler is None:
            return True
        return handler(md_line)

    def _detect_title(self, md_line: str) -> bool:
        if "合并资产负债表" == md_line.strip():
            print(
                f"begin balance sheet line {md_line}"
            )
            self.stage = TableParsingStage.BS_REPORTING_DATE_DETECTION
            print("Stage changed to BS_REPORTING_DATE_DETECTION")
        return True

    @staticmethod
    def match_dates(line: str) -> list[str]:
        pattern = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
        return [
            f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            for year, month, day in re.findall(pattern, line)
        ]
    
    def _detect_reporting_date(self, md_line: str) -> bool:
        dates = self.match_dates(md_line)
        if not dates or dates.count(dates[0]) > 1:
            print(
                f"Warning: Found multiple or no dates in line "
                f"{md_line.line_no}: {md_line.plain_text}"
            )
            self.stage = TableParsingStage.FAILED
            print("Stage changed to FAILED")
            return False

        self.reporting_date = dates[0]
        print(f"reporting_date = {self.reporting_date}")
        self.stage = TableParsingStage.BS_REPORTING_ENTITY_DETECTION
        print("Stage changed to BS_REPORTING_ENTITY_DETECTION")
        return True

    def _detect_reporting_entity(self, md_line: str) -> bool:
        pattern = r"编制单位[：:]\s*(.+?公司)"
        match = re.search(pattern, md_line)
        if match:
            self.reporting_entity = match.group(1).strip()
            print(f"reporting_entity = {self.reporting_entity}")
            self.stage = TableParsingStage.BS_TABLE_CAPTION_EXTRACTION
            print("Stage changed to BS_TABLE_CAPTION_EXTRACTION")
        return True

    def _extract_table_caption(self, page_line: str) -> bool:
        print(
            f"Extracting BS table caption in line {page_line}"
        )
        if len(page_line) < 10 or page_line[0] != "|":
            return True

        fields = page_line.strip("|").split("|")

        if (len(fields) == 3 or len(fields) == 4) and fields[0] == "项目":
            self.table_caption_line = page_line
            self.has_notes = len(fields) == 4
            self.current_reporting_date = fields[-2]
            self.previous_reporting_date = fields[-1]
            print(
                "curr/prev dates = "
                f"{self.has_notes} {self.current_reporting_date} "
                f"{self.previous_reporting_date}"
            )

            self.stage = TableParsingStage.BS_TABLE_ITEMS_EXTRACTION
            print("Stage changed to BS_TABLE_ITEMS_EXTRACTION")
        return True

    @staticmethod
    def is_number(s):
        try:
            float(s.replace(",", ""))   # 去掉千分位逗号
            return True
        except (ValueError, AttributeError):
            return False
        
    def _extract_table_item(self, page_line: str) -> bool:
        if len(page_line) < 10 or page_line[0] != "|":
            return True

        fields = page_line.replace("<br>", "").strip("|").split("|")
        if len(fields) < 2:
            return True

        caption_fields = self.table_caption_line.strip("|").split("|")
        if len(fields) != len(caption_fields):
            return True

        if page_line.find("归属于母公司所有者权益") >= 0:
             print(f"===== found 归属于母公司所有者权益")
        result = ["", "", "", ""]
        if len(fields) == 3:
            result = [fields[0], "---", fields[1], fields[2]]
        elif len(fields) == 4:
            result = [fields[0], fields[1], fields[2], fields[3]]

        if self.is_number(result[2].strip("*")) or self.is_number(result[3].strip("*")):
            item = self.line_item_dict.get(fields[0])
            if item:
                print(f"Extracting BS item: {item.item_id} {item.name} {item.english_name} {result}")
            else:
                print(f"ERROR: unknown item name: {fields[0]}")

        else:
            print(f"Skip BS item: {page_line}")

        if re.search(r"负债.*权益.*", result[0]):
            self.stage = TableParsingStage.BS_TABLE_COMPLETED
            print("Stage changed to BS_TABLE_COMPLETED")
        return True


class PDFDigest:
    def __init__(self, pdf_path: str | Path) -> None:
        self.pdf_path = Path(pdf_path)
        self.state_machine = TableParsingStateMachine()

    def process_document(self, pages: list = None) -> None:
        total_pages = 0
        if not pages:
            with pymupdf.open(self.pdf_path) as document:
                total_pages = document.page_count
                pages = list(range(total_pages))

        for page_number in pages:
            print(
                f"\rConverting page {page_number + 1}/{total_pages}...",
                end="",
                flush=True,
            )
            # print(f"Digesting page {page_number + 1}/{total_pages} ...")
            md_text = pymupdf4llm.to_markdown(self.pdf_path, pages = [page_number])
            for md_line in md_text.splitlines():
                self.state_machine.process_md_line(md_line)

        print(" done.")

def main() -> None:

    # digest = PDFDigest("reports/test.pdf")

    digest = PDFDigest("reports/dongpeng-2026q3.pdf")
    # digest.process_document()
    digest.process_document([7,8,9])

    # digest = PDFDigest("reports/dongpeng-2025.pdf")
    # digest.process_document([110, 111, 112])
    # digest.process_document()


    # digest = PDFDigest("reports/maotai-2025.pdf")
    # digest.process_document([110, 111, 112])
    # digest.process_document()

if __name__ == "__main__":
    main()
