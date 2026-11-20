import tempfile
import hashlib
from dataclasses import asdict
from pathlib import Path
import unittest
import json
from unittest.mock import call, patch

import pymupdf

from pdf_extract import STATEMENTS, Statement, ReportExtraction, clean_rows, extract_report, statement_title, main


class PDFExtractTests(unittest.TestCase):
    def test_cli_queries_downloaded_reports_without_path_arguments(self):
        with patch("pdf_extract.extract_report", return_value=ReportExtraction(
                "reports/report.pdf", pdf_hash="registered",
                balance_sheet=Statement("合并资产负债表", text="Assets"),
                income_statement=Statement("合并利润表", text="Profit"),
                cash_flow_statement=Statement("合并现金流量表", text="Cash"))) as extract, \
                patch("pdf_extract.LineItemStorage") as factory, patch("builtins.print"), \
                patch("sys.argv", ["pdf_extract.py"]):
            storage = factory.return_value.__enter__.return_value
            storage.get_downloaded_reports.return_value = [
                {"pdf_hash": "registered", "local_path": "reports/report.pdf"}]
            self.assertEqual(main(), 0)
            storage.get_downloaded_reports.assert_called_once_with()
            extract.assert_called_once_with(Path("reports/report.pdf").resolve())
            storage.get_report_digestion_stage.assert_not_called()
            self.assertEqual(storage.store_extracted_report.call_args.args[0], "registered")

    def test_cli_empty_downloaded_queue_does_not_extract_or_write(self):
        with patch("pdf_extract.extract_report") as extract, \
                patch("pdf_extract.LineItemStorage") as factory, patch("builtins.print") as output, \
                patch("sys.argv", ["pdf_extract.py"]):
            storage = factory.return_value.__enter__.return_value
            storage.get_downloaded_reports.return_value = []
            self.assertEqual(main(), 0)
            extract.assert_not_called()
            storage.store_extracted_report.assert_not_called()
            storage.update_report_digestion_stage.assert_not_called()
            output.assert_called_once_with("No reports at Downloaded stage.")

    def test_cli_requires_database_and_handles_query_failure(self):
        for configured in (False, True):
            with self.subTest(configured=configured), patch("pdf_extract.LineItemStorage") as factory, \
                    patch("pdf_extract.extract_report") as extract, patch("builtins.print"), \
                    patch("sys.argv", ["pdf_extract.py"]):
                storage = factory.return_value.__enter__.return_value
                storage.database_url = "test" if configured else ""
                storage.get_downloaded_reports.side_effect = RuntimeError("database unavailable")
                self.assertEqual(main(), 1)
                extract.assert_not_called()
                storage.store_extracted_report.assert_not_called()
                if not configured:
                    storage.get_downloaded_reports.assert_not_called()

    def test_cli_marks_extraction_error_after_resetting_failed_transaction(self):
        for failure in ("extraction", "database"):
            with self.subTest(failure=failure), patch("pdf_extract.extract_report") as extract, \
                    patch("pdf_extract.LineItemStorage") as factory, patch("builtins.print"), \
                    patch("sys.argv", ["pdf_extract.py"]):
                storage = factory.return_value.__enter__.return_value
                storage.database_url = "test"
                storage.get_downloaded_reports.return_value = [
                    {"pdf_hash": "registered", "local_path": "report.pdf"}]
                if failure == "extraction":
                    extract.side_effect = ValueError("bad PDF")
                else:
                    extract.return_value = ReportExtraction("report.pdf", pdf_hash="registered")
                    storage.store_extracted_report.side_effect = RuntimeError("write failed")
                self.assertEqual(main(), 1)
                self.assertEqual(storage.method_calls[-2:], [
                    call.close(), call.update_report_digestion_stage("registered", "ExtractionError")])

    def test_cli_continues_when_saving_extraction_error_fails(self):
        with patch("pdf_extract.extract_report", side_effect=ValueError("bad PDF")) as extract, \
                patch("pdf_extract.LineItemStorage") as factory, patch("builtins.print") as output, \
                patch("sys.argv", ["pdf_extract.py"]):
            storage = factory.return_value.__enter__.return_value
            storage.get_downloaded_reports.return_value = [
                {"pdf_hash": name, "local_path": name + ".pdf"} for name in ("first", "second")]
            storage.update_report_digestion_stage.side_effect = RuntimeError("database unavailable")
            self.assertEqual(main(), 1)
            self.assertEqual(extract.call_count, 2)
            self.assertEqual(storage.update_report_digestion_stage.call_args_list, [
                call("first", "ExtractionError"), call("second", "ExtractionError")])
            self.assertEqual(storage.close.call_count, 4)
            self.assertTrue(any("unable to save ExtractionError" in str(c) for c in output.call_args_list))

    def test_cli_missing_path_or_file_marks_registered_hash(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("pdf_extract.LineItemStorage") as factory, patch("builtins.print"), \
                patch("sys.argv", ["pdf_extract.py"]):
            storage = factory.return_value.__enter__.return_value
            storage.get_downloaded_reports.return_value = [
                {"pdf_hash": "no-path", "local_path": None},
                {"pdf_hash": "missing-file", "local_path": str(Path(directory) / "missing.pdf")}]
            self.assertEqual(main(), 1)
            self.assertEqual(storage.update_report_digestion_stage.call_args_list, [
                call("no-path", "ExtractionError"), call("missing-file", "ExtractionError")])
            storage.store_extracted_report.assert_not_called()

    def test_cli_rejects_file_hash_mismatch_without_saving_extraction(self):
        with patch("pdf_extract.extract_report", return_value=ReportExtraction(
                "report.pdf", pdf_hash="different")), patch("pdf_extract.LineItemStorage") as factory, \
                patch("builtins.print") as output, patch("sys.argv", ["pdf_extract.py"]):
            storage = factory.return_value.__enter__.return_value
            storage.get_downloaded_reports.return_value = [
                {"pdf_hash": "registered", "local_path": "report.pdf"}]
            self.assertEqual(main(), 1)
            storage.store_extracted_report.assert_not_called()
            storage.update_report_digestion_stage.assert_called_once_with("registered", "ExtractionError")
            self.assertTrue(any("PDF hash mismatch" in str(c) for c in output.call_args_list))

    def test_cli_stores_json_and_identical_markdown_preserving_folders(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("pdf_extract.extract_report") as extract, \
                patch("pdf_extract.LineItemStorage") as factory, patch("builtins.print"):
            root = Path(directory)
            paths = [root / folder / "report.pdf" for folder in ("first", "second")]
            results = [ReportExtraction(
                pdf_file=str(path), pdf_hash=path.parent.name, stock_code="600519",
                reporting_date="2025-12-31", pdf_creation_time="20260313165727",
                balance_sheet=Statement("合并资产负债表", text="Assets"),
                income_statement=Statement("合并利润表", text="Profit"),
                cash_flow_statement=Statement("合并现金流量表", text="Cash"),
            ) for path in paths]
            extract.side_effect = results
            storage = factory.return_value.__enter__.return_value
            storage.get_downloaded_reports.return_value = [
                {"pdf_hash": result.pdf_hash, "local_path": str(path)}
                for result, path in zip(results, paths)]
            with patch("sys.argv", ["pdf_extract.py", "-o", str(root / "output")]):
                self.assertEqual(main(), 0)
            self.assertEqual(extract.call_args_list, [call(path.resolve()) for path in paths])
            storage.update_report_digestion_stage.assert_not_called()
            for result, saved in zip(results, storage.store_extracted_report.call_args_list):
                self.assertEqual(saved.args[0], result.pdf_hash)
                self.assertEqual(json.loads(saved.args[1]), asdict(result))
                folder = root / "output" / result.pdf_hash / "report"
                self.assertEqual((folder / "report.json").read_text(), saved.args[1] + "\n")
                for name, content in zip(("balance_sheet", "income_statement", "cash_flow_statement"),
                                         saved.args[2:]):
                    self.assertEqual((folder / f"{name}.md").read_text(), content)
                self.assertEqual(saved.kwargs["stock_code"], "600519")

    def test_cli_stores_missing_statements_as_null_and_marks_extraction_error(self):
        with patch("pdf_extract.extract_report", return_value=ReportExtraction(
                "report.pdf", pdf_hash="hash")), patch("pdf_extract.LineItemStorage") as factory, \
                patch("builtins.print") as output, patch("sys.argv", ["pdf_extract.py"]):
            storage = factory.return_value.__enter__.return_value
            storage.get_downloaded_reports.return_value = [
                {"pdf_hash": "hash", "local_path": "report.pdf"}]
            self.assertEqual(main(), 1)
            self.assertEqual(storage.store_extracted_report.call_args.args[2:], (None, None, None))
            self.assertTrue(any("Empty extracted statements" in str(c) for c in output.call_args_list))
            storage.update_report_digestion_stage.assert_called_once_with("hash", "ExtractionError")

    def test_cli_any_missing_or_blank_markdown_marks_extraction_error(self):
        fields = ("balance_sheet", "income_statement", "cash_flow_statement")
        for field in fields:
            for empty in (None, "", " \n\t"):
                with self.subTest(field=field, empty=empty), \
                        patch("pdf_extract.LineItemStorage") as factory, \
                        patch("builtins.print"), patch("sys.argv", ["pdf_extract.py"]):
                    result = ReportExtraction("report.pdf", pdf_hash="hash", **{
                        name: Statement(name, text="content") for name in fields})
                    if empty is None:
                        setattr(result, field, None)
                    storage = factory.return_value.__enter__.return_value
                    storage.get_downloaded_reports.return_value = [
                        {"pdf_hash": "hash", "local_path": "report.pdf"}]
                    with patch("pdf_extract.extract_report", return_value=result), \
                            patch.object(Statement, "markdown", autospec=True,
                                         side_effect=lambda statement: empty if statement.title == field else "content"):
                        self.assertEqual(main(), 1)
                    saved = storage.store_extracted_report.call_args
                    self.assertEqual(saved.args[2 + fields.index(field)], empty)
                    storage.update_report_digestion_stage.assert_called_once_with("hash", "ExtractionError")

    def test_cli_rejects_obsolete_path_argument(self):
        with patch("sys.argv", ["pdf_extract.py", "report.pdf"]), \
                patch("sys.stderr"), patch("pdf_extract.LineItemStorage") as factory:
            with self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            factory.assert_not_called()

    def test_markdown_does_not_repeat_plain_text_table(self):
        statement = Statement(
            title="合并现金流量表",
            rows=[["项目", "2025年度"], ["销售商品收到的现金", "123.45"]],
            text="编制单位：测试公司 单位：元 项目 2025年度销售商品收到的现金 123.45",
        )
        markdown = statement.markdown()
        self.assertEqual(markdown.count("销售商品收到的现金"), 1)
        self.assertEqual(markdown.count("123.45"), 1)
        self.assertIn("单位：元", markdown)
        self.assertNotIn("编制单位", markdown)

    def test_markdown_preserves_dates_units_and_text_fallback(self):
        statement = Statement("合并资产负债表", rows=[["项目", "金额"]],
                              text="2025年12月31日 单位：千元 项目 金额\n单位：千元")
        markdown = statement.markdown()
        self.assertIn("2025-12-31", markdown)
        self.assertEqual(markdown.count("单位：千元"), 1)
        self.assertIn("原始内容", Statement("合并利润表", text="原始内容").markdown())

    def test_headings(self):
        self.assertEqual(statement_title("1、合并资产负债表（续）", ["单位：元"]), "合并资产负债表")
        self.assertEqual(statement_title("3、 合并利润表", ["单位：元"]), "合并利润表")
        self.assertIsNone(statement_title("参见合并资产负债表相关项目"))
        self.assertIsNone(statement_title("合并利润表........32"))
        for title in ("合并年初到报告期末利润表", "合并年初到报告期末现金流量表"):
            with self.subTest(title=title):
                self.assertEqual(statement_title(f"3、 {title}（续）", ["单位：元"]), title)
                self.assertIsNone(statement_title(f"参见{title}相关项目"))
                self.assertIsNone(statement_title(f"{title}........32"))

    def test_dated_unaudited_combined_company_balance_sheet_heading(self):
        heading = "2026 年 6 月 30 日未经审计合并及公司资产负债表"
        self.assertEqual(statement_title(heading), "合并及公司资产负债表")
        self.assertEqual(statement_title("合并及公司资产负债表", ["单位：元"]), "合并及公司资产负债表")
        self.assertIsNone(statement_title("参见" + heading))
        self.assertIsNone(statement_title(heading + "相关项目"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                page.insert_text((30, 50), heading, fontname="china-s", fontsize=10)
                page.insert_text((30, 80), "ASSETS")
                doc.save(path)
            result = extract_report(path)
        self.assertIsNotNone(result.balance_sheet)
        self.assertEqual(result.balance_sheet.title, "合并及公司资产负债表")
        self.assertIn("ASSETS", result.balance_sheet.text)
        self.assertEqual(result.balance_sheet_reporting_date, "2026-06-30")
        self.assertEqual(result.reporting_date, "2026-06-30")

    def test_period_ending_unaudited_combined_company_headings(self):
        prefix = "截至 2026 年 6 月 30 日止 6 个月期间未经审计"
        for title in ("合并及公司利润表", "合并及公司现金流量表"):
            with self.subTest(title=title):
                heading = prefix + title
                self.assertEqual(statement_title(heading), title)
                self.assertEqual(statement_title(heading + "（续）"), title)
                self.assertIsNone(statement_title("参见" + heading))
                self.assertIsNone(statement_title(heading + "相关项目"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for y, text in ((50, prefix + "合并及公司利润表"), (80, "PROFIT"),
                                (110, prefix + "合并及公司现金流量表"), (140, "CASH")):
                    page.insert_text((30, y), text, fontname="china-s", fontsize=8)
                doc.save(path)
            result = extract_report(path)
        self.assertIsNotNone(result.income_statement)
        self.assertIsNotNone(result.cash_flow_statement)
        self.assertEqual(result.income_statement.title, "合并及公司利润表")
        self.assertEqual(result.cash_flow_statement.title, "合并及公司现金流量表")
        self.assertIn("PROFIT", result.income_statement.text)
        self.assertNotIn("CASH", result.income_statement.text)
        self.assertIn("CASH", result.cash_flow_statement.text)

    def test_dated_headings_with_chinese_month_count(self):
        headings = (
            ("2021年6月30日合并资产负债表", "合并资产负债表", "balance_sheet", "ASSETS"),
            ("截至2021年6月30日止六个月期间合并利润表", "合并利润表", "income_statement", "PROFIT"),
            ("截至2021年6月30日止六个月期间合并现金流量表", "合并现金流量表", "cash_flow_statement", "CASH"),
        )
        for heading, title, _, _ in headings:
            with self.subTest(heading=heading):
                self.assertEqual(statement_title(heading), title)
                self.assertIsNone(statement_title("参见" + heading))
                self.assertIsNone(statement_title(heading + "相关项目"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, (heading, _, _, body) in enumerate(headings):
                    page.insert_text((30, 50 + index * 80), heading, fontname="china-s", fontsize=8)
                    page.insert_text((30, 80 + index * 80), body)
                doc.save(path)
            result = extract_report(path)
        for _, title, field, body in headings:
            with self.subTest(field=field):
                statement = getattr(result, field)
                self.assertIsNotNone(statement)
                self.assertEqual(statement.title, title)
                self.assertIn(body, statement.text)
                for _, _, other_field, other_body in headings:
                    if other_field != field:
                        self.assertNotIn(other_body, statement.text)
        self.assertEqual(result.reporting_date, "2021-06-30")

    def test_spaced_dated_combined_company_headings(self):
        headings = (
            ("2026 年 6 月 30 日合并及公司资产负债表", "balance_sheet", "合并及公司资产负债表", "ASSETS"),
            ("截至 2026 年 6 月 30 日止六个月期间合并及公司利润表", "income_statement", "合并及公司利润表", "PROFIT"),
            ("截至 2026 年 6 月 30 日止六个月期间合并及公司现金流量表", "cash_flow_statement", "合并及公司现金流量表", "CASH"),
        )
        for heading, _, title, _ in headings:
            with self.subTest(heading=heading):
                self.assertEqual(statement_title(heading), title)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, (heading, _, _, body) in enumerate(headings):
                    page.insert_text((30, 50 + index * 80), heading, fontname="china-s", fontsize=8)
                    page.insert_text((30, 80 + index * 80), body)
                doc.save(path)
            result = extract_report(path)
        for _, field, title, body in headings:
            with self.subTest(field=field):
                statement = getattr(result, field)
                self.assertIsNotNone(statement)
                self.assertEqual(statement.title, title)
                self.assertIn(body, statement.text)
                for _, other_field, _, other_body in headings:
                    if other_field != field:
                        self.assertNotIn(other_body, statement.text)
        self.assertEqual(result.reporting_date, "2026-06-30")

    def test_hyphenated_section_numbers_and_unprefixed_income_statement(self):
        for heading, title in (("3-1．合并利润表", "合并利润表"), ("4-1．利润表", "利润表")):
            with self.subTest(heading=heading), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(statement_title(heading, ["单位：元"]), title)
                self.assertEqual(statement_title(heading + "（续）", ["单位：元"]), title)
                self.assertIsNone(statement_title(heading))
                self.assertIsNone(statement_title("参见" + heading, ["单位：元"]))
                self.assertIsNone(statement_title(heading + "相关项目", ["单位：元"]))
                path = Path(directory) / "report.pdf"
                with pymupdf.open() as doc:
                    page = doc.new_page()
                    for index, text in enumerate((heading, "单位：元", "PROFIT", "母公司资产负债表", "单位：元", "OTHER")):
                        page.insert_text((30, 50 + index * 30), text, fontname="china-s", fontsize=10)
                    doc.save(path)
                result = extract_report(path)
                self.assertIsNotNone(result.income_statement)
                self.assertEqual(result.income_statement.title, title)
                self.assertIn("PROFIT", result.income_statement.text)
                self.assertNotIn("OTHER", result.income_statement.text)

    def test_annual_combined_company_headings_with_hegong_variant(self):
        headings = (
            ("2023年12月31日合并及公司资产负债表", "balance_sheet", "合并及公司资产负债表", "ASSETS"),
            ("2023 年度合共及公司利润表", "income_statement", "合并及公司利润表", "PROFIT"),
            ("2023年度合并及公司现金流量表", "cash_flow_statement", "合并及公司现金流量表", "CASH"),
        )
        for heading, _, title, _ in headings:
            with self.subTest(heading=heading):
                self.assertEqual(statement_title(heading), title)
                self.assertEqual(statement_title(heading + "（续）"), title)
                self.assertIsNone(statement_title("参见" + heading))
                self.assertIsNone(statement_title(heading + "相关项目"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, (heading, _, _, body) in enumerate(headings):
                    page.insert_text((30, 50 + index * 80), heading, fontname="china-s", fontsize=8)
                    page.insert_text((30, 80 + index * 80), body)
                doc.save(path)
            result = extract_report(path)
        for _, field, title, body in headings:
            with self.subTest(field=field):
                statement = getattr(result, field)
                self.assertIsNotNone(statement)
                self.assertEqual(statement.title, title)
                self.assertIn(body, statement.text)
                for _, other_field, _, other_body in headings:
                    if other_field != field:
                        self.assertNotIn(other_body, statement.text)
        self.assertEqual(result.reporting_date, "2023-12-31")

    def test_dated_combined_parent_company_headings(self):
        headings = (
            ("2022 年 12 月 31 日合并及母公司资产负债表", "balance_sheet", "合并及母公司资产负债表", "ASSETS"),
            ("2022 年度合并及母公司利润表", "income_statement", "合并及母公司利润表", "PROFIT"),
            ("2022 年度合并及母公司现金流量表", "cash_flow_statement", "合并及母公司现金流量表", "CASH"),
        )
        for heading, _, title, _ in headings:
            with self.subTest(heading=heading):
                self.assertEqual(statement_title(heading), title)
                self.assertEqual(statement_title(heading + "（续）"), title)
                self.assertIsNone(statement_title("参见" + heading))
                self.assertIsNone(statement_title(heading + "相关项目"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, (heading, _, _, body) in enumerate(headings):
                    page.insert_text((30, 50 + index * 80), heading, fontname="china-s", fontsize=8)
                    page.insert_text((30, 80 + index * 80), body)
                doc.save(path)
            result = extract_report(path)
        for _, field, title, body in headings:
            with self.subTest(field=field):
                statement = getattr(result, field)
                self.assertIsNotNone(statement)
                self.assertEqual(statement.title, title)
                self.assertIn(body, statement.text)
                for _, other_field, _, other_body in headings:
                    if other_field != field:
                        self.assertNotIn(other_body, statement.text)
        self.assertEqual(result.reporting_date, "2022-12-31")

    def test_unaudited_standalone_statements_with_date_and_quarter(self):
        headings = (
            ("2025 年 9 月 30 日资产负债表(未经审计)", "balance_sheet", "资产负债表", "ASSETS"),
            ("2025 年三季度利润表(未经审计)", "income_statement", "利润表", "PROFIT"),
            ("2025 年三季度现金流量表(未经审计)", "cash_flow_statement", "现金流量表", "CASH"),
        )
        for heading, _, title, _ in headings:
            with self.subTest(heading=heading):
                self.assertEqual(statement_title(heading), title)
                self.assertEqual(statement_title(heading.replace("(", "（").replace(")", "）")), title)
                self.assertIsNone(statement_title("参见" + heading))
                self.assertIsNone(statement_title(heading + "相关项目"))
                self.assertIsNone(statement_title(title + "(未经审计)"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, (heading, _, _, body) in enumerate(headings):
                    page.insert_text((30, 50 + index * 80), heading, fontname="china-s", fontsize=8)
                    page.insert_text((30, 80 + index * 80), body)
                doc.save(path)
            result = extract_report(path)
        for _, field, title, body in headings:
            with self.subTest(field=field):
                statement = getattr(result, field)
                self.assertIsNotNone(statement)
                self.assertEqual(statement.title, title)
                self.assertIn(body, statement.text)
                for _, other_field, _, other_body in headings:
                    if other_field != field:
                        self.assertNotIn(other_body, statement.text)
        self.assertEqual(result.reporting_date, "2025-09-30")

    def test_heading_accepts_units_in_rmb_millions(self):
        for title in ("合并资产负债表", "合并利润表", "合并现金流量表"):
            for units in ("单位为人民币百万元", "单位 为 人民币 百万元"):
                with self.subTest(title=title, units=units):
                    self.assertEqual(statement_title(title, [units]), title)

    def test_interim_consolidated_statement_headings(self):
        headings = (
            ("中期合并资产负债表", "balance_sheet", "ASSETS"),
            ("中期合并利润表", "income_statement", "PROFIT"),
            ("中期合并现金流量表", "cash_flow_statement", "CASH"),
        )
        for title, _, _ in headings:
            with self.subTest(title=title):
                self.assertEqual(statement_title(title, ["单位：元"]), title)
                self.assertEqual(statement_title(title + "（续）", ["单位：元"]), title)
                self.assertIsNone(statement_title(title))
                self.assertIsNone(statement_title("参见" + title, ["单位：元"]))
                self.assertIsNone(statement_title(title + "相关项目", ["单位：元"]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, (title, _, body) in enumerate(headings):
                    for offset, text in ((0, title), (25, "单位：元"), (50, body)):
                        page.insert_text((30, 50 + index * 100 + offset), text, fontname="china-s", fontsize=10)
                doc.save(path)
            result = extract_report(path)
        for title, field, body in headings:
            with self.subTest(field=field):
                statement = getattr(result, field)
                self.assertIsNotNone(statement)
                self.assertEqual(statement.title, title)
                self.assertIn(body, statement.text)
                for _, other_field, other_body in headings:
                    if other_field != field:
                        self.assertNotIn(other_body, statement.text)

    def test_parenthesized_section_numbers(self):
        headings = (
            ("（一） 合并资产负债表", "balance_sheet", "合并资产负债表", "ASSETS"),
            ("（三） 合并利润表", "income_statement", "合并利润表", "PROFIT"),
            ("（五）合并现金流量表", "cash_flow_statement", "合并现金流量表", "CASH"),
        )
        for heading, _, title, _ in headings:
            with self.subTest(heading=heading):
                self.assertEqual(statement_title(heading, ["单位：元"]), title)
                self.assertEqual(statement_title(heading.replace("（", "(").replace("）", ")"), ["单位：元"]), title)
                self.assertIsNone(statement_title(heading))
                self.assertIsNone(statement_title("参见" + heading, ["单位：元"]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, (heading, _, _, body) in enumerate(headings):
                    for offset, text in ((0, heading), (25, "单位：元"), (50, body)):
                        page.insert_text((30, 50 + index * 100 + offset), text, fontname="china-s", fontsize=10)
                doc.save(path)
            result = extract_report(path)
        for _, field, title, body in headings:
            with self.subTest(field=field):
                statement = getattr(result, field)
                self.assertIsNotNone(statement)
                self.assertEqual(statement.title, title)
                self.assertIn(body, statement.text)
                for _, other_field, _, other_body in headings:
                    if other_field != field:
                        self.assertNotIn(other_body, statement.text)

    def test_comparative_period_statement_headings(self):
        headings = (
            ("（一）截至 2024 年 9 月 30 日和 2023 年 12 月 31 日的合并资产负债表", "balance_sheet", "合并资产负债表", "ASSETS"),
            ("（二）2024 年 1-9 月和 2023 年 1-9 月的合并利润表 ", "income_statement", "合并利润表", "PROFIT"),
            ("（三）2024 年 1-9 月和 2023 年 1-9 月的合并现金流量表", "cash_flow_statement", "合并现金流量表", "CASH"),
        )
        for heading, _, title, _ in headings:
            with self.subTest(heading=heading):
                self.assertEqual(statement_title(heading), title)
                self.assertEqual(statement_title(heading.strip() + "（续）"), title)
                self.assertIsNone(statement_title("参见" + heading))
                self.assertIsNone(statement_title(heading + "相关项目"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, (heading, _, _, body) in enumerate(headings):
                    page.insert_text((30, 50 + index * 80), heading, fontname="china-s", fontsize=8)
                    page.insert_text((30, 80 + index * 80), body)
                doc.save(path)
            result = extract_report(path)
        for _, field, title, body in headings:
            with self.subTest(field=field):
                statement = getattr(result, field)
                self.assertIsNotNone(statement)
                self.assertEqual(statement.title, title)
                self.assertIn(body, statement.text)
                for _, other_field, _, other_body in headings:
                    if other_field != field:
                        self.assertNotIn(other_body, statement.text)
        self.assertEqual(result.reporting_date, "2024-09-30")

    def test_date_range_period_combined_parent_company_headings(self):
        prefix = "2024 年 1 月 1 日至 6 月 30 日止期间"
        headings = (
            ("合并及母公司利润表", "income_statement", "PROFIT"),
            ("合并及母公司现金流量表", "cash_flow_statement", "CASH"),
        )
        for title, _, _ in headings:
            with self.subTest(title=title):
                self.assertEqual(statement_title(prefix + title), title)
                self.assertEqual(statement_title(prefix + title + "（续）"), title)
                self.assertIsNone(statement_title("参见" + prefix + title))
                self.assertIsNone(statement_title(prefix + title + "相关项目"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, (title, _, body) in enumerate(headings):
                    page.insert_text((30, 50 + index * 80), prefix + title, fontname="china-s", fontsize=8)
                    page.insert_text((30, 80 + index * 80), body)
                doc.save(path)
            result = extract_report(path)
        for title, field, body in headings:
            with self.subTest(field=field):
                statement = getattr(result, field)
                self.assertIsNotNone(statement)
                self.assertEqual(statement.title, title)
                self.assertIn(body, statement.text)
                for _, other_field, other_body in headings:
                    if other_field != field:
                        self.assertNotIn(other_body, statement.text)

    def test_metadata_lookahead_excludes_blank_and_short_unicode_lines(self):
        ignored = ["", "   ", "注", "123", "中文短", " a ", "................................", "  ....  "]
        title = "合并资产负债表"
        self.assertEqual(statement_title(title, ignored * 2 + ["单位：元"]), title)
        self.assertEqual(statement_title(title, ignored + ["其他文字"] * 4 + ["单位：元"]), title)
        self.assertIsNone(statement_title(title, ignored + ["其他文字"] * 5 + ["单位：元"]))
        self.assertIsNone(statement_title(title, ignored))
        self.assertIsNone(statement_title(title, ["....文字"] * 5 + ["单位：元"]))

    def test_extraction_skips_short_lines_in_metadata_lookahead_across_pages(self):
        for cross_page in (False, True):
            with self.subTest(cross_page=cross_page), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.pdf"
                with pymupdf.open() as doc:
                    page = doc.new_page()
                    page.insert_text((30, 50), "合并资产负债表", fontname="china-s")
                    if cross_page:
                        page.insert_text((30, 80), "注", fontname="china-s")
                        page = doc.new_page()
                    for index, text in enumerate(("注", "12", "abc", "中文短", "1", "二", "................................", "....", "单位：元", "ASSETS")):
                        page.insert_text((30, 100 + index * 25), text, fontname="china-s")
                    doc.save(path)
                result = extract_report(path)
                self.assertIsNotNone(result.balance_sheet)
                self.assertIn("ASSETS", result.balance_sheet.text)
                self.assertIn(2 if cross_page else 1, result.balance_sheet.pages)

    def test_headings_require_metadata_within_five_following_lines(self):
        for title, metadata in (
            ("合并利润表", "2026 年 1—6 月"),
            ("合并现金流量表", "2026年1-6月"),
            ("合并年初到报告期末利润表", "2026年1月至6月"),
            ("合并年初到报告期末现金流量表", "2026年1月至6月"),
            ("合并年初至报告期末利润表", "2026年1月至6月"),
            ("合并年初至报告期末现金流量表", "2026年1月至6月"),
            ("合并及公司资产负债表", "2018年12月31日"),
            ("合并及公司利润表", "2026年1月至6月"),
            ("合并及公司现金流量表", "2026年1月至6月"),
            ("合并及母公司资产负债表", "2018年12月31日"),
            ("合并及母公司利润表", "2026年1月至6月"),
            ("合并及母公司现金流量表", "2026年1月至6月"),
            ("合并利润表", "2026年1月至6月"),
            ("合并资产负债表", "2018年12月31日"),
            ("合并资产负债表", "2018-12-31"),
            ("中期合并资产负债表", "2018年12月31日"),
            ("中期合并利润表", "2026年1月至6月"),
            ("中期合并现金流量表", "2026年1月至6月"),
            ("资产负债表", "2018年12月31日"),
            ("利润表", "2026年1月至6月"),
            ("现金流量表", "2026年1月至6月"),
            ("合并资产负债表", "单位（千元）"),
            ("合并利润表", "单位：万元"),
            ("合并现金流量表", "单位：人民币元"),
            ("合并利润表", "币种：人民币"),
            ("合并资产负债表", "编制单位：测试股份有限公司"),
        ):
            with self.subTest(title=title, metadata=metadata):
                for offset in range(5):
                    self.assertEqual(statement_title(title, ["其他文字"] * offset + [metadata]), title)
                self.assertIsNone(statement_title(title, ["其他文字"] * 5 + [metadata]))
                self.assertIsNone(statement_title(title, ["其他文字"] * 5))
                self.assertIsNone(statement_title(title))
        self.assertIsNone(statement_title("合并资产负债表", ["2026年1—6月"]))
        self.assertIsNone(statement_title("合并利润表", ["2018-12-31"]))

    def test_heading_metadata_can_continue_on_next_page_within_five_lines(self):
        for title, field in (("合并资产负债表", "balance_sheet"),
                             ("合并利润表", "income_statement"),
                             ("合并现金流量表", "cash_flow_statement")):
            for offset in range(6):
                with self.subTest(title=title, offset=offset), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "report.pdf"
                    with pymupdf.open() as doc:
                        page = doc.new_page()
                        page.insert_text((50, 780), title, fontname="china-s")
                        page = doc.new_page()
                        for index, text in enumerate(["OTHER"] * offset + ["单位：元", "STATEMENT_BODY"]):
                            page.insert_text((50, 50 + index * 30), text, fontname="china-s")
                        doc.save(path)
                    statement = getattr(extract_report(path), field)
                    if offset < 5:
                        self.assertIsNotNone(statement)
                        self.assertIn("STATEMENT_BODY", statement.text)
                        self.assertIn(2, statement.pages)
                    else:
                        self.assertIsNone(statement)

    def test_heading_does_not_borrow_metadata_from_next_statement(self):
        self.assertIsNone(statement_title("合并资产负债表", ["合并利润表", "单位：元"]))

    def test_extraction_ignores_heading_without_nearby_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, text in enumerate(("合并利润表", "FALSE_POSITIVE", "NOT_METADATA", "MORE_TEXT")):
                    page.insert_text((50, 50 + index * 30), text, fontname="china-s")
                page = doc.new_page()
                for index, text in enumerate(("合并利润表", "2026年1-6月", "ACTUAL_PROFIT")):
                    page.insert_text((50, 50 + index * 30), text, fontname="china-s")
                doc.save(path)
            result = extract_report(path)
            self.assertEqual(result.income_statement.pages, [2])
            self.assertIn("ACTUAL_PROFIT", result.income_statement.text)
            self.assertNotIn("FALSE_POSITIVE", result.income_statement.text)

        for metadata in ("2026 年 1 - 6 月", "2026年1月至6月"):
            self.assertEqual(statement_title("合并利润表", [metadata]), "合并利润表")

    def test_alternative_statement_titles(self):
        for income_title in ("合并年初到报告期末利润表",):
            with self.subTest(income_title=income_title), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.pdf"
                with pymupdf.open() as document:
                    for heading, body in ((income_title, "PROFIT"),
                                          (income_title + "（续）", "CONTINUED"),
                                          ("合并年初到报告期末现金流量表", "CASH")):
                        page = document.new_page()
                        page.insert_text((50, 50), heading, fontname="china-s")
                        page.insert_text((50, 70), "单位：元", fontname="china-s")
                        page.insert_text((50, 90), body)
                    document.save(path)
                result = extract_report(path)
                self.assertEqual(result.income_statement.title, income_title)
                self.assertIn("CONTINUED", result.income_statement.text)
                self.assertNotIn("CASH", result.income_statement.text)
                self.assertIn("CASH", result.cash_flow_statement.text)

    def test_different_profit_periods_are_not_combined(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as document:
                page = document.new_page()
                for index, text in enumerate(("合并利润表", "单位：元", "CURRENT", "合并年初到报告期末利润表", "单位：元", "YEAR_TO_DATE")):
                    page.insert_text((50, 50 + index * 30), text, fontname="china-s")
                document.save(path)
            result = extract_report(path)
            self.assertIn("CURRENT", result.income_statement.text)
            self.assertNotIn("YEAR_TO_DATE", result.income_statement.text)

    def test_empty_columns_keep_missing_amounts(self):
        self.assertEqual(clean_rows([["", "项目", "", "金额"],
                                     [None, "现金", None, None]]),
                         [["项目", "金额"], ["现金", ""]])
        self.assertEqual(clean_rows([]), [])

    def test_accounting_signoffs_end_statement_before_parent_content(self):
        for signoff in ("主管会计工作负责人：张三", " 会计机构负责人：李四"):
            with self.subTest(signoff=signoff), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.pdf"
                with pymupdf.open() as doc:
                    pages = [
                        ["合并利润表", "单位：元", "CONSOLIDATED", signoff,
                         "6 、母公司年初至报告期末利润表", "单位：元", "PARENT"],
                        ["PARENT_CONTINUED"],
                        ["合并现金流量表", "单位：元", "CASH"],
                    ]
                    for lines in pages:
                        page = doc.new_page()
                        for index, text in enumerate(lines):
                            page.insert_text((50, 50 + index * 30), text, fontname="china-s")
                    doc.save(path)
                result = extract_report(path)
                self.assertIn("CONSOLIDATED", result.income_statement.text)
                self.assertNotIn("PARENT", result.income_statement.text)
                self.assertNotIn("负责人", result.income_statement.text)
                self.assertNotIn("母公司", result.income_statement.text)
                self.assertEqual(result.income_statement.pages, [1])
                self.assertIn("CASH", result.cash_flow_statement.text)

    def test_parent_year_to_date_heading_is_a_boundary_without_signoff(self):
        self.assertEqual(statement_title("6 、母公司年初至报告期末利润表", ["单位：元"]),
                         "母公司年初至报告期末利润表")

    def test_statement_endings_keep_final_table_row_and_exclude_following_content(self):
        for heading, field, ending_label in (
            ("合并现金流量表", "cash_flow_statement", "六、期末现金及现金等价物余额"),
            ("合并利润表", "income_statement", "（二）稀释每股收益"),
            ("合并资产负债表", "balance_sheet", "负债和所有者权益总计"),
            ("合并资产负债表", "balance_sheet", "负债和股东权益总计"),
            ("合并资产负债表", "balance_sheet", "负债及所有者权益总计"),
            ("合并资产负债表", "balance_sheet", "负债及股东权益总计"),
            ("合并资产负债表", "balance_sheet", "负债和所有者权益（或股东权益）总计"),
            ("合并资产负债表", "balance_sheet", "负债及所有者权益（或股东权益）总计"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.pdf"
                with pymupdf.open() as doc:
                    page = doc.new_page()
                    page.insert_text((50, 50), heading, fontname="china-s")
                    page.insert_text((50, 70), "单位：元", fontname="china-s", fontsize=10)
                    for x in (50, 340, 430, 520):
                        page.draw_line((x, 80), (x, 200))
                    for y in (80, 120, 160, 200):
                        page.draw_line((50, y), (520, y))
                    for y, cells in (
                        (105, ("项目", "本期", "上期")),
                        (145, (ending_label, "123.45", "678.90")),
                        (185, ("UNRELATED", "999.00", "888.00")),
                    ):
                        for x, text in zip((55, 345, 435), cells):
                            page.insert_text((x, y), text, fontname="china-s", fontsize=10)
                    page = doc.new_page()
                    page.insert_text((50, 50), "FOLLOWING_PAGE")
                    doc.save(path)
                result = extract_report(path)
                statement = getattr(result, field)
                self.assertIn(ending_label, statement.text)
                self.assertIn("123.45", statement.text)
                self.assertIn("678.90", statement.text)
                self.assertNotIn("UNRELATED", statement.text)
                self.assertNotIn("FOLLOWING_PAGE", statement.text)
                self.assertEqual(statement.pages, [1])
                self.assertEqual(statement.rows[-1], [ending_label, "123.45", "678.90"])
                self.assertEqual(len(statement.rows), 2)

    def test_income_text_ending_keeps_values_and_allows_next_statement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, text in enumerate((
                    "合并利润表", "单位：元", "（二）稀释每股收益 0.0123 0.1847", "UNRELATED",
                    "合并现金流量表", "单位：元", "CASH_CONTINUED",
                )):
                    page.insert_text((50, 50 + index * 30), text, fontname="china-s")
                doc.save(path)
            result = extract_report(path)
            self.assertIn("稀释每股收益", result.income_statement.text)
            self.assertIn("0.0123", result.income_statement.text)
            self.assertIn("0.1847", result.income_statement.text)
            self.assertNotIn("UNRELATED", result.income_statement.text)
            self.assertIn("CASH_CONTINUED", result.cash_flow_statement.text)

    def test_balance_sheet_ending_accepts_he_and_ji(self):
        for conjunction in ("和", "及"):
            with self.subTest(conjunction=conjunction), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.pdf"
                ending = f"负债{conjunction}所有者权益总计"
                with pymupdf.open() as doc:
                    page = doc.new_page()
                    for index, text in enumerate(("合并资产负债表", "单位：元", ending, "UNRELATED")):
                        page.insert_text((50, 50 + index * 30), text, fontname="china-s")
                    doc.save(path)
                statement = extract_report(path).balance_sheet
                self.assertIsNotNone(statement)
                self.assertIn(ending, statement.text)
                self.assertNotIn("UNRELATED", statement.text)

    def test_balance_sheet_text_ending_skips_subtotals_and_allows_next_statement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, text in enumerate((
                    "合并资产负债表", "单位：元", "负债合计 111.00", "所有者权益合计 222.00",
                    "负债和所有者权益总计 333.00 444.00", "UNRELATED",
                    "合并利润表", "单位：元", "PROFIT",
                )):
                    page.insert_text((50, 50 + index * 30), text, fontname="china-s")
                doc.save(path)
            result = extract_report(path)
            self.assertIn("负债合计", result.balance_sheet.text)
            self.assertIn("所有者权益合计", result.balance_sheet.text)
            self.assertIn("333.00", result.balance_sheet.text)
            self.assertIn("444.00", result.balance_sheet.text)
            self.assertNotIn("UNRELATED", result.balance_sheet.text)
            self.assertIn("PROFIT", result.income_statement.text)

    def test_cash_flow_text_ending_only_stops_cash_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for index, text in enumerate((
                    "合并利润表", "单位：元", "期末现金及现金等价物余额", "PROFIT_CONTINUED",
                    "合并现金流量表", "单位：元", "期末现金及现金等价物余额 123.45 678.90", "UNRELATED",
                )):
                    page.insert_text((50, 50 + index * 30), text, fontname="china-s")
                doc.save(path)
            result = extract_report(path)
            self.assertIn("PROFIT_CONTINUED", result.income_statement.text)
            self.assertIn("123.45", result.cash_flow_statement.text)
            self.assertNotIn("UNRELATED", result.cash_flow_statement.text)

    def test_continuations_and_same_page_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                doc.set_metadata({"creationDate": "D:20260313165727+08'00'"})
                pages = [
                    ["股票代码：000408", "1、合并资产负债表", "2025年12月31日", "ASSET"],
                    ["1、合并资产负债表（续）", "单位：元", "LIABILITY", "2、母公司资产负债表", "单位：元", "PARENT"],
                    ["PARENT_CONTINUED", "3、合并利润表", "单位：元", "PROFIT", "4、母公司利润表", "单位：元", "PARENT_PROFIT"],
                    ["5、合并现金流量表", "单位：元", "CASH", "6、母公司现金流量表", "单位：元", "PARENT_CASH"],
                ]
                for lines in pages:
                    page = doc.new_page()
                    for index, text in enumerate(lines):
                        page.insert_text((50, 50 + index * 30), text, fontname="china-s")
                doc.save(path)
            result = extract_report(path)
        self.assertEqual(result.stock_code, "000408")
        self.assertEqual(result.reporting_date, "2025-12-31")
        self.assertEqual(result.pdf_creation_time, "20260313165727")
        self.assertEqual(result.balance_sheet.pages, [1, 2])
        self.assertIn("LIABILITY", result.balance_sheet.text)
        self.assertNotIn("PARENT", result.balance_sheet.text)
        self.assertIn("PROFIT", result.income_statement.text)
        self.assertNotIn("PARENT", result.income_statement.text)
        self.assertNotIn("PARENT", result.cash_flow_statement.text)

    def test_missing_statements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.pdf"
            with pymupdf.open() as doc:
                doc.new_page()
                doc.save(path)
            result = extract_report(path)
            expected_hash = hashlib.md5(path.read_bytes()).hexdigest()
        self.assertIsNone(result.balance_sheet)
        self.assertEqual(result.reporting_date, "")
        self.assertEqual(result.pdf_hash, expected_hash)
        self.assertEqual(asdict(result)["pdf_hash"], expected_hash)

    def test_reporting_date_title_patterns_provide_fallback(self):
        for title, expected in (
            ("**2023 **年半年度报告", "2023-06-30"),
            ("2024 年第一季度报告", "2024-03-31"),
            ("2017 年年度报告", "2017-12-31"),
            ("2019 年第三季度报告", "2019-09-30"),
        ):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.pdf"
                with pymupdf.open() as doc:
                    page = doc.new_page()
                    page.insert_text((50, 50), title, fontname="china-s")
                    page = doc.new_page()
                    page.insert_text((50, 50), "合并资产负债表", fontname="china-s")
                    page.insert_text((50, 70), "单位：元", fontname="china-s")
                    page.insert_text((50, 90), "ASSETS")
                    doc.save(path)
                result = extract_report(path)
                self.assertEqual(result.reporting_date, expected)
                self.assertEqual(result.title_reporting_date, expected)
                self.assertEqual(result.balance_sheet_reporting_date, "")
                self.assertIsNone(result.reporting_dates_match)

    def test_reporting_date_sources_compare_and_balance_sheet_takes_priority(self):
        for year, matches in ((2024, True), (2023, False)):
            with self.subTest(year=year), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.pdf"
                with pymupdf.open() as doc:
                    doc.new_page()
                    doc.new_page()
                    page = doc.new_page()
                    page.insert_text((50, 50), "2024 年年度报告", fontname="china-s")
                    page = doc.new_page()
                    page.insert_text((50, 50), "合并资产负债表", fontname="china-s")
                    page.insert_text((50, 90), f"{year}年12月31日", fontname="china-s")
                    doc.save(path)
                result = extract_report(path)
                self.assertEqual(result.reporting_date, f"{year}-12-31")
                self.assertEqual(result.balance_sheet_reporting_date, f"{year}-12-31")
                self.assertEqual(result.title_reporting_date, "2024-12-31")
                self.assertIs(result.reporting_dates_match, matches)

    def test_reporting_date_title_scan_stops_after_three_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.pdf"
            with pymupdf.open() as doc:
                for _ in range(3):
                    doc.new_page()
                page = doc.new_page()
                page.insert_text((50, 50), "2024 年年度报告", fontname="china-s")
                doc.save(path)
            result = extract_report(path)
            self.assertEqual(result.reporting_date, "")
            self.assertEqual(result.title_reporting_date, "")
            self.assertIsNone(result.reporting_dates_match)

    def test_stock_code_path_fallback_preserves_pdf_priority(self):
        for relative_path, pdf_code, expected in (
            ("000702/report.pdf", "", "000702"),
            ("000702/600519_2025年年度报告.pdf", "", "600519"),
            ("300750/report.pdf", "", "300750"),
            ("688001/report.pdf", "", "688001"),
            ("000702/report_123456.pdf", "", "000702"),
            ("123456/report_202501.pdf", "", ""),
            ("000702/report.pdf", "000408", "000408"),
            ("000702/report.pdf", "600519", "600519"),
            ("20250101/report_1234567.pdf", "", ""),
        ):
            with self.subTest(path=relative_path, pdf_code=pdf_code), \
                    tempfile.TemporaryDirectory(prefix="stock-code-test-") as directory:
                path = Path(directory) / relative_path
                path.parent.mkdir(parents=True)
                with pymupdf.open() as doc:
                    doc.new_page()
                    # A code on a later page must still take priority over the path.
                    page = doc.new_page()
                    if pdf_code:
                        page.insert_text((50, 50), f"股票代码：{pdf_code}", fontname="china-s")
                    doc.save(path)
                self.assertEqual(extract_report(path).stock_code, expected)

    def test_sample_reports_when_available(self):
        reports = Path(__file__).resolve().parents[1] / "reports"
        samples = [
            ("藏格矿业2025", "000408", "2025-12-31", "20260313165727"),
            ("宁德时代2025", "300750", "2025-12-31", "20260309191613"),
            ("泸州老窖2026q2", "000568", "2026-06-30", "20260824162929"),
        ]
        if not all((reports / f"{name}.pdf").exists() for name, *_ in samples):
            self.skipTest("Local sample PDFs are not available")
        for name, code, date, creation_time in samples:
            with self.subTest(report=name):
                result = extract_report(reports / f"{name}.pdf")
                self.assertEqual(result.stock_code, code)
                self.assertEqual(result.reporting_date, date)
                self.assertEqual(result.pdf_creation_time, creation_time)
                for field in STATEMENTS.values():
                    statement = getattr(result, field)
                    self.assertIsNotNone(statement)
                    self.assertGreater(len(statement.rows), 30)
                    self.assertTrue(all(len(row) == 3 for row in statement.rows))


if __name__ == "__main__":
    unittest.main()
