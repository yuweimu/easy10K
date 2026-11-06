import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Callable

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
    BS_TITLE_DETECTION = "bs_header_detection"
    BS_HEADER_EXTRACTION = "bs_header_extraction"
    BS_REPORTING_DATE_DETECTION = "bs_date_detection"
    BS_REPORTING_ENTITY_DETECTION = "bs_reporting_entity_detection"
    BS_TABLE_CAPTION_EXTRACTION = "bs_table_caption_extraction"
    BS_TABLE_ITEMS_EXTRACTION = "bs_table_items_extraction"
    BS_TABLE_COMPLETED = "bs_table_completed"
    TABLE_DETECTION = "table_detection"
    PERIOD_EXTRACTION = "period_extraction"
    ROW_EXTRACTION = "row_extraction"
    COMPLETED = "completed"
    FAILED = "failed"

class PageLine:
    def __init__(self, line_no: int, text: str, words: list[tuple]) -> None:
        self.line_no = line_no
        self.plain_text = text
        self.words = words

class TableParsingStateMachine:
    def __init__(self) -> None:
        self.stage = TableParsingStage.BS_TITLE_DETECTION
        print("Stage changed to BS_TITLE_DETECTION")
        self.reporting_date: str | None = None
        self.reporting_entity: str | None = None
        self.current_reporting_date: str | None = None
        self.previous_reporting_date: str | None = None
        self.has_notes = False
        self.table_caption_line: PageLine | None = None

        self._handlers = {
            TableParsingStage.BS_TITLE_DETECTION: self._detect_title,
            TableParsingStage.BS_REPORTING_DATE_DETECTION: self._detect_reporting_date,
            TableParsingStage.BS_REPORTING_ENTITY_DETECTION: self._detect_reporting_entity,
            TableParsingStage.BS_TABLE_CAPTION_EXTRACTION: self._extract_table_caption,
            TableParsingStage.BS_TABLE_ITEMS_EXTRACTION: self._extract_table_item,
        }

    @staticmethod
    def match_dates(line: str) -> list[str]:
        pattern = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
        return [
            f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            for year, month, day in re.findall(pattern, line)
        ]

    def process_word_line(self, page_line: PageLine) -> bool:
        if not page_line.words:
            return True

        handler = self._handlers.get(self.stage)
        if handler is None:
            return True
        return handler(page_line)

    def _detect_title(self, page_line: PageLine) -> bool:
        if "合并资产负债表" == page_line.plain_text.strip():
            print(
                f"begin balance sheet in line {page_line.line_no}: "
                f"{page_line.plain_text}"
            )
            self.stage = TableParsingStage.BS_REPORTING_DATE_DETECTION
            print("Stage changed to BS_REPORTING_DATE_DETECTION")
        return True

    def _detect_reporting_date(self, page_line: PageLine) -> bool:
        dates = self.match_dates(page_line.plain_text)
        if not dates or dates.count(dates[0]) > 1:
            print(
                f"Warning: Found multiple or no dates in line "
                f"{page_line.line_no}: {page_line.plain_text}"
            )
            self.stage = TableParsingStage.FAILED
            print("Stage changed to FAILED")
            return False

        self.reporting_date = dates[0]
        print(f"reporting_date = {self.reporting_date}")
        self.stage = TableParsingStage.BS_REPORTING_ENTITY_DETECTION
        print("Stage changed to BS_REPORTING_ENTITY_DETECTION")
        return True

    def _detect_reporting_entity(self, page_line: PageLine) -> bool:
        pattern = r"编制单位[：:]\s*(.+?公司)"
        match = re.search(pattern, page_line.plain_text)
        if match:
            self.reporting_entity = match.group(1).strip()
            print(f"reporting_entity = {self.reporting_entity}")
            self.stage = TableParsingStage.BS_TABLE_CAPTION_EXTRACTION
            print("Stage changed to BS_TABLE_CAPTION_EXTRACTION")
        return True

    def _extract_table_caption(self, page_line: PageLine) -> bool:
        print(
            f"Extracting BS table caption in line {page_line.line_no}: "
            f"{page_line.plain_text}"
        )
        fields = page_line.plain_text.split()

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

    def mid(self, r):
        return (r[0] + r[1]) / 2

    def mid_distance(self, r1, r2):
        return abs(self.mid(r1) - self.mid(r2))

    def _align_table_item(self, word: tuple) -> str:
        if self.table_caption_line is None:
            return "item_field_error"

        caption_words = self.table_caption_line.words
        if len(caption_words) == 3:
            assert self.has_notes == False, "Notes column unexpected but found"
        elif len(caption_words) == 4:
            assert self.has_notes == True, "Notes column expected but not found"
        else:
            return "item_field_error"

        if word[2] < caption_words[1][0]:
            return "item_name"
        if word[2] < caption_words[2][0]:
            return "notes" if self.has_notes else "current_balance"
        if word[2] < caption_words[3][0]:
            return "current_balance" if self.has_notes else "previous_balance"
        return "previous_balance"

    def _extract_table_item(self, page_line: PageLine) -> bool:
        fields = page_line.plain_text.split()
        if len(fields) < 2:
            return True

        if page_line.plain_text.find("应付票据") >= 0:
            print(f"===== found 应付票据")
        item_name        = ""
        notes            = ""
        current_balance  = ""
        previous_balance = ""

        for word in page_line.words:
            aligned_item = self._align_table_item(word)
            if aligned_item == "item_name":
                item_name = word[4]
                assert item_name == fields[0], f"Item name mismatch: {item_name} vs {fields[0]}"
            elif aligned_item == "notes":
                notes = word[4]
                assert self.has_notes and notes == fields[1], f"Notes mismatch: {notes} vs {fields[1]}"
            elif aligned_item == "current_balance":
                current_balance = word[4]
                assert current_balance == fields[-2], f"Current balance mismatch: {current_balance} vs {fields[-2]}"
            elif aligned_item == "previous_balance":
                previous_balance = word[4]
                assert previous_balance == fields[-1], f"Previous balance mismatch: {previous_balance} vs {fields[-1]}"
            else:
                print(
                    f"Item name '{word[4]}' "
                    f"'{aligned_item}' in line {page_line.line_no}: "
                    f"{page_line.plain_text}"
                )

        print(
            f"Extracting BS item: {[item_name,notes,current_balance,previous_balance]}"
        )

        if re.search(r"负债.*权益.*", item_name):
            self.stage = TableParsingStage.BS_TABLE_COMPLETED
            print("Stage changed to BS_TABLE_COMPLETED")
        return True


