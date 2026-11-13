import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from line_item_chart import LineItemChart
from mddigest import TableParsingStage, TableParsingStateMachine


class LineItemChartTests(unittest.TestCase):
    def write_source(self, content: str) -> Path:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        source_path = Path(temporary_directory.name) / "line-items.txt"
        source_path.write_text(content, encoding="utf-8")
        return source_path

    def test_loads_items_and_ids(self) -> None:
        source_path = self.write_source(
            "101|资产|Assets\n"
            "\t101101|流动资产|Current Assets\n"
            "\t\t101101101|货币资金|Monetary Funds\n"
        )

        items = LineItemChart(source_path)

        self.assertEqual(items.get_item_id("货币资金"), 101101101)
        self.assertEqual(items.item_ids["流动资产"], 101101)
        self.assertIn("资产", items)
        self.assertEqual(items["资产"].name, "资产")
        self.assertEqual(items["资产"].english_name, "Assets")

    def test_duplicate_item_id(self) -> None:
        source_path = self.write_source(
            "\t\t301101|股本|Share Capital\n"
            "\t\t301101|实收资本（或股本）|Paid-in Capital (or Share Capital)\n"
            "\t\t301101|实收资本|Paid-in Capital\n"
        )

        items = LineItemChart(source_path)

        self.assertEqual(items.get("股本").name, "股本")
        self.assertEqual(items.get("股本").english_name, "Share Capital")
        self.assertEqual(items.get("实收资本（或股本）").name, "股本")
        self.assertEqual(items.get("实收资本（或股本）").english_name, "Share Capital")
        self.assertEqual(items.get("实收资本").name, "股本")
        self.assertEqual(items.get("实收资本").english_name, "Share Capital")

    def test_get_returns_none_and_get_item_id_rejects_unknown_name(self) -> None:
        source_path = self.write_source("101|资产|Assets\n")
        items = LineItemChart(source_path)

        self.assertIsNone(items.get("不存在"))
        with self.assertRaisesRegex(KeyError, "Unknown financial statement"):
            items.get_item_id("不存在")

    def test_rejects_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            LineItemChart("/tmp/does-not-exist-line-items.txt")

    def test_rejects_malformed_and_duplicate_definitions(self) -> None:
        malformed_path = self.write_source("资产\n")
        with self.assertRaisesRegex(ValueError, "Invalid line-item definition"):
            LineItemChart(malformed_path)

        duplicate_path = self.write_source("101|资产|Assets\n102|资产|Assets\n")
        with self.assertRaisesRegex(ValueError, "Duplicate line-item name"):
            LineItemChart(duplicate_path)

    def test_loads_default_source_file(self) -> None:
        items = LineItemChart()

        self.assertEqual(items.get_item_id("货币资金"), 110101)
        self.assertEqual(items["货币资金"].english_name, "Cash and Cash Equivalents")


