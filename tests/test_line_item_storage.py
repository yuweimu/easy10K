import unittest
import json
import os
from pathlib import Path
import runpy
from datetime import date
from decimal import Decimal
from unittest import mock

from line_item_storage import LineItemStorage, _normalize_pdf_creation_date
import line_item_storage


class LineItemStorageTests(unittest.TestCase):
    def test_normalizes_pdf_creation_date_with_utc_suffix(self):
        self.assertEqual(
            _normalize_pdf_creation_date("D:20181026075333Z").isoformat(),
            "2018-10-26T07:53:33+00:00",
        )

    def test_statement_reads_preserve_units_and_use_parameters_without_writes(self):
        for statement in ("income_statement", "cash_flow_statement"):
            self.cursor.fetchall.return_value = [(date(2025, 12, 31), 5901, Decimal("0.1256"), "created", "元/股")]
            with LineItemStorage("postgresql://test") as storage:
                result = storage.get_statement_items(statement, "300750")
            query, parameters = self.cursor.execute.call_args.args
            self.assertIn(f"FROM {statement}_items", query)
            self.assertEqual(parameters, ("300750",))
            self.assertEqual(result[0]["amount_unit"], "元/股")
            self.assertEqual(result[0]["amount"], Decimal("0.1256"))
            self.cursor.fetchall.return_value = [("300750", 2, "宁德时代", "Done", [date(2025, 12, 31)])]
            with LineItemStorage("postgresql://test") as storage:
                self.assertEqual(storage.get_statement_stock_codes(statement)[0]["report_count"], 2)
            self.assertIn(f"FROM {statement}_items", self.cursor.execute.call_args.args[0])
        self.connection.commit.assert_not_called()

    def test_statement_read_rejects_untrusted_table_names(self):
        with LineItemStorage("postgresql://test") as storage:
            for method, args in ((storage.get_statement_stock_codes, ("invalid",)),
                                 (storage.get_statement_items, ("invalid", "300750"))):
                with self.assertRaises(ValueError):
                    method(*args)
        self.cursor.execute.assert_not_called()

    def test_new_statement_queues_and_explicit_hash_are_read_only(self):
        for statement, stage in (("income_statement", "BalanceOK"), ("cash_flow_statement", "IncomeOK")):
            for pdf_hash in (None, "hash"):
                with self.subTest(statement=statement, pdf_hash=pdf_hash), LineItemStorage("postgresql://test") as storage:
                    self.cursor.fetchall.return_value = [("hash", "markdown", "600519", "2025-12-31", "created")]
                    rows = storage.get_statement_reports(statement, pdf_hash=pdf_hash)
                    query, parameters = self.cursor.execute.call_args.args
                    self.assertIn("WHERE pdf_hash = %s" if pdf_hash else "WHERE digestion_stage = %s", query)
                    self.assertEqual(parameters, (pdf_hash or stage,))
                    self.assertEqual(rows[0][statement], "markdown")
        self.connection.commit.assert_not_called()

    def test_new_statement_storage_uses_separate_tables_reports_and_stages(self):
        for statement, table, report_field, queued, success, failure in (
                ("income_statement", "income_statement_items", "is_digestion_report", "BalanceOK", "IncomeOK", "IncomeErr"),
                ("cash_flow_statement", "cash_flow_statement_items", "cfs_digestion_report", "IncomeOK", "CashFlowOK", "CashFlowErr")):
            for passed in (True, False):
                with self.subTest(statement=statement, passed=passed), LineItemStorage("postgresql://test") as storage:
                    self.cursor.reset_mock()
                    storage._report_digestion_ready = True
                    self.cursor.fetchone.return_value = (queued,)
                    row = {"pdf_hash": "hash", "stock_code": "600519", "reporting_date": "2025-12-31", "pdf_creation_date": "created"}
                    report = {"passed": passed, "issues": [], "items": [{"item_id": 5901, "amount": "0.1256", "amount_unit": "元/股"}]}
                    storage.store_statement_digestion(statement, row, report)
                    self.assertIn("FOR UPDATE", self.cursor.execute.call_args_list[0].args[0])
                    query, parameters = self.cursor.execute.call_args.args
                    self.assertIn(f"SET {report_field} = %s", query)
                    self.assertNotIn("bs_digestion_report", query)
                    self.assertEqual(parameters[1:], (success if passed else failure, "hash"))
                    self.assertNotIn("items", json.loads(parameters[0]))
                    if passed:
                        insert, values = self.cursor.executemany.call_args.args
                        self.assertIn(f"INSERT INTO {table}", insert)
                        self.assertIn("amount_unit", insert)
                        self.assertEqual(values[0][-2:], ("0.1256", "元/股"))
                    else:
                        self.cursor.executemany.assert_not_called()

    def test_statement_storage_rolls_back_and_enforces_stage_except_explicit_hash(self):
        with LineItemStorage("postgresql://test") as storage:
            storage._report_digestion_ready = True
            self.cursor.fetchone.return_value = ("CashFlowOK",)
            report = {"passed": False, "issues": ["invalid"], "items": []}
            with self.assertRaisesRegex(ValueError, "no longer at BalanceOK"):
                storage.store_statement_digestion("income_statement", {"pdf_hash": "hash"}, report)
            self.connection.rollback.assert_called_once()
            self.connection.commit.assert_not_called()
            storage.store_statement_digestion("income_statement", {"pdf_hash": "hash"}, report, allow_any_stage=True)
            self.connection.commit.assert_called_once()

    def test_new_statement_insert_failure_rolls_back_without_advancing_stage(self):
        with LineItemStorage("postgresql://test") as storage:
            storage._report_digestion_ready = True
            self.cursor.fetchone.return_value = ("IncomeOK",)
            self.cursor.executemany.side_effect = RuntimeError("insert failed")
            row = {"pdf_hash": "hash", "stock_code": "600519", "reporting_date": "2025-12-31", "pdf_creation_date": "created"}
            with self.assertRaisesRegex(RuntimeError, "insert failed"):
                storage.store_statement_digestion("cash_flow_statement", row, {"passed": True, "items": []})
            self.connection.rollback.assert_called_once()
            self.connection.commit.assert_not_called()
            self.assertEqual(self.cursor.execute.call_count, 1)

    def test_statement_identifiers_are_whitelisted_before_sql(self):
        with LineItemStorage("postgresql://test") as storage:
            with self.assertRaises(ValueError):
                storage.get_statement_reports("income_statement; DROP TABLE report_digestion")
            with self.assertRaises(ValueError):
                storage.store_statement_digestion("invalid", {"pdf_hash": "hash"}, {"passed": False})
        self.driver.connect.assert_not_called()

    def test_schema_preserves_old_digestion_report_and_adds_statement_reports(self):
        schema = Path(line_item_storage.__file__).with_name("schema.sql").read_text()
        self.assertIn("RENAME COLUMN digestion_report TO bs_digestion_report", schema)
        self.assertIn("COALESCE(bs_digestion_report, digestion_report)", schema)
        for field in ("bs_digestion_report", "is_digestion_report", "cfs_digestion_report"):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {field} TEXT", schema)

    def test_schema_migrates_old_balance_sheet_stages_before_adding_constraint(self):
        schema = Path(line_item_storage.__file__).with_name("schema.sql").read_text()
        migration = schema.index("WHEN 'DigestionSuccess' THEN 'BalanceOK'")
        self.assertIn("WHEN 'DigestionError' THEN 'BalanceErr'", schema)
        self.assertLess(schema.index("DROP CONSTRAINT IF EXISTS report_digestion_digestion_stage_check"), migration)
        self.assertLess(migration, schema.index("ADD CONSTRAINT report_digestion_digestion_stage_check"))

    def setUp(self) -> None:
        self.driver = mock.MagicMock()
        self.connection = self.driver.connect.return_value
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        patcher = mock.patch.dict("sys.modules", {"psycopg": self.driver})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_get_reports_at_FileOK_filters_stage_without_schema_writes(self):
        self.cursor.fetchall.return_value = [("hash", "reports/report.pdf", "600519", date(2025, 12, 31))]
        with LineItemStorage("postgresql://test") as storage:
            rows = storage.get_reports_at_FileOK()
        self.assertEqual(rows, [{"pdf_hash": "hash", "local_path": "reports/report.pdf",
                                 "stock_code": "600519", "reporting_date": date(2025, 12, 31)}])
        self.cursor.execute.assert_called_once_with(
            "SELECT pdf_hash, local_path, stock_code, reporting_date FROM report_digestion "
            "WHERE digestion_stage = %s ORDER BY pdf_hash", ("FileOK",))
        self.connection.commit.assert_not_called()
        self.connection.close.assert_called_once()

    def test_get_reports_at_FileOK_can_retry_extraction_errors(self):
        self.cursor.fetchall.return_value = []
        with LineItemStorage("postgresql://test") as storage:
            storage.get_reports_at_FileOK(retry=True)
        self.cursor.execute.assert_called_once_with(
            "SELECT pdf_hash, local_path, stock_code, reporting_date FROM report_digestion "
            "WHERE digestion_stage = %s ORDER BY pdf_hash", ("ExtractErr",))

    def test_get_report_by_hash_reads_registered_report(self):
        self.cursor.fetchone.return_value = ("hash", "reports/report.pdf", "600519", date(2025, 12, 31))
        with LineItemStorage("postgresql://test") as storage:
            row = storage.get_report_by_hash("hash")
        self.assertEqual(row, {"pdf_hash": "hash", "local_path": "reports/report.pdf",
                               "stock_code": "600519", "reporting_date": date(2025, 12, 31)})
        self.cursor.execute.assert_called_once_with(
            "SELECT pdf_hash, local_path, stock_code, reporting_date FROM report_digestion WHERE pdf_hash = %s",
            ("hash",))

    def test_get_extracted_reports_filters_success_without_schema_writes(self):
        self.cursor.fetchall.return_value = [("hash", "reports/report.pdf", "{}")]
        with LineItemStorage("postgresql://test") as storage:
            rows = storage.get_extracted_reports()
        self.assertEqual(rows, [{"pdf_hash": "hash", "local_path": "reports/report.pdf",
                                 "extraction_report": "{}"}])
        self.cursor.execute.assert_called_once()
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn("FROM report_digestion", query)
        self.assertIn("WHERE digestion_stage = %s", query)
        self.assertEqual(parameters, ("ExtractOK",))
        self.connection.commit.assert_not_called()
        self.connection.close.assert_called_once()

    def test_extraction_success_tab_filters_count_and_paginated_rows(self):
        self.cursor.fetchone.return_value = (51,)
        self.cursor.fetchall.return_value = []
        with LineItemStorage("postgresql://test") as storage:
            result = storage.get_report_digestion(2, 25, report_group="ExtractOK")
        count, rows = self.cursor.execute.call_args_list
        self.assertIn("WHERE digestion_stage = %s", count.args[0])
        self.assertEqual(count.args[1], ("ExtractOK",))
        self.assertIn("WHERE digestion_stage = %s", rows.args[0])
        self.assertEqual(rows.args[1], ("ExtractOK", 25, 25))
        self.assertEqual((result["total"], result["page"], result["total_pages"]), (51, 2, 3))

    def test_in_progress_tab_excludes_stages_with_separate_tabs(self):
        self.cursor.fetchone.return_value = (0,)
        self.cursor.fetchall.return_value = []
        with LineItemStorage("postgresql://test") as storage:
            storage.get_report_digestion(report_group="InProgress")
        count, rows = self.cursor.execute.call_args_list
        for query in (count.args[0], rows.args[0]):
            self.assertIn(
                "WHERE digestion_stage NOT IN ('ManualPass', 'Deprecated', 'FileErr', 'ExtractErr', 'AuditIssues', "
                "'BalanceErr', 'IncomeErr', "
                "'CashFlowErr', 'CashFlowOK')",
                query,
            )
        self.assertEqual(count.args[1], ())
        self.assertEqual(rows.args[1], (25, 0))

    def test_get_audit_pass_reports_selects_only_eligible_rows(self):
        self.cursor.fetchall.return_value = [("hash", "markdown", "600519", "2025-12-31", "created")]
        with LineItemStorage("postgresql://test") as storage:
            rows = storage.get_audit_pass_reports()
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn("WHERE digestion_stage = %s ORDER BY pdf_hash", query)
        self.assertEqual(parameters, ("AuditPass",))
        self.assertEqual(rows[0], {"pdf_hash": "hash", "balance_sheet": "markdown", "stock_code": "600519",
                                   "reporting_date": "2025-12-31", "pdf_creation_date": "created"})
        self.connection.commit.assert_not_called()

    def test_get_audit_pass_reports_can_filter_by_pdf_hash(self):
        self.cursor.fetchall.return_value = []
        pdf_hash = "007b7f4055bbfeedac1e0742338e0f00"
        with LineItemStorage("postgresql://test") as storage:
            self.assertEqual(storage.get_audit_pass_reports(pdf_hash=pdf_hash), [])
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn("WHERE pdf_hash = %s ORDER BY pdf_hash", query)
        self.assertNotIn("digestion_stage =", query)
        self.assertEqual(parameters, (pdf_hash,))
        self.connection.commit.assert_not_called()

    def test_get_audit_pass_reports_can_retry_balance_errors(self):
        self.cursor.fetchall.return_value = []
        with LineItemStorage("postgresql://test") as storage:
            self.assertEqual(storage.get_audit_pass_reports(retry=True), [])
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn("WHERE digestion_stage = %s ORDER BY pdf_hash", query)
        self.assertEqual(parameters, ("BalanceErr",))
        self.connection.commit.assert_not_called()

    def test_line_item_digestion_commits_items_report_and_stage_together(self):
        row = {"pdf_hash": "hash", "stock_code": "600519", "reporting_date": "2025-12-31",
               "pdf_creation_date": "created"}
        report = {"passed": True, "issues": [], "items": [
            {"item_id": 110101, "amount": "123.45", "english_name": "Cash"}]}
        self.cursor.fetchone.return_value = ("AuditPass",)
        with LineItemStorage("postgresql://test") as storage:
            storage._table_ready = storage._report_digestion_ready = True
            storage.store_line_item_digestion(row, report)
        self.assertIn("FOR UPDATE", self.cursor.execute.call_args_list[0].args[0])
        query, parameters = self.cursor.executemany.call_args.args
        self.assertIn("INSERT INTO balance_sheet_items", query)
        self.assertNotIn("item_english_name", query)
        self.assertEqual(parameters, [("600519", "2025-12-31", 110101, "created", "123.45")])
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn("bs_digestion_report = %s, digestion_stage = %s", query)
        self.assertEqual(json.loads(parameters[0]), {"passed": True, "issues": []})
        self.assertEqual(len(report["items"]), 1)
        self.assertEqual(parameters[1:], ("BalanceOK", "hash"))
        self.connection.commit.assert_called_once()
        self.connection.rollback.assert_not_called()

    def test_failed_line_item_checks_save_report_without_inserting_items(self):
        self.cursor.fetchone.return_value = ("AuditPass",)
        report = {"passed": False, "issues": ["missing required item"], "items": []}
        with LineItemStorage("postgresql://test") as storage:
            storage._report_digestion_ready = True
            storage.store_line_item_digestion({"pdf_hash": "hash"}, report)
        self.cursor.executemany.assert_not_called()
        self.assertEqual(self.cursor.execute.call_args.args[1][1:], ("BalanceErr", "hash"))
        self.assertEqual(json.loads(self.cursor.execute.call_args.args[1][0]),
                         {"passed": False, "issues": ["missing required item"]})
        self.connection.commit.assert_called_once()

    def test_line_item_digestion_rolls_back_partial_inserts(self):
        self.cursor.fetchone.return_value = ("AuditPass",)
        self.cursor.executemany.side_effect = RuntimeError("insert failed")
        row = {"pdf_hash": "hash", "stock_code": "600519", "reporting_date": "2025-12-31",
               "pdf_creation_date": "created"}
        with LineItemStorage("postgresql://test") as storage:
            storage._table_ready = storage._report_digestion_ready = True
            with self.assertRaisesRegex(RuntimeError, "insert failed"):
                storage.store_line_item_digestion(row, {"passed": True, "items": [], "issues": []})
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.assertFalse(any("UPDATE report_digestion" in call.args[0]
                             for call in self.cursor.execute.call_args_list))

    def test_line_item_digestion_leaves_reports_at_other_stages_unchanged(self):
        self.cursor.fetchone.return_value = ("ManualPass",)
        with LineItemStorage("postgresql://test") as storage:
            storage._report_digestion_ready = True
            with self.assertRaisesRegex(ValueError, "no longer at AuditPass"):
                storage.store_line_item_digestion({"pdf_hash": "hash"}, {"passed": False, "issues": []})
        self.cursor.executemany.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()

    def test_report_search_filters_individually_and_together(self):
        for filters, expected in (
            ({"pdf_hash": "  abc123  "}, ("abc123",)),
            ({"stock_code": "600519"}, ("600519",)),
            ({"digestion_stage": "NoStatements"}, ("NoStatements",)),
            ({"pdf_hash": "abc123", "stock_code": "600519", "digestion_stage": "AuditPass"},
             ("abc123", "600519", "AuditPass")),
        ):
            with self.subTest(filters=filters):
                self.cursor.execute.reset_mock()
                self.cursor.fetchone.return_value = (51,)
                self.cursor.fetchall.return_value = []
                with LineItemStorage("postgresql://test") as storage:
                    result = storage.get_report_digestion(2, 25, report_group="Search", **filters)
                count, rows = self.cursor.execute.call_args_list
                self.assertEqual(count.args[1], expected)
                self.assertEqual(rows.args[1], expected + (25, 25))
                self.assertNotIn("NOT IN", count.args[0])
                for field in filters:
                    self.assertIn(field + " = %s", count.args[0])
                    self.assertIn(field + " = %s", rows.args[0])
                self.assertEqual((result["total"], result["page"], result["total_pages"]), (51, 2, 3))

    def test_empty_search_includes_all_stages_and_clamps_page(self):
        self.cursor.fetchone.return_value = (0,)
        self.cursor.fetchall.return_value = []
        with LineItemStorage("postgresql://test") as storage:
            result = storage.get_report_digestion(99, report_group="Search", pdf_hash="  ", stock_code="")
        count, rows = self.cursor.execute.call_args_list
        self.assertNotIn("WHERE", count.args[0])
        self.assertNotIn("WHERE", rows.args[0])
        self.assertEqual(count.args[1], ())
        self.assertEqual(rows.args[1], (25, 0))
        self.assertEqual(result["page"], 1)

    def test_search_values_are_bound_and_invalid_stage_is_rejected(self):
        self.cursor.fetchone.return_value = (0,)
        self.cursor.fetchall.return_value = []
        value = "x' OR 1=1 --"
        with LineItemStorage("postgresql://test") as storage:
            storage.get_report_digestion(report_group="Search", pdf_hash=value)
            self.assertNotIn(value, self.cursor.execute.call_args.args[0])
            self.assertEqual(self.cursor.execute.call_args.args[1], (value, 25, 0))
            self.cursor.execute.reset_mock()
            with self.assertRaisesRegex(ValueError, "Invalid report digestion stage"):
                storage.get_report_digestion(report_group="Search", digestion_stage="invalid")
            self.cursor.execute.assert_not_called()

    def test_explicit_hash_can_save_digestion_at_other_stages(self):
        row = {"pdf_hash": "hash", "stock_code": "600519", "reporting_date": "2025-12-31",
               "pdf_creation_date": "created"}
        for current_stage in ("ManualPass", "Deprecated", "BalanceErr", "BalanceOK", "FileOK", "FileErr"):
            for passed in (True, False):
                with self.subTest(stage=current_stage, passed=passed):
                    self.cursor.fetchone.return_value = (current_stage,)
                    with LineItemStorage("postgresql://test") as storage:
                        storage._table_ready = storage._report_digestion_ready = True
                        storage.store_line_item_digestion(row, {"passed": passed, "items": [], "issues": []},
                                                         allow_any_stage=True)
                    self.assertEqual(self.cursor.execute.call_args.args[1][1:],
                                     ("BalanceOK" if passed else "BalanceErr", "hash"))

    def test_explicit_hash_does_not_save_a_deleted_report(self):
        self.cursor.fetchone.return_value = None
        with LineItemStorage("postgresql://test") as storage:
            storage._report_digestion_ready = True
            with self.assertRaisesRegex(ValueError, "not found"):
                storage.store_line_item_digestion({"pdf_hash": "missing"}, {"passed": False},
                                                 allow_any_stage=True)
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()

    def test_get_report_digestion_stage_by_hash(self):
        with LineItemStorage("postgresql://test") as storage:
            for stage in ("AuditPass", "AuditIssues", "ManualPass", "ExtractOK", None):
                self.cursor.fetchone.return_value = (stage,) if stage else None
                self.assertEqual(storage.get_report_digestion_stage("hash"), stage)
                self.cursor.execute.assert_called_with(
                    "SELECT digestion_stage FROM report_digestion WHERE pdf_hash = %s", ("hash",))
        self.assertEqual(self.cursor.execute.call_count, 6)
        self.connection.commit.assert_called_once()

    def test_get_report_digestion_stage_without_database(self):
        with LineItemStorage("") as storage:
            self.assertIsNone(storage.get_report_digestion_stage("hash"))
        self.driver.connect.assert_not_called()

    def test_manual_stage_update_preserves_content_and_returns_saved_row(self):
        self.cursor.fetchone.return_value = ("hash", "ExtractOK", "now")
        with LineItemStorage("postgresql://test") as storage:
            result = storage.update_report_digestion_stage("hash", "ExtractOK")
        query, parameters = self.cursor.execute.call_args.args
        self.assertEqual(parameters, ("ExtractOK", "hash"))
        self.assertIn("WHERE pdf_hash = %s RETURNING", query)
        self.assertIn("updated_at = CURRENT_TIMESTAMP", query)
        self.assertNotIn("extraction_report =", query)
        self.assertNotIn("extraction_audit =", query)
        self.assertEqual(result, {"pdf_hash": "hash", "digestion_stage": "ExtractOK", "updated_at": "now"})
        self.connection.commit.assert_called_once()

    def test_manual_stage_update_rejects_invalid_stages_and_handles_missing_rows(self):
        with LineItemStorage("postgresql://test") as storage:
            with self.assertRaises(ValueError):
                storage.update_report_digestion_stage("hash", "invalid")
            self.driver.connect.assert_not_called()
            self.cursor.fetchone.return_value = None
            self.assertIsNone(storage.update_report_digestion_stage("missing", "FileOK"))

    def test_manual_pass_and_no_statements_stages_are_persisted_and_allowed_by_schema(self):
        for stage in ("ManualPass", "NoStatements"):
            with self.subTest(stage=stage):
                self.cursor.fetchone.return_value = ("hash", stage, "now")
                with LineItemStorage("postgresql://test") as storage:
                    result = storage.update_report_digestion_stage("hash", stage)
                self.assertEqual(result["digestion_stage"], stage)
                self.assertEqual(self.cursor.execute.call_args.args[1], (stage, "hash"))
                self.assertIn(f"'{stage}'", self.cursor.execute.call_args_list[0].args[0])

    def test_manual_stage_update_does_not_commit_on_write_failure(self):
        with LineItemStorage("postgresql://test") as storage:
            storage._report_digestion_ready = True
            self.cursor.execute.side_effect = RuntimeError("write failed")
            with self.assertRaises(RuntimeError):
                storage.update_report_digestion_stage("hash", "AuditIssues")
            self.connection.commit.assert_not_called()

    def test_store_extraction_audit_saves_json_and_stage_atomically(self):
        statements = {"balance_sheet": {"rows": 10}}
        with LineItemStorage("postgresql://test") as storage:
            storage.store_extraction_audit("passed", [], statements)
            storage.store_extraction_audit("flagged", ["missing stock_code"], {})
        calls = self.cursor.execute.call_args_list
        self.assertEqual(len(calls), 3)
        schema = calls[0].args[0]
        self.assertIn("DROP CONSTRAINT IF EXISTS report_digestion_digestion_stage_check", schema)
        self.assertIn("ADD CONSTRAINT report_digestion_digestion_stage_check", schema)
        for call, pdf_hash, issues, details, stage in (
            (calls[1], "passed", [], statements, "AuditPass"),
            (calls[2], "flagged", ["missing stock_code"], {}, "AuditIssues"),
        ):
            query, values = call.args
            self.assertIn("SET extraction_audit = %s, digestion_stage = %s", query)
            self.assertIn("WHERE pdf_hash = %s", query)
            self.assertIn("updated_at = CURRENT_TIMESTAMP", query)
            self.assertEqual(json.loads(values[0]), {"issues": issues, "statements": details})
            self.assertEqual(values[1:], (stage, pdf_hash))
        self.assertEqual(self.connection.commit.call_count, 2)

    def test_store_extraction_audit_does_not_commit_failed_update(self):
        storage = LineItemStorage("postgresql://test")
        storage._report_digestion_ready = True
        self.cursor.execute.side_effect = RuntimeError("write failed")
        with self.assertRaises(RuntimeError):
            storage.store_extraction_audit("hash", [], {})
        self.connection.commit.assert_not_called()
        storage.close()

    def test_store_extracted_report_upserts_content_and_stage_atomically(self):
        with LineItemStorage("postgresql://test") as storage:
            storage.store_extracted_report("hash", '{"stock_code":"600519"}', "balance", "income", "cash",
                                           local_path="reports/report.pdf", stock_code="600519",
                                           reporting_date="2025-12-31", pdf_creation_date="created")
        query, values = self.cursor.execute.call_args.args
        self.assertIn("ON CONFLICT (pdf_hash) DO UPDATE", query)
        self.assertIn("digestion_stage = 'ExtractOK'", query)
        self.assertIn("updated_at = CURRENT_TIMESTAMP", query)
        self.assertNotIn("download_url =", query)
        self.assertNotIn("extraction_audit =", query)
        self.assertEqual(values, ("hash", '{"stock_code":"600519"}', "balance", "income", "cash",
                                  "reports/report.pdf", "600519", "2025-12-31", "created"))
        self.connection.commit.assert_called_once()

    def test_store_extracted_report_without_database(self):
        with LineItemStorage("") as storage:
            storage.store_extracted_report("hash", "{}", None, None, None, local_path="report.pdf")
        self.driver.connect.assert_not_called()

    def test_store_extracted_report_does_not_commit_on_error(self):
        storage = LineItemStorage("postgresql://test")
        self.cursor.execute.side_effect = RuntimeError("write failed")
        with self.assertRaises(RuntimeError):
            storage.store_extracted_report("hash", "{}", "balance", "income", "cash", local_path="report.pdf")
        self.connection.commit.assert_not_called()
        storage.close()

    def test_report_registry_schema_and_primary_key(self) -> None:
        with LineItemStorage("postgresql://test") as storage:
            storage.ensure_report_registry()
            storage.ensure_report_registry()
        self.assertEqual(self.cursor.execute.call_count, 5)
        query = self.cursor.execute.call_args_list[0].args[0]
        self.assertIn("CREATE TABLE IF NOT EXISTS report_registry", query)
        self.assertIn("PRIMARY KEY (pdf_hash, pdf_creation_time)", query)
        self.assertIn("pdf_hash TEXT NOT NULL", query)
        self.assertIn("stock_code VARCHAR(12),", query)
        self.assertIn("reporting_date DATE,", query)
        for field in ("pdf_download_url", "pdf_local_path", "markdown_file", "preprocessed_file"):
            self.assertIn(f"{field} TEXT,", query)
            self.assertNotIn(f"{field} TEXT NOT NULL", query)
        self.assertIn("updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP", query)
        for sql in (query,):
            self.assertIn("digestion_status TEXT NOT NULL DEFAULT 'PENDING'", sql)
            self.assertNotIn("CHECK (digestion_status", sql)
        self.assertNotIn("digestion_time", query)
        self.assertIn("DROP TRIGGER IF EXISTS", self.cursor.execute.call_args_list[1].args[0])
        self.assertEqual(self.cursor.execute.call_args_list[2].args[0],
                         "ALTER TABLE report_registry DROP COLUMN IF EXISTS digestion_time")
        self.assertIn("DROP FUNCTION IF EXISTS", self.cursor.execute.call_args_list[3].args[0])
        self.assertEqual(self.cursor.execute.call_args_list[4].args[0],
                         "ALTER TABLE report_registry DROP CONSTRAINT IF EXISTS report_registry_digestion_status_check")
        self.connection.commit.assert_called_once()
        self.assertFalse(storage._report_registry_ready)

    def test_downloaded_report_upserts_metadata_and_preserves_existing_identifiers(self):
        with LineItemStorage("postgresql://test") as storage:
            for url in ("https://example.com/first.pdf", "https://example.com/second.pdf"):
                storage.store_downloaded_report("hash", url, str(Path("reports/report.pdf").resolve()),
                                               stock_code="300750", reporting_date="2025-12-31")
        calls = self.cursor.execute.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIn("CREATE TABLE IF NOT EXISTS report_digestion", calls[0].args[0])
        for call, url in zip(calls[1:], ("https://example.com/first.pdf", "https://example.com/second.pdf")):
            query, parameters = call.args
            self.assertIn("INSERT INTO report_digestion (", query)
            self.assertIn("download_report,", query)
            self.assertIn("stock_code, reporting_date, pdf_creation_date, digestion_stage", query)
            self.assertIn("ON CONFLICT (pdf_hash) DO UPDATE SET", query)
            self.assertIn("download_report = COALESCE(EXCLUDED.download_report, report_digestion.download_report)", query)
            self.assertIn("stock_code = COALESCE(EXCLUDED.stock_code, report_digestion.stock_code)", query)
            self.assertIn("digestion_stage = 'FileOK'", query)
            self.assertEqual(parameters, ("hash", url, "reports/report.pdf", None, "300750", "2025-12-31", None))
        self.assertEqual(self.connection.commit.call_count, 2)
        self.assertFalse(storage._report_digestion_ready)

    def test_downloaded_report_without_database_does_not_connect(self):
        with LineItemStorage("") as storage:
            storage.store_downloaded_report("hash", "https://example.com/report.pdf", "report.pdf")
        self.driver.connect.assert_not_called()

    def test_downloaded_report_rejects_missing_values(self):
        for values in (("", "url", "path"), ("hash", " ", "path"), ("hash", "url", "")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                LineItemStorage("postgresql://test").store_downloaded_report(*values)

    def test_store_report_profile_upserts_paths_and_refreshes_timestamp(self) -> None:
        with LineItemStorage("postgresql://test") as storage:
            for output in ("preprocessed/first.md", "preprocessed/updated.md"):
                storage.store_report_profile("hash", "created", "markdown/report.md", output,
                                             stock_code="300750", reporting_date="2025-12-31",
                                             pdf_download_url="https://example.com/report.pdf", pdf_local_path="report.pdf")
        calls = self.cursor.execute.call_args_list
        self.assertEqual(len(calls), 7)
        for call, output in zip(calls[5:], ("preprocessed/first.md", "preprocessed/updated.md")):
            query, parameters = call.args
            self.assertIn("INSERT INTO report_registry", query)
            self.assertIn("ON CONFLICT (pdf_hash, pdf_creation_time)", query)
            self.assertIn("markdown_file = COALESCE(EXCLUDED.markdown_file, report_registry.markdown_file)", query)
            self.assertIn("preprocessed_file = COALESCE(EXCLUDED.preprocessed_file, report_registry.preprocessed_file)", query)
            self.assertIn("updated_at = CURRENT_TIMESTAMP", query)
            self.assertNotIn("pdf_url", query)
            for field in ("stock_code", "reporting_date", "pdf_download_url", "pdf_local_path"):
                self.assertIn(f"{field} = COALESCE(EXCLUDED.{field}, report_registry.{field})", query)
            self.assertNotIn("digestion_status", query)
            self.assertNotIn("digestion_time", query)
            self.assertEqual(parameters, ("hash", "created", "300750", "2025-12-31",
                                          "https://example.com/report.pdf", "report.pdf", "markdown/report.md", output))
        self.assertEqual(self.connection.commit.call_count, 3)
        self.driver.connect.assert_called_once_with("postgresql://test")
        self.connection.close.assert_called_once()

    def test_store_report_profile_accepts_null_paths(self) -> None:
        with LineItemStorage("postgresql://test") as storage:
            storage.store_report_profile("hash", "created", None, None)
        self.assertEqual(self.cursor.execute.call_args.args[1], ("hash", "created", None, None, None, None, None, None))

    def test_registry_stores_relative_local_paths_without_changing_download_url(self) -> None:
        with LineItemStorage("postgresql://test") as storage:
            storage.store_report_profile(
                "hash", "created",
                str(Path("reports/markdown/report.md").resolve()),
                str(Path("reports/preprocessed/report.md").resolve()),
                pdf_local_path=str(Path("reports/pdf/report.pdf").resolve()),
                pdf_download_url="https://example.com/report.pdf",
            )
        self.assertEqual(self.cursor.execute.call_args.args[1], (
            "hash", "created", None, None, "https://example.com/report.pdf",
            "reports/pdf/report.pdf", "reports/markdown/report.md", "reports/preprocessed/report.md",
        ))

    def test_store_report_profile_without_database_does_not_connect(self) -> None:
        with LineItemStorage("") as storage:
            storage.store_report_profile("hash", "created", "source.md", "out.md")
        self.driver.connect.assert_not_called()

    def test_store_report_profile_rejects_invalid_keys_without_connecting(self) -> None:
        for index in range(2):
            for value in (None, "", " \t\n"):
                keys = ["hash", "created"]
                keys[index] = value
                with self.subTest(index=index, value=value), LineItemStorage("postgresql://test") as storage:
                    with self.assertRaisesRegex(ValueError, "nonblank strings"):
                        storage.store_report_profile(*keys, "source.md", "out.md")
        self.driver.connect.assert_not_called()

    def test_report_registry_requires_database_configuration(self) -> None:
        with LineItemStorage("") as storage:
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL is not configured"):
                storage.ensure_report_registry()
        self.driver.connect.assert_not_called()

    def test_digestion_status_upserts_exact_report_version(self) -> None:
        with LineItemStorage("postgresql://test") as storage:
            for status in ("PENDING", "DONE", "ERROR"):
                storage.update_digestion_status("hash", "created", status)
                query, parameters = self.cursor.execute.call_args.args
                self.assertIn("INSERT INTO report_registry (pdf_hash, pdf_creation_time, digestion_status)", query)
                self.assertIn("VALUES (%s, %s, %s)", query)
                self.assertIn("ON CONFLICT (pdf_hash, pdf_creation_time)", query)
                self.assertIn("digestion_status = EXCLUDED.digestion_status", query)
                self.assertIn("updated_at = CURRENT_TIMESTAMP", query)
                for field in ("stock_code", "reporting_date", "pdf_download_url", "pdf_local_path", "markdown_file", "preprocessed_file"):
                    self.assertNotIn(field, query)
                self.assertEqual(parameters, ("hash", "created", status))
        self.assertEqual(self.connection.commit.call_count, 4)

    def test_status_accepts_arbitrary_text(self) -> None:
        with LineItemStorage("postgresql://test") as storage:
            for status in ("RUNNING", "解析失败：缺少表头", "", "error with 'quotes'"):
                with self.subTest(status=status):
                    storage.update_digestion_status("hash", "created", status)
                    self.assertEqual(self.cursor.execute.call_args.args[1], ("hash", "created", status))

    def test_registry_status_lookup_uses_exact_pdf_version(self) -> None:
        with LineItemStorage("postgresql://test") as storage:
            for row, expected in ((None, None), (("PDF2MD_ERROR",), "PDF2MD_ERROR"), (("",), ""), (("DONE",), "DONE")):
                with self.subTest(row=row):
                    self.cursor.fetchone.return_value = row
                    self.assertEqual(storage.get_report_digestion_status("hash", "created"), expected)
                    query, parameters = self.cursor.execute.call_args.args
                    self.assertIn("WHERE pdf_hash = %s AND pdf_creation_time = %s", query)
                    self.assertEqual(parameters, ("hash", "created"))

    def test_registry_status_lookup_without_database_is_optional(self) -> None:
        with LineItemStorage("") as storage:
            self.assertIsNone(storage.get_report_digestion_status("hash", "created"))
        self.driver.connect.assert_not_called()

    def test_registry_file_lookup_is_parameterized(self) -> None:
        self.cursor.fetchone.return_value = ("a.pdf", "a.md", None)
        with LineItemStorage("postgresql://test") as storage:
            result = storage.get_report_files("hash", "created")
            self.assertEqual(result, {"pdf_local_path": "a.pdf", "markdown_file": "a.md", "preprocessed_file": None})
            self.assertEqual(self.cursor.execute.call_args.args[1], ("hash", "created"))
            self.cursor.fetchone.return_value = None
            self.assertIsNone(storage.get_report_files("missing", "created"))

    def test_status_update_skips_missing_keys_or_database(self) -> None:
        for database_url, keys in (
            ("", ("hash", "created")),
            ("postgresql://test", (None, "created")),
            ("postgresql://test", ("", "created")),
            ("postgresql://test", ("hash", " \t")),
        ):
            with self.subTest(keys=keys), LineItemStorage(database_url) as storage:
                storage.update_digestion_status(*keys, "ERROR")
        self.driver.connect.assert_not_called()

    def test_store_reuses_connection_and_creates_schema_once(self) -> None:
        with LineItemStorage("postgresql://test") as storage:
            for amount in ("1,234.50", Decimal("99.25")):
                storage.store_item("300750", "2025-12-31", 101, "created", amount)
        self.driver.connect.assert_called_once_with("postgresql://test")
        calls = self.cursor.execute.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertIn("CREATE TABLE IF NOT EXISTS", calls[0].args[0])
        self.assertNotIn("item_english_name", calls[0].args[0])
        self.assertEqual(calls[1].args[0], "ALTER TABLE balance_sheet_items DROP COLUMN IF EXISTS item_english_name")
        self.assertIn("ON CONFLICT", calls[2].args[0])
        self.assertNotIn("item_english_name", calls[2].args[0])
        self.assertEqual(calls[2].args[1], (
            "300750", "2025-12-31", 101, "created", "1234.50",
        ))
        self.assertEqual(calls[3].args[1][4], "99.25")
        self.assertEqual(self.connection.commit.call_count, 3)
        self.connection.close.assert_called_once()

    def test_no_database_or_invalid_id_does_not_connect(self) -> None:
        for database_url, item_id in (("", 101), ("postgresql://test", 0)):
            with LineItemStorage(database_url) as storage:
                storage.store_item("300750", "2025-12-31", item_id, "created", 1)
        self.driver.connect.assert_not_called()

    def test_startup_loads_project_dotenv_without_overriding_environment(self) -> None:
        # Mock the loader so the test never reads the user's real .env file.
        def load_environment(path, *, override):
            os.environ.setdefault("DATABASE_URL", "postgresql://dotenv-test")

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "dotenv.load_dotenv", side_effect=load_environment,
        ) as loader:
            module = runpy.run_path(line_item_storage.__file__)
            storage = module["LineItemStorage"]()
        loader.assert_called_once_with(
            Path(line_item_storage.__file__).resolve().with_name(".env"), override=False,
        )
        self.assertEqual(storage.database_url, "postgresql://dotenv-test")
        self.driver.connect.assert_not_called()

    def test_environment_and_explicit_database_configuration(self) -> None:
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://environment"}):
            self.assertEqual(LineItemStorage().database_url, "postgresql://environment")
            self.assertEqual(LineItemStorage("postgresql://explicit").database_url, "postgresql://explicit")
            self.assertEqual(LineItemStorage("").database_url, "")

    def test_stock_directory_reads_distinct_codes_without_writes(self) -> None:
        self.cursor.fetchall.return_value = [
            ("000001", 3, "平安银行", "Done", [date(2025, 3, 31), date(2025, 9, 30)]),
            ("300750", 7, None, "InProgress", [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30)]),
        ]
        with LineItemStorage("postgresql://test") as storage:
            self.assertEqual(storage.get_balance_sheet_stock_codes(), [
                {"stock_code": "000001", "report_count": 3, "stock_name": "平安银行", "enrollment": "Done", "has_missing_reporting_dates": True},
                {"stock_code": "300750", "report_count": 7, "stock_name": None, "enrollment": "InProgress", "has_missing_reporting_dates": False},
            ])
        query = self.cursor.execute.call_args.args[0]
        self.assertIn("COUNT(DISTINCT (reporting_date, report_creation_time))", query)
        self.assertIn("FROM balance_sheet_items", query)
        self.assertIn("GROUP BY stock_code", query)
        self.assertIn("ORDER BY stocks.stock_code", query)
        self.assertIn("LEFT JOIN stock_metadata AS metadata ON metadata.stock_code = stocks.stock_code", query)
        self.assertIn("stock_code IS NOT NULL", query)
        self.assertIn("stock_code <> ''", query)
        self.connection.commit.assert_not_called()
        self.connection.close.assert_called_once()

    def test_quarter_completeness_handles_year_boundary_and_duplicate_dates(self):
        for dates, missing in (
            ([date(2024, 12, 31), date(2025, 3, 31)], False),
            ([date(2024, 9, 30), date(2025, 3, 31)], True),
            ([date(2025, 6, 30), date(2025, 6, 30)], False),
            ([date(2025, 1, 15), date(2025, 4, 1)], True),
            ([], True),
        ):
            with self.subTest(dates=dates):
                self.assertEqual(line_item_storage._has_missing_quarters(dates), missing)

    def test_read_uses_parameters_and_preserves_values(self) -> None:
        reporting_date = date(2025, 12, 31)
        self.cursor.fetchall.return_value = [(reporting_date, 101, Decimal("1234.50"), "created")]
        with LineItemStorage("postgresql://test") as storage:
            result = storage.get_balance_sheet_items("300750")
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn("WHERE stock_code = %s", query)
        self.assertIn("ORDER BY reporting_date, item_id, report_creation_time", query)
        self.assertEqual(parameters, ("300750",))
        self.assertEqual(result, [{
            "reporting_date": reporting_date, "item_id": 101, "amount": Decimal("1234.50"),
            "report_creation_time": "created",
        }])
        self.connection.close.assert_called_once()

    def test_source_lookup_is_bound_to_displayed_report_version(self):
        self.cursor.fetchall.return_value = [("hash", "reports/a.pdf", "{}", "单位：元", Decimal("12"), "created", "https://example.com/a.pdf")]
        with LineItemStorage("postgresql://test") as storage:
            record = storage.get_balance_sheet_source("300750", "2025-12-31", 110101, "created")
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn("r.pdf_creation_date = b.report_creation_time", query)
        self.assertIn("b.report_creation_time = %s", query)
        self.assertEqual(parameters, ("300750", "2025-12-31", 110101, "created"))
        self.assertEqual(record["pdf_hash"], "hash")
        self.assertEqual(record["amount"], Decimal("12"))
        self.assertEqual(record["download_url"], "https://example.com/a.pdf")
        self.connection.commit.assert_not_called()

    def test_source_lookup_rejects_ambiguous_pdf_versions(self):
        self.cursor.fetchall.return_value = [("first",), ("second",)]
        with LineItemStorage("postgresql://test") as storage:
            with self.assertRaisesRegex(ValueError, "Multiple PDFs"):
                storage.get_balance_sheet_source("300750", "2025-12-31", 110101, "created")

    def test_registry_pagination_returns_all_fields_in_stable_order(self) -> None:
        self.cursor.fetchone.return_value = (51,)
        row = ("hash", "created", None, date(2025, 12, 31), "local", "a.pdf", "a.md", None, "DONE", None)
        self.cursor.fetchall.return_value = [row]
        with LineItemStorage("postgresql://test") as storage:
            result = storage.get_report_registry(2, 25)
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn("ORDER BY updated_at DESC NULLS LAST, pdf_hash, pdf_creation_time", query)
        self.assertEqual(parameters, (25, 25))
        self.assertEqual((result["total"], result["page"], result["total_pages"]), (51, 2, 3))
        self.assertEqual(result["items"][0]["pdf_hash"], "hash")
        self.assertEqual(len(result["items"][0]), 10)
        self.assertNotIn("digestion_time", result["items"][0])
        self.assertIsNone(result["items"][0]["stock_code"])
        self.connection.commit.assert_not_called()

    def test_registry_pagination_handles_empty_and_out_of_range_pages(self) -> None:
        for total, expected_page in ((0, 1), (26, 2)):
            with self.subTest(total=total), LineItemStorage("postgresql://test") as storage:
                self.cursor.fetchone.return_value = (total,)
                self.cursor.fetchall.return_value = []
                result = storage.get_report_registry(999, 25)
                self.assertEqual(result["page"], expected_page)
                self.assertEqual(result["items"], [])
                self.assertEqual(self.cursor.execute.call_args.args[1], (25, (expected_page - 1) * 25))

    def test_registry_pagination_rejects_invalid_limits(self) -> None:
        with LineItemStorage("postgresql://test") as storage:
            for page, size in ((0, 25), (1, 0), (1, 101)):
                with self.subTest(page=page, size=size), self.assertRaises(ValueError):
                    storage.get_report_registry(page, size)
        self.driver.connect.assert_not_called()

    def test_ignored_item_does_not_connect_or_execute_sql(self) -> None:
        with LineItemStorage("postgresql://test") as storage:
            storage.store_item("300750", "2025-12-31", 99000001, "created", 100)
        self.driver.connect.assert_not_called()
        self.cursor.execute.assert_not_called()
        self.connection.commit.assert_not_called()

    def test_read_without_configuration_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "DATABASE_URL is not configured"):
            LineItemStorage("").get_balance_sheet_items("300750")
        self.driver.connect.assert_not_called()

    def test_context_manager_closes_connection_on_failure(self) -> None:
        self.cursor.execute.side_effect = RuntimeError("query failed")
        with self.assertRaisesRegex(RuntimeError, "query failed"):
            with LineItemStorage("postgresql://test") as storage:
                storage.get_balance_sheet_items("300750")
        self.connection.close.assert_called_once()
        self.assertIsNone(storage._connection)

if __name__ == "__main__":
    unittest.main()
