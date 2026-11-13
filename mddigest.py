import re
from enum import StrEnum
from pathlib import Path

import argparse
from line_item_chart import LineItemChart
from line_item_storage import LineItemStorage


class TableParsingStage(StrEnum):
    BS_TITLE_DETECTION = "bs_header_detection"
    BS_HEADER_EXTRACTION = "bs_header_extraction"
    BS_REPORTING_METADATA_DETECTION = "bs_reporting_metadata_detection"
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
        self.report_creation_time:str = ""
        self.stock_code:str | None = None
        self.amount_unit: int | None = None
        self.has_notes:bool = False
        self.table_caption_line: str | None = None
        self.line_item_dict = LineItemChart("line-item-chart.txt")
        self.storage = LineItemStorage()

        self._handlers = {
            TableParsingStage.BS_TITLE_DETECTION: self._detect_title,
            TableParsingStage.BS_REPORTING_METADATA_DETECTION: self._detect_reporting_metadata,
            TableParsingStage.BS_TABLE_CAPTION_EXTRACTION: self._extract_table_caption,
            TableParsingStage.BS_TABLE_ITEMS_EXTRACTION: self._extract_table_item,
        }

    def process_as_metadata(self, md_line: str) -> bool:
        """Process document metadata, page markers, stock code, etc."""
        if not self.report_creation_time:
            creation_date_match = re.match(
                r"^<!--\s*ORIGINAL_PDF_FILE_CREATION_DATE:\s*(.*?)\s*-->$",
                md_line.strip(),
            )
            if creation_date_match:
                self.report_creation_time = creation_date_match.group(1)
                return True

        if not self.stock_code:
            stock_code_match = re.search(
                r"(?:证券|公司|股票)代码[：:|]\s*(\d{6})", md_line
            )
            if stock_code_match:
                self.stock_code = stock_code_match.group(1)
                return True

        page_match = re.match(
            r"^<!--\s*PDF_PAGE:\s*(\d+)\s*/\s*(\d+)\s*-->$",
            md_line.strip(),
        )
        if page_match:
            current_page = int(page_match.group(1))
            total_pages = int(page_match.group(2))
            print(
                f"Digesting page {current_page}/{total_pages}...",
                flush=True,
            )
            return True
        return False

    def process_md_line(self, md_line: str) -> bool:
        if not md_line or len(md_line) == 0:
            return True
        if self.process_as_metadata(md_line):
            return True

        handler = self._handlers.get(self.stage)
        if handler is None:
            return True
        return handler(md_line)

    def _store_extracted_item(
        self,
        item_id: int,
        amount: str | float,
        item_english_name: str,
    ) -> None:
        """Pass an extracted item and its report metadata to storage."""
        self.storage.store_item(
            self.stock_code,
            self.current_reporting_date,
            item_id,
            self.report_creation_time,
            amount,
            item_english_name,
        )

    def close(self) -> None:
        self.storage.close()

    def _detect_title(self, md_line: str) -> bool:
        result = re.sub(r"[ \*0-9,、]", "", md_line)
        if "合并资产负债表" in result and len(result) < len("合并资产负债表") + 3:
            print(
                f"begin balance sheet line {md_line} {len(result)}"
            )
            self.stage = TableParsingStage.BS_REPORTING_METADATA_DETECTION
            print("Stage changed to BS_REPORTING_METADATA_DETECTION")
        return True

    @staticmethod
    def match_dates(line: str) -> list[str]:
        pattern = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
        return [
            f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            for year, month, day in re.findall(pattern, line)
        ]
    
    @staticmethod
    def get_amout_unit(unit_name) -> int:
        UNIT_MAP = {"亿元": 100_000_000, "万元": 10_000, "千元": 1_000, "元": 1}
        return UNIT_MAP.get(unit_name, 1)

    def _detect_reporting_metadata(self, md_line: str) -> bool:
        dates = self.match_dates(md_line)
        if dates and dates.count(dates[0]) == 1 and self.reporting_date is None:
            self.reporting_date = dates[0]
            print(f"reporting_date = {self.reporting_date}")

        pattern = r"编制单位[：:]\s*(.+?公司)"
        match = re.search(pattern, md_line)
        if match and self.reporting_entity is None:
            self.reporting_entity = match.group(1).strip()
            print(f"reporting_entity = {self.reporting_entity}")
        if "单位" in md_line:
            print("======");
        pattern = r"单位\s*[：:]\s*(亿元|万元|千元|元)"
        match = re.search(pattern, md_line)
        if match and self.amount_unit is None:
            self.amount_unit = self.get_amout_unit(match.group(1).strip())
            print(f"amount_unit = {self.amount_unit}")

        if self.reporting_date is not None and self.reporting_entity is not None and self.amount_unit is not None :
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

            date_pattern = r"\*{0,2}(\d{4})\*{0,2}\s*年\s*\*{0,2}(\d{1,2})\*{0,2}\s*月\s*\*{0,2}(\d{1,2})\*{0,2}\s*日"
            matches = re.findall(date_pattern, page_line)
            reporting_dates = [f"{y}-{m.zfill(2)}-{d.zfill(2)}" for y, m, d in matches]
            if len(reporting_dates) == 2:
                assert self.reporting_date == reporting_dates[0], "date mismatch"
                self.current_reporting_date = reporting_dates[0]
                self.previous_reporting_date = reporting_dates[1]
                print(
                    "curr/prev dates = "
                    f"{self.has_notes} {self.current_reporting_date} "
                    f"{self.previous_reporting_date}"
                )
                assert self.current_reporting_date == self.reporting_date, "reporting date mismatch"
            else:
                self.current_reporting_date = self.reporting_date
                assert fields[2] in ("期末余额", "期初余额"), "unexpected line, please check"

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
        if re.search(r"负债和.*权益.*总计", page_line):
            self.stage = TableParsingStage.BS_TABLE_COMPLETED
            print("Stage changed to BS_TABLE_COMPLETED")
            return True

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
            result = [fields[0], "---", fields[1].strip("*"), fields[2].strip("*")]
        elif len(fields) == 4:
            result = [fields[0], fields[1], fields[2].strip("*"), fields[3].strip("*")]

        if self.is_number(result[2]):
            amount = float(result[2].replace(",", ""))
            item = self.line_item_dict.get(fields[0])
            if item:
                print(
                    f"Extracted BS item: {self.stock_code} {self.current_reporting_date} "
                    f"{item.item_id} {self.report_creation_time} "
                    f"{result[2]} {item.english_name}"
                )
                self._store_extracted_item(
                    item.item_id,
                    amount * (self.amount_unit or 1),
                    item.english_name,
                )
            else:
                print(f"ERROR: unknown item name: {fields[0]}")

        else:
            print(f"Skip BS item: {page_line}")

        return True


class MDDigest:
    def __init__(self, md_path: str | Path) -> None:
        self.md_path = Path(md_path)
        self.state_machine = TableParsingStateMachine()

    def process_document(self) -> None:
        if not self.md_path.is_file():
            raise FileNotFoundError(f"Markdown file not found: {self.md_path}")

        md_lines = self.md_path.read_text(encoding="utf-8").splitlines()
        try:
            for line in md_lines:
                self.state_machine.process_md_line(line)
        finally:
            self.state_machine.close()

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown with searchable page markers."
    )
    parser.add_argument(
        "md_path",
        type=Path,
        nargs="?",
        default=Path("reports/dongpeng-2026q3.md"),
        help="Path to the input MD file",
    )
    args = parser.parse_args()

    digest = MDDigest(args.md_path)
    digest.process_document()

if __name__ == "__main__":
    main()