class TableParsingStateMachineTests(unittest.TestCase):
    """Unit tests for ``TableParsingStateMachine`` in mddigest.py.

    The state machine resolves its line-item dictionary via the
    CWD-relative path ``line-item-chart.txt``, so these
    tests must be run from the repository root (as the VS Code unittest
    configuration does).
    """

    def make_machine(self) -> TableParsingStateMachine:
        # The constructor prints progress messages; keep test output clean.
        with contextlib.redirect_stdout(io.StringIO()):
            machine = TableParsingStateMachine()
        # Keep the tests independent of a live PostgreSQL instance.
        machine.storage.database_url = None
        return machine

    def process(self, machine: TableParsingStateMachine, md_line: str) -> bool:
        with contextlib.redirect_stdout(io.StringIO()):
            return machine.process_md_line(md_line)

    def test_initial_state(self) -> None:
        machine = self.make_machine()
        self.assertEqual(machine.stage, TableParsingStage.BS_TITLE_DETECTION)
        self.assertIsNone(machine.reporting_date)
        self.assertIsNone(machine.reporting_entity)
        self.assertIsNone(machine.current_reporting_date)
        self.assertIsNone(machine.previous_reporting_date)
        self.assertIsNone(machine.stock_code)
        self.assertIsNone(machine.table_caption_line)
        self.assertEqual(machine.report_creation_time, "")
        self.assertFalse(machine.has_notes)

    def test_process_md_line_ignores_empty_lines(self) -> None:
        machine = self.make_machine()
        self.assertTrue(self.process(machine, ""))
        self.assertTrue(self.process(machine, "   "))
        self.assertEqual(machine.stage, TableParsingStage.BS_TITLE_DETECTION)

    def test_process_as_metadata_extracts_creation_date(self) -> None:
        machine = self.make_machine()
        self.assertTrue(
            self.process(
                machine,
                "<!-- ORIGINAL_PDF_FILE_CREATION_DATE: 2026-10-30 10:30 -->",
            )
        )
        self.assertEqual(machine.report_creation_time, "2026-10-30 10:30")
        self.assertEqual(machine.stage, TableParsingStage.BS_TITLE_DETECTION)

    def test_process_as_metadata_keeps_first_creation_date(self) -> None:
        machine = self.make_machine()
        self.assertTrue(
            self.process(
                machine,
                "<!-- ORIGINAL_PDF_FILE_CREATION_DATE: 2026-10-30 -->",
            )
        )
        self.assertTrue(
            self.process(
                machine,
                "<!-- ORIGINAL_PDF_FILE_CREATION_DATE: 2026-11-01 -->",
            )
        )
        self.assertEqual(machine.report_creation_time, "2026-10-30")

    def test_process_as_metadata_extracts_stock_code(self) -> None:
        machine = self.make_machine()
        self.assertTrue(self.process(machine, "证券代码：600835"))
        self.assertEqual(machine.stock_code, "600835")
        self.assertEqual(machine.stage, TableParsingStage.BS_TITLE_DETECTION)

    def test_process_as_metadata_handles_pdf_page_marker(self) -> None:
        machine = self.make_machine()
        self.assertTrue(self.process(machine, "<!-- PDF_PAGE: 3/120 -->"))
        self.assertEqual(machine.stage, TableParsingStage.BS_TITLE_DETECTION)

    def test_process_as_metadata_returns_false_for_content_lines(self) -> None:
        machine = self.make_machine()
        self.assertFalse(machine.process_as_metadata("合并资产负债表"))
        self.assertFalse(machine.process_as_metadata("some prose"))

    def test_match_dates_normalizes_and_zero_pads(self) -> None:
        self.assertEqual(
            TableParsingStateMachine.match_dates("2026年9月3日"),
            ["2026-09-03"],
        )
        self.assertEqual(
            TableParsingStateMachine.match_dates(
                "2026年9月30日 至 2025年9月30日"
            ),
            ["2026-09-30", "2025-09-30"],
        )
        self.assertEqual(
            TableParsingStateMachine.match_dates("未注明日期"), []
        )

    def test_detect_title_starts_balance_sheet_pipeline(self) -> None:
        machine = self.make_machine()
        self.assertTrue(self.process(machine, "  合并资产负债表  "))
        self.assertEqual(
            machine.stage, TableParsingStage.BS_REPORTING_METADATA_DETECTION
        )

    def test_detect_title_ignores_other_titles(self) -> None:
        machine = self.make_machine()
        self.assertTrue(self.process(machine, "合并利润表"))
        self.assertEqual(machine.stage, TableParsingStage.BS_TITLE_DETECTION)

    def test_reporting_metadata_detection_accepts_date_before_entity(self) -> None:
        machine = self.make_machine()
        machine.stage = TableParsingStage.BS_REPORTING_METADATA_DETECTION
        self.assertTrue(self.process(machine, "2026年9月30日"))
        self.assertEqual(machine.reporting_date, "2026-09-30")
        self.assertEqual(
            machine.stage, TableParsingStage.BS_REPORTING_METADATA_DETECTION
        )

    def test_reporting_metadata_detection_accepts_entity_before_date(self) -> None:
        machine = self.make_machine()
        machine.stage = TableParsingStage.BS_REPORTING_METADATA_DETECTION
        self.assertTrue(
            self.process(machine, "编制单位：某某股份有限公司 单位：元")
        )
        self.assertEqual(machine.reporting_entity, "某某股份有限公司")
        self.assertEqual(
            machine.stage, TableParsingStage.BS_REPORTING_METADATA_DETECTION
        )
        self.assertTrue(self.process(machine, "2026年9月30日"))
        self.assertEqual(machine.reporting_date, "2026-09-30")
        self.assertEqual(
            machine.stage, TableParsingStage.BS_TABLE_CAPTION_EXTRACTION
        )

    def test_reporting_metadata_detection_parses_both_fields_on_one_line(self) -> None:
        machine = self.make_machine()
        machine.stage = TableParsingStage.BS_REPORTING_METADATA_DETECTION
        self.assertTrue(
            self.process(
                machine,
                "编制单位：某某股份有限公司 截至2026年9月30日",
            )
        )
        self.assertEqual(machine.reporting_date, "2026-09-30")
        self.assertEqual(machine.reporting_entity, "某某股份有限公司")
        self.assertEqual(
            machine.stage, TableParsingStage.BS_TABLE_CAPTION_EXTRACTION
        )

    def test_reporting_entity_detection(self) -> None:
        machine = self.make_machine()
        machine.stage = TableParsingStage.BS_REPORTING_METADATA_DETECTION
        machine.reporting_date = "2026-09-30"
        self.assertTrue(
            self.process(machine, "编制单位：某某股份有限公司 单位：元")
        )
        self.assertEqual(machine.reporting_entity, "某某股份有限公司")
        self.assertEqual(
            machine.stage, TableParsingStage.BS_TABLE_CAPTION_EXTRACTION
        )

    def test_reporting_entity_detection_keeps_stage_on_mismatch(self) -> None:
        machine = self.make_machine()
        machine.stage = TableParsingStage.BS_REPORTING_METADATA_DETECTION
        self.assertTrue(self.process(machine, "单位：元"))
        self.assertIsNone(machine.reporting_entity)
        self.assertEqual(
            machine.stage, TableParsingStage.BS_REPORTING_METADATA_DETECTION
        )

    def test_table_caption_with_three_columns(self) -> None:
        machine = self.make_machine()
        machine.stage = TableParsingStage.BS_TABLE_CAPTION_EXTRACTION
        caption = "|项目|2026年9月30日|2025年9月30日|"
        self.assertTrue(self.process(machine, caption))
        self.assertEqual(machine.table_caption_line, caption)
        self.assertFalse(machine.has_notes)
        self.assertEqual(machine.current_reporting_date, "2026-09-30")
        self.assertEqual(machine.previous_reporting_date, "2025-09-30")
        self.assertEqual(
            machine.stage, TableParsingStage.BS_TABLE_ITEMS_EXTRACTION
        )

    def test_table_caption_with_notes_column(self) -> None:
        machine = self.make_machine()
        machine.stage = TableParsingStage.BS_TABLE_CAPTION_EXTRACTION
        self.assertTrue(
            self.process(
                machine, "|项目|附注|2026年9月30日|2025年9月30日|"
            )
        )
        self.assertTrue(machine.has_notes)
        self.assertEqual(machine.current_reporting_date, "2026-09-30")
        self.assertEqual(machine.previous_reporting_date, "2025-09-30")

    def test_table_caption_ignores_non_table_lines(self) -> None:
        machine = self.make_machine()
        machine.stage = TableParsingStage.BS_TABLE_CAPTION_EXTRACTION
        self.assertTrue(self.process(machine, "以下是资产负债表补充说明"))
        self.assertIsNone(machine.table_caption_line)
        self.assertEqual(
            machine.stage, TableParsingStage.BS_TABLE_CAPTION_EXTRACTION
        )

    def drive_to_items_extraction(self, machine: TableParsingStateMachine) -> None:
        self.assertTrue(self.process(machine, "合并资产负债表"))
        self.assertTrue(self.process(machine, "2026年9月30日"))
        self.assertTrue(self.process(machine, "编制单位：某某股份有限公司"))
        self.assertTrue(
            self.process(machine, "|项目|2026年9月30日|2025年9月30日|")
        )
        self.assertEqual(
            machine.stage, TableParsingStage.BS_TABLE_ITEMS_EXTRACTION
        )

    def test_extract_recognized_item_in_three_column_table(self) -> None:
        machine = self.make_machine()
        self.drive_to_items_extraction(machine)
        machine._store_extracted_item = mock.Mock()
        self.assertTrue(
            self.process(machine, "|货币资金|1,234,567.89|1,100,000.00|")
        )
        machine._store_extracted_item.assert_called_once_with(
            101101101, "1,234,567.89", "Monetary Funds"
        )
        self.assertEqual(
            machine.stage, TableParsingStage.BS_TABLE_ITEMS_EXTRACTION
        )

    def test_extract_notes_item_strips_asterisks_and_br_tags(self) -> None:
        machine = self.make_machine()
        machine.stage = TableParsingStage.BS_TABLE_CAPTION_EXTRACTION
        self.assertTrue(
            self.process(
                machine, "|项目|附注|2026年9月30日|2025年9月30日|"
            )
        )
        machine._store_extracted_item = mock.Mock()
        self.assertTrue(
            self.process(
                machine, "|货币资金<br>|5|1,234,567.89*|1,100,000.00*|"
            )
        )
        machine._store_extracted_item.assert_called_once_with(
            101101101, "1,234,567.89", "Monetary Funds"
        )

    def test_extract_item_skips_unknown_names(self) -> None:
        machine = self.make_machine()
        self.drive_to_items_extraction(machine)
        machine._store_extracted_item = mock.Mock()
        self.assertTrue(
            self.process(machine, "|不存在的科目|100.00|50.00|")
        )
        machine._store_extracted_item.assert_not_called()

    def test_extract_item_skips_malformed_lines(self) -> None:
        machine = self.make_machine()
        self.drive_to_items_extraction(machine)
        machine._store_extracted_item = mock.Mock()
        for line in ("|短行|", "货币资金 100 50", "|项目|1|2|3|4|"):
            self.assertTrue(self.process(machine, line))
        machine._store_extracted_item.assert_not_called()
        self.assertEqual(
            machine.stage, TableParsingStage.BS_TABLE_ITEMS_EXTRACTION
        )

    def test_extract_item_skips_non_numeric_values(self) -> None:
        machine = self.make_machine()
        self.drive_to_items_extraction(machine)
        machine._store_extracted_item = mock.Mock()
        self.assertTrue(
            self.process(machine, "|货币资金|--|1,100,000.00|")
        )
        machine._store_extracted_item.assert_not_called()

    def test_total_line_completes_balance_sheet(self) -> None:
        machine = self.make_machine()
        self.drive_to_items_extraction(machine)
        self.assertTrue(
            self.process(
                machine, "|负债和所有者权益总计|2,000,000.00|1,800,000.00|"
            )
        )
        self.assertEqual(machine.stage, TableParsingStage.BS_TABLE_COMPLETED)

    def test_full_balance_sheet_markdown_pipeline(self) -> None:
        machine = self.make_machine()
        machine._store_extracted_item = mock.Mock()

        for line in [
            "<!-- ORIGINAL_PDF_FILE_CREATION_DATE: 2026-10-30 10:30:00 -->",
            "<!-- PDF_PAGE: 1/120 -->",
            "证券代码：600835",
            "财务报告",
            "合并资产负债表",
            "2026年9月30日",
            "编制单位：某某股份有限公司 单位：元",
            "|项目|2026年9月30日|2025年9月30日|",
            "|货币资金|1,234,567.89|1,100,000.00|",
            "|交易性金融资产|50,000.00|60,000.00|",
            "|负债和所有者权益总计|1,500,000.00|1,400,000.00|",
        ]:
            self.assertTrue(self.process(machine, line))

        self.assertEqual(machine.stock_code, "600835")
        self.assertEqual(machine.report_creation_time, "2026-10-30 10:30:00")
        self.assertEqual(machine.reporting_date, "2026-09-30")
        self.assertEqual(machine.reporting_entity, "某某股份有限公司")
        self.assertEqual(machine.current_reporting_date, "2026-09-30")
        self.assertEqual(machine.previous_reporting_date, "2025-09-30")
        self.assertEqual(machine.stage, TableParsingStage.BS_TABLE_COMPLETED)
        self.assertEqual(machine._store_extracted_item.call_count, 3)

    def test_is_number(self) -> None:
        self.assertTrue(TableParsingStateMachine.is_number("1,234,567.89"))
        self.assertTrue(TableParsingStateMachine.is_number("-100"))
        self.assertFalse(TableParsingStateMachine.is_number(""))
        self.assertFalse(TableParsingStateMachine.is_number("abc"))
        self.assertFalse(TableParsingStateMachine.is_number(None))

    def test_close_without_database_connection_is_noop(self) -> None:
        machine = self.make_machine()
        machine.close()
        self.assertIsNone(machine.storage._connection)


if __name__ == "__main__":
    unittest.main()
