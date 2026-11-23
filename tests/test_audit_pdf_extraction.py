from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_extraction import audit_extracted_reports, audit_report, audit_reports, load_cached_report, main
from extract import STATEMENTS, ReportExtraction, Statement


def sample_report():
    report = ReportExtraction("sample.pdf", stock_code="002304",
                              reporting_date="2025-12-31")
    for title, key in STATEMENTS.items():
        setattr(report, key, Statement(title, pages=[1],
                                      rows=[["项目", "1.00", "2.00"] for _ in range(10)],
                                      text="1.00 2.00"))
    return report


class AuditPDFExtractionTests(unittest.TestCase):
    def test_reporting_date_sources_disagreement_is_an_audit_issue(self):
        report = sample_report()
        report.title_reporting_date = "2025-09-30"
        report.balance_sheet_reporting_date = "2025-12-31"
        report.reporting_dates_match = False
        entry = audit_report(report, Path("report.pdf"))
        self.assertIn("reporting_date sources disagree: title 2025-09-30, balance sheet 2025-12-31",
                      entry["issues"])
        self.assertFalse(entry["reporting_dates_match"])
        report.title_reporting_date = "2025-12-31"
        report.reporting_dates_match = True
        self.assertEqual(audit_report(report, Path("report.pdf"))["issues"], [])

    def test_legacy_extraction_json_without_date_sources_still_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "extraction_report.json"
            data = asdict(sample_report())
            for key in ("title_reporting_date", "balance_sheet_reporting_date", "reporting_dates_match"):
                del data[key]
            path.write_text(json.dumps(data))
            report = load_cached_report(path)
            self.assertEqual(report.reporting_date, "2025-12-31")
            self.assertEqual(report.title_reporting_date, "")
            self.assertIsNone(report.reporting_dates_match)

    def test_database_audit_uses_saved_json_and_continues_after_invalid_rows(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("audit_extraction.LineItemStorage") as storage_class, \
                patch("audit_extraction.extract_report") as extract, \
                redirect_stdout(io.StringIO()):
            storage = storage_class.return_value.__enter__.return_value
            storage.get_extracted_reports.return_value = [
                {"pdf_hash": "bad", "local_path": None, "extraction_report": None},
                {"pdf_hash": "good", "local_path": "missing/2025.pdf",
                 "extraction_report": json.dumps(asdict(sample_report()))},
                {"pdf_hash": "fallback", "local_path": None,
                 "extraction_report": json.dumps(asdict(sample_report()))},
            ]
            results = audit_extracted_reports(Path(directory))
            self.assertEqual(len(results), 3)
            self.assertIn("error", results[0])
            self.assertEqual(results[1]["pdf_hash"], "good")
            self.assertEqual(results[1]["file"], "missing/2025.pdf")
            self.assertEqual(results[1]["issues"], [])
            self.assertEqual(results[2]["file"], "sample.pdf")
            self.assertEqual(json.loads((Path(directory) / "audit.json").read_text()), results)
            self.assertEqual(storage.store_extraction_audit.call_count, len(results))
            for call, entry in zip(storage.store_extraction_audit.call_args_list, results):
                self.assertEqual(call.args, (entry["pdf_hash"], entry["issues"], entry["statements"]))
            storage_class.return_value.__exit__.assert_called_once()
            extract.assert_not_called()

    def test_database_audit_does_not_hide_persistence_errors(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("audit_extraction.LineItemStorage") as storage_class, \
                redirect_stdout(io.StringIO()):
            storage = storage_class.return_value.__enter__.return_value
            storage.get_extracted_reports.return_value = [
                {"pdf_hash": "hash", "local_path": "sample.pdf",
                 "extraction_report": json.dumps(asdict(sample_report()))},
            ]
            storage.store_extraction_audit.side_effect = RuntimeError("write failed")
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                audit_extracted_reports(Path(directory))
            self.assertEqual(len(json.loads((Path(directory) / "audit.json").read_text())), 1)

    def test_cli_defaults_to_database_and_supports_fail_on_issues(self):
        with patch("audit_extraction.audit_extracted_reports",
                   return_value=[{"issues": ["missing stock_code"]}]) as audit:
            self.assertEqual(main([]), 0)
            self.assertEqual(main(["--fail-on-issues"]), 1)
            self.assertEqual(audit.call_count, 2)

    def test_empty_database_writes_empty_audit(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("audit_extraction.LineItemStorage") as storage_class, \
                redirect_stdout(io.StringIO()):
            storage_class.return_value.__enter__.return_value.get_extracted_reports.return_value = []
            self.assertEqual(audit_extracted_reports(Path(directory)), [])
            self.assertEqual(json.loads((Path(directory) / "audit.json").read_text()), [])
            storage_class.return_value.__enter__.return_value.store_extraction_audit.assert_not_called()

    def test_good_report_and_filename_stock_code_not_mistaken_for_year(self):
        result = audit_report(sample_report(), Path("002027_分众传媒：2025年年度报告.pdf"))
        self.assertEqual(result["issues"], [])

    def test_filename_reporting_period_patterns(self):
        for label, date in (
            ("年年度", "12-31"), ("年第三季度", "09-30"),
            ("年三季度", "09-30"), ("年第一季度", "03-31"),
            ("年一季度", "03-31"), ("年半年度", "06-30"),
            ("q1", "03-31"), ("Q2", "06-30"),
            ("q3", "09-30"), ("Q4", "12-31"),
        ):
            with self.subTest(label=label):
                report = sample_report()
                report.reporting_date = "2025-" + date
                path = Path(f"2025{label}报告.pdf")
                self.assertEqual(audit_report(report, path)["issues"], [])
                report.reporting_date = "2024-" + date
                self.assertIn(f"reporting_date {report.reporting_date} differs from filename expected 2025-{date}",
                              audit_report(report, path)["issues"])

    def test_metadata_date_and_missing_statement(self):
        report = sample_report()
        report.stock_code = ""
        report.cash_flow_statement = None
        issues = audit_report(report, Path("2025q2.pdf"))["issues"]
        self.assertIn("missing stock_code", issues)
        self.assertIn(f"reporting_date {report.reporting_date} differs from filename expected 2025-06-30", issues)
        self.assertIn("missing cash_flow_statement", issues)

    def test_structure_and_repeated_decimal_counts(self):
        report = sample_report()
        report.balance_sheet = Statement("合并资产负债表", pages=list(range(1, 10)),
                                         rows=[["项目", "1.00"]], text="1.00 1.00 1.00 1.00")
        result = audit_report(report, Path("2025.pdf"))
        self.assertEqual(result["statements"]["balance_sheet"]["missing_decimal_sum"], 3)
        self.assertEqual(len(result["issues"]), 3)

    def test_statement_rows_must_match_width_within_each_table_segment(self):
        report = sample_report()
        report.balance_sheet = Statement(
            "合并资产负债表", pages=[1],
            rows=[
                ["项目", "本期", "上期"],
                ["货币资金", "1.00", "2.00"],
                ["应收账款", "3.00"],
                ["项目", "附注", "本期", "上期"],
                ["存货", "六、1", "4.00", "5.00"],
            ],
            text="1.00 2.00 3.00 4.00 5.00",
        )
        result = audit_report(report, Path("2025.pdf"))
        self.assertIn(
            "balance_sheet[1]: row 3 has 2 columns; expected 3",
            result["issues"],
        )
        self.assertFalse(any("row 5" in issue for issue in result["issues"]))

    def test_recursive_batch_continues_after_error_and_writes_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            (source / "nested").mkdir(parents=True)
            (source / "a.pdf").touch()
            (source / "nested/b.PDF").touch()
            (source / "ignore.md").touch()
            with patch("audit_extraction.extract_report", side_effect=[ValueError("bad PDF"), sample_report()]), redirect_stdout(io.StringIO()):
                results = audit_reports(source, root / "output")
            self.assertEqual(len(results), 2)
            self.assertIn("error", results[0])
            self.assertEqual(results[1]["issues"], [])
            self.assertTrue((root / "output/nested/b/balance_sheet.md").exists())
            self.assertEqual(json.loads((root / "output/audit.json").read_text()), results)

    def test_cached_legacy_results_do_not_reextract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            (source / "2025.pdf").touch()
            folder = root / "output/2025"
            folder.mkdir(parents=True)
            data = asdict(sample_report())
            del data["pdf_hash"]
            (folder / "extraction_report.json").write_text(json.dumps(data))
            self.assertEqual(load_cached_report(folder / "extraction_report.json").pdf_hash, "")
            with patch("audit_extraction.extract_report") as extract, redirect_stdout(io.StringIO()):
                results = audit_reports(source, root / "output", cached=True)
            extract.assert_not_called()
            self.assertEqual(results[0]["issues"], [])

    def test_empty_directory_and_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            root = Path(directory)
            results = audit_reports(root, root / "output")
            self.assertEqual(results, [])
            self.assertEqual(json.loads((root / "output/audit.json").read_text()), [])
            with patch("audit_extraction.audit_reports", return_value=[{"issues": ["missing stock_code"]}]):
                args = ["--input-dir", str(root), "--output-dir", str(root / "output")]
                self.assertEqual(main(args), 0)
                self.assertEqual(main(args + ["--fail-on-issues"]), 1)


if __name__ == "__main__":
    unittest.main()