class ResultPage:
    def __init__(self, page: pymupdf.Page, page_number: int, page_text: str) -> None:
        self.page = page
        self.page_number = page_number
        self.page_lines: list[PageLine] = []
        self.current_line = ""
        self.current_word_line: list[tuple] = []
        self.current_x = 0.0
        self.current_y = 0.0
        self.current_block_no = -1

    def append_word(self, current_x: float, word: tuple) -> None:
        space_width = 11.0  # Estimated space width, to be refined
        x_diff = word[0] - current_x
        if x_diff > space_width * 0.7:
            self.current_line += "  " * int(x_diff / space_width)
        self.current_line += word[4]

        if word[0] - self.current_x < 3 and len(self.current_word_line) > 0:
            tail_word = self.current_word_line[-1]

            assert abs(word[3] - tail_word[3]) < 2, "coord Y mistach"
            new_tail_word = tuple([tail_word[0], tail_word[1], word[2], tail_word[3], tail_word[4] + word[4]])
            self.current_word_line[-1] = new_tail_word
        else:
            self.current_word_line.append(word)
        self.current_x = word[2]

    def add_line(self, line: str) -> None:
        page_line = PageLine(
            len(self.page_lines),
            line,
            self.current_word_line,
        )
        self.page_lines.append(page_line)
        self.current_word_line = []

    def dump_lines(self) -> None:
        print(f"Page {self.page_number} lines {len(self.page_lines)}:")
        for page_line in self.page_lines:
            print(page_line.plain_text)
            print(">>>", end="")
            for word in page_line.words:
                print(f"  {word[4]}", end="")
            print()  # New line after each word line

    def process_word(self, word: tuple) -> None:
        # print(f"Page width and height: {self.page.rect.width} x {self.page.rect.height}")
        space_width = self.page.rect.width / 48  # Estimated space width, to be refined
        line_height = self.page.rect.height / 64  # Estimated line height, to be refined
        # print(f"Estimated space width and line_height: {space_width} x {line_height}")

        block_no = word[5]
        if block_no != self.current_block_no or abs(word[3] - self.current_y) > line_height:
            self.current_block_no = block_no
            self.current_y = word[3]
            self.add_line(self.current_line)
            self.current_line = ""
            self.append_word(0.0, word)
        else:
            self.append_word(self.current_x, word)


class PDFDigest:
    def __init__(self, pdf_path: str | Path) -> None:
        self.pdf_path = Path(pdf_path)
        self.state_machine = TableParsingStateMachine()

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

    def parse_page_line(self, page_line: PageLine) -> bool:
        self.state_machine.process_word_line(page_line)

    def process_page(self, page: pymupdf.Page, previous_result_page: ResultPage | None = None) -> ResultPage:
        result_page = ResultPage(page, page.number, "")

        for word in page.get_text("words", sort=True):
            result_page.process_word(word)

        if result_page.current_line:
            result_page.add_line(result_page.current_line)

        # result_page.dump_lines()

        for page_line in result_page.page_lines:
            self.parse_page_line(page_line)
        return result_page

    def process_document(self, pages: list = None) -> None:
        with pymupdf.open(self.pdf_path) as document:
            print(f"Total pages: {document.page_count}")
            for page in document:
                if pages and page.number not in pages:
                    continue
                self.process_page(page)


def main() -> None:
    # digest = PDFDigest("reports/test.pdf")

    # digest = PDFDigest("reports/dongpeng-2026q3.pdf")
    # digest.process_document()
    #digest.process_document([7,8,9])

    digest = PDFDigest("reports/dongpeng-2025.pdf")
    digest.process_document([110, 111, 112])
    # digest.process_document([110])


if __name__ == "__main__":
    main()
