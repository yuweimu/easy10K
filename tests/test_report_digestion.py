import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from line_item_storage import LineItemStorage
from main import get_report_digestion, get_digestion_pdf, ReportStageUpdate, update_report_digestion_stage


class ReportDigestionTests(unittest.TestCase):
    def test_api_accepts_deprecated_stage(self):
        with patch("main.LineItemStorage") as factory:
            storage = factory.return_value.__enter__.return_value
            storage.update_report_digestion_stage.return_value = {
                "pdf_hash": "hash", "digestion_stage": "Deprecated", "updated_at": "now",
            }
            result = update_report_digestion_stage("hash", ReportStageUpdate(digestion_stage="Deprecated"))
            self.assertEqual(result["digestion_stage"], "Deprecated")
            storage.update_report_digestion_stage.assert_called_once_with("hash", "Deprecated")

    def test_api_updates_stage_for_selected_report(self):
        with patch("main.LineItemStorage") as factory:
            storage = factory.return_value.__enter__.return_value
            storage.update_report_digestion_stage.return_value = {
                "pdf_hash": "hash", "digestion_stage": "ExtractionSuccess", "updated_at": "now",
            }
            result = update_report_digestion_stage(
                "hash", ReportStageUpdate(digestion_stage="ExtractionSuccess"))
            self.assertEqual(result["digestion_stage"], "ExtractionSuccess")
            storage.update_report_digestion_stage.assert_called_once_with("hash", "ExtractionSuccess")

    def test_api_stage_update_validates_stage(self):
        for value in ("invalid", "", "HandPass", "AuditPass'; DROP TABLE report_digestion", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ReportStageUpdate(digestion_stage=value)

    def test_api_accepts_manual_pass_stage(self):
        with patch("main.LineItemStorage") as factory:
            storage = factory.return_value.__enter__.return_value
            storage.update_report_digestion_stage.return_value = {
                "pdf_hash": "hash", "digestion_stage": "ManualPass", "updated_at": "now",
            }
            result = update_report_digestion_stage("hash", ReportStageUpdate(digestion_stage="ManualPass"))
            self.assertEqual(result["digestion_stage"], "ManualPass")
            storage.update_report_digestion_stage.assert_called_once_with("hash", "ManualPass")

    def test_api_stage_update_missing_report_and_unavailable_database(self):
        data = ReportStageUpdate(digestion_stage="FileOK")
        with patch("main.LineItemStorage") as factory, patch("main.logger.warning"):
            storage = factory.return_value.__enter__.return_value
            storage.update_report_digestion_stage.return_value = None
            with self.assertRaises(HTTPException) as error:
                update_report_digestion_stage("missing", data)
            self.assertEqual(error.exception.status_code, 404)
            storage.update_report_digestion_stage.side_effect = RuntimeError("private database details")
            with self.assertRaises(HTTPException) as error:
                update_report_digestion_stage("hash", data)
            self.assertEqual(error.exception.status_code, 503)
            self.assertNotIn("private", error.exception.detail)
            storage.database_url = None
            storage.update_report_digestion_stage.reset_mock()
            with self.assertRaises(HTTPException) as error:
                update_report_digestion_stage("hash", data)
            self.assertEqual(error.exception.status_code, 503)
            storage.update_report_digestion_stage.assert_not_called()

    def test_registered_pdf_lookup(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        storage = LineItemStorage("test")
        with patch.object(storage, "_connect", return_value=connection):
            cursor.fetchone.return_value = ("reports/download/report.pdf",)
            self.assertEqual(storage.get_digestion_pdf_path("hash"), "reports/download/report.pdf")
            self.assertEqual(cursor.execute.call_args.args[1], ("hash",))
            cursor.fetchone.return_value = None
            self.assertIsNone(storage.get_digestion_pdf_path("missing"))

    def test_pdf_opens_inline_and_rejects_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch("main.PROJECT_DIR", Path(directory)), patch("main.LineItemStorage") as factory:
            root = Path(directory)
            pdf = root / "reports/download/report.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.7\n%%EOF")
            storage = factory.return_value.__enter__.return_value
            storage.get_digestion_pdf_path.return_value = "reports/download/report.pdf"
            response = get_digestion_pdf("hash")
            self.assertEqual(response.path, pdf.resolve())
            self.assertEqual(response.media_type, "application/pdf")
            self.assertIn("inline", response.headers["content-disposition"])
            for path, status in ((None, 404), ("../outside.pdf", 403),
                                 ("reports/missing.pdf", 404), ("reports/download/report.md", 404)):
                storage.get_digestion_pdf_path.return_value = path
                with self.subTest(path=path), self.assertRaises(HTTPException) as error:
                    get_digestion_pdf("hash")
                self.assertEqual(error.exception.status_code, status)

    def test_storage_pagination_and_fields(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (26,)
        cursor.fetchall.return_value = [("abc123", "created", "url", "path", '{"title":"report"}', "FileOK",
                                       "600519", None, "info", "balance", None,
                                       "income", None, "cash", None, "audit result", '{"passed":true}',
                                       '{"statement":"income"}', '{"statement":"cash_flow"}', None)]
        storage = LineItemStorage("test")
        with patch.object(storage, "_connect", return_value=connection):
            result = storage.get_report_digestion(99, 25)
        self.assertEqual((result["page"], result["total_pages"], result["total"]), (2, 2, 26))
        self.assertEqual(result["items"][0]["balance_sheet"], "balance")
        self.assertEqual(result["items"][0]["extraction_report"], "info")
        self.assertNotIn("extraction_info", result["items"][0])
        self.assertEqual(result["items"][0]["extraction_audit"], "audit result")
        self.assertEqual(result["items"][0]["bs_digestion_report"], '{"passed":true}')
        self.assertEqual(result["items"][0]["is_digestion_report"], '{"statement":"income"}')
        self.assertEqual(result["items"][0]["cfs_digestion_report"], '{"statement":"cash_flow"}')
        self.assertNotIn("digestion_report", result["items"][0])
        self.assertNotIn("stock_name", result["items"][0])
        self.assertEqual(result["items"][0]["download_report"], '{"title":"report"}')
        self.assertEqual(len(result["items"][0]), 20)
        sql, parameters = cursor.execute.call_args.args
        self.assertIn("ORDER BY updated_at DESC NULLS LAST, pdf_hash", sql)
        self.assertEqual(parameters, (25, 25))
        for call in cursor.execute.call_args_list:
            self.assertIn("WHERE digestion_stage NOT IN ('ManualPass', 'AuditPass')", call.args[0])
        connection.commit.assert_not_called()

    def test_storage_can_include_passed_reports(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (0,)
        cursor.fetchall.return_value = []
        storage = LineItemStorage("test")
        with patch.object(storage, "_connect", return_value=connection):
            result = storage.get_report_digestion(show_passed=True)
        self.assertEqual(result["total"], 0)
        for call in cursor.execute.call_args_list:
            self.assertNotIn("WHERE", call.args[0])
        connection.commit.assert_not_called()

    def test_empty_and_invalid_pages(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (0,)
        cursor.fetchall.return_value = []
        storage = LineItemStorage("test")
        with patch.object(storage, "_connect", return_value=connection):
            self.assertEqual(storage.get_report_digestion(9)["page"], 1)
            for page, size in ((0, 25), (1, 0), (1, 101)):
                with self.assertRaises(ValueError):
                    storage.get_report_digestion(page, size)

    def test_storage_filters_manual_pass_before_counting_and_pagination(self):
        for show_passed in (False, True):
            with self.subTest(show_passed=show_passed):
                connection = MagicMock()
                cursor = connection.cursor.return_value.__enter__.return_value
                cursor.fetchone.return_value = (11,)
                cursor.fetchall.return_value = []
                storage = LineItemStorage("test")
                with patch.object(storage, "_connect", return_value=connection):
                    result = storage.get_report_digestion(
                        99, 10, show_passed=show_passed, only_manual_pass=True)
                self.assertEqual((result["total"], result["total_pages"], result["page"]), (11, 2, 2))
                for call in cursor.execute.call_args_list:
                    self.assertIn("WHERE digestion_stage = 'ManualPass'", call.args[0])
                self.assertEqual(cursor.execute.call_args.args[1], (10, 10))
                connection.commit.assert_not_called()

    def test_api_forwards_pagination(self):
        with patch("main.LineItemStorage") as factory:
            storage = factory.return_value.__enter__.return_value
            storage.get_report_digestion.return_value = {"items": [], "page": 2}
            self.assertEqual(get_report_digestion(2, 10, pdf_hash=None, stock_code=None), {"items": [], "page": 2})
            storage.get_report_digestion.assert_called_once_with(
                2, 10, show_passed=False, only_manual_pass=False, report_group=None,
                pdf_hash=None, stock_code=None, digestion_stage=None)
            get_report_digestion(1, 25, show_passed=True, pdf_hash=None, stock_code=None)
            storage.get_report_digestion.assert_called_with(
                1, 25, show_passed=True, only_manual_pass=False, report_group=None,
                pdf_hash=None, stock_code=None, digestion_stage=None)
            get_report_digestion(1, 25, only_manual_pass=True, pdf_hash=None, stock_code=None)
            storage.get_report_digestion.assert_called_with(
                1, 25, show_passed=False, only_manual_pass=True, report_group=None,
                pdf_hash=None, stock_code=None, digestion_stage=None)
            for group in ("InProgress", "AuditPass", "ManualPass"):
                get_report_digestion(1, 25, report_group=group, pdf_hash=None, stock_code=None)
                storage.get_report_digestion.assert_called_with(
                    1, 25, show_passed=False, only_manual_pass=False, report_group=group,
                    pdf_hash=None, stock_code=None, digestion_stage=None)

    def test_report_tabs_filter_counts_and_rows_consistently(self):
        for group in ("InProgress", "ExtractionError", "AuditIssues", "BalanceSheetErr",
                      "IncomeStatementErr", "CashFlowStatementErr", "CashFlowStatementOK",
                      "AuditPass", "ManualPass"):
            with self.subTest(group=group):
                connection = MagicMock()
                cursor = connection.cursor.return_value.__enter__.return_value
                cursor.fetchone.return_value = (11,)
                cursor.fetchall.return_value = []
                storage = LineItemStorage("test")
                with patch.object(storage, "_connect", return_value=connection):
                    result = storage.get_report_digestion(99, 10, report_group=group)
                self.assertEqual((result["total"], result["page"], result["total_pages"]), (11, 2, 2))
                parameters = () if group == "InProgress" else (group,)
                for call in cursor.execute.call_args_list:
                    if group == "InProgress":
                        self.assertIn("WHERE digestion_stage NOT IN (", call.args[0])
                        for stage in ("ManualPass", "AuditPass", "Deprecated", "ExtractionSuccess",
                                      "ExtractionError", "AuditIssues", "BalanceSheetErr",
                                      "IncomeStatementErr", "CashFlowStatementErr", "CashFlowStatementOK"):
                            self.assertIn(f"'{stage}'", call.args[0])
                    else:
                        self.assertIn("WHERE digestion_stage = %s", call.args[0])
                self.assertEqual(cursor.execute.call_args_list[0].args[1], parameters)
                self.assertEqual(cursor.execute.call_args.args[1], parameters + (10, 10))
                connection.commit.assert_not_called()
        with self.assertRaises(ValueError):
            LineItemStorage("test").get_report_digestion(report_group="invalid")

    def test_api_unavailable(self):
        for configured in (False, True):
            with patch("main.LineItemStorage") as factory, patch("main.logger.warning"):
                storage = factory.return_value.__enter__.return_value
                storage.database_url = "test" if configured else None
                storage.get_report_digestion.side_effect = RuntimeError("private database details")
                with self.assertRaises(HTTPException) as error:
                    get_report_digestion(1, 25)
                self.assertEqual(error.exception.status_code, 503)
                self.assertNotIn("private", error.exception.detail)
