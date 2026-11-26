from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import MagicMock, patch


RUN_EX = runpy.run_path(str(Path(__file__).resolve().parents[1] / "run-ex.py"))


class RunExTests(unittest.TestCase):
    def test_stock_code_is_required_as_a_positional_argument(self):
        main = RUN_EX["main"]
        with patch.dict(main.__globals__, {"LineItemStorage": MagicMock()}) as globals_patch:
            for arguments in ([], ["--stock_code", "600398"]):
                with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        main(arguments)
                    self.assertEqual(error.exception.code, 2)
            globals_patch["LineItemStorage"].assert_not_called()

    def test_stage_summary_counts_final_non_deprecated_reports(self):
        output = io.StringIO()
        with redirect_stdout(output):
            RUN_EX["_print_stage_summary"](
                ["ExCashFlowOK", "ExIncomeErr", "CashFlowOK", "Deprecated"], "600398",
            )
        summary = output.getvalue()
        self.assertIn("Summary for 600398:", summary)
        self.assertIn("\033[1;35m  Total non-deprecated reports: 3\033[0m", summary)
        self.assertIn("\033[31m  ExIncomeErr: 1\033[0m", summary)
        self.assertIn("\033[32m  ExCashFlowOK: 1\033[0m", summary)
        self.assertIn("\033[32m  CashFlowOK: 1\033[0m", summary)
        self.assertNotIn("  Deprecated:", summary)

    def test_external_pipeline_advances_all_four_steps_and_stops_on_error(self):
        process = RUN_EX["_run_remaining_digestion"]
        group = RUN_EX["ExtractedFiles"]("600398", "2020-12-31", "a" * 32, {})
        storage = MagicMock()
        storage.get_report_digestion_stage.side_effect = [
            "ExAuditOK", "ExBalanceOK", "ExIncomeOK", "ExCashFlowOK",
        ]
        audit = MagicMock()
        bs = MagicMock()
        income = MagicMock()
        cash = MagicMock()
        bs.__name__, income.__name__, cash.__name__ = "digest_bs", "digest_is", "digest_cf"
        for module in (bs, income, cash):
            module.main.return_value = 0
        with patch.dict(process.__globals__, {"audit_extraction": MagicMock(audit_extracted_reports=audit),
                                              "digest_bs": bs, "digest_is": income, "digest_cf": cash}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(process(storage, group, "ExExtractOK"), "ExCashFlowOK")
        audit.assert_called_once()
        self.assertTrue(audit.call_args.kwargs["ex"])
        for module in (bs, income, cash):
            module.main.assert_called_once_with(["--hash", group.pdf_hash, "--ex"])
        self.assertEqual(storage.close.call_count, 4)

        storage.reset_mock()
        storage.get_report_digestion_stage.side_effect = None
        storage.get_report_digestion_stage.return_value = "ExBalanceErr"
        with patch.dict(process.__globals__, {"digest_bs": bs}), redirect_stdout(io.StringIO()):
            self.assertEqual(process(storage, group, "ExAuditOK"), "ExBalanceErr")
        storage.get_report_digestion_stage.assert_called_once_with(group.pdf_hash)

    def test_reimport_preserves_later_ex_stage_before_resuming(self):
        main = RUN_EX["main"]
        pdf_hash = "a" * 32
        with tempfile.TemporaryDirectory() as directory:
            prefix = f"600398.2020-12-31.{pdf_hash}"
            for kind, suffix in (("metadata", "json"), ("balance-sheet", "md"),
                                 ("income-statement", "md"), ("cash-flow-statement", "md")):
                contents = json.dumps({"pdf_md5": pdf_hash}) if kind == "metadata" else "| 项目 | 本期 |"
                (Path(directory) / f"{prefix}.{kind}.{suffix}").write_text(contents, encoding="utf-8")
            factory = MagicMock()
            storage = factory.return_value.__enter__.return_value
            storage.database_url = "postgresql://test"
            storage.get_report_digestion_stage.return_value = "ExBalanceOK"
            storage.store_extracted_report_extras.return_value = True
            process = MagicMock(return_value="ExCashFlowOK")
            with patch.dict(main.__globals__, {"LineItemStorage": factory,
                                              "_run_remaining_digestion": process}), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(["600398", "--outputs-dir", directory]), 0)
            storage.update_report_digestion_stage.assert_not_called()
            self.assertEqual(process.call_args.args[2], "ExBalanceOK")

    def test_completed_reports_are_skipped_before_extraction_files_are_read(self):
        main = RUN_EX["main"]
        pdf_hash = "a" * 32
        for stage in ("CashFlowOK", "ExCashFlowOK"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"600398.2020-12-31.{pdf_hash}.metadata.json"
                path.write_text("invalid metadata", encoding="utf-8")
                factory = MagicMock()
                storage = factory.return_value.__enter__.return_value
                storage.database_url = "postgresql://test"
                storage.get_report_digestion_stage.return_value = stage
                read_metadata = MagicMock(side_effect=AssertionError("read completed report"))
                output = io.StringIO()
                with patch.dict(main.__globals__, {"LineItemStorage": factory, "_read_metadata": read_metadata}), \
                        redirect_stdout(output):
                    self.assertEqual(main(["600398", "--outputs-dir", directory]), 0)
                read_metadata.assert_not_called()
                storage.store_extracted_report_extras.assert_not_called()
                self.assertIn(f"skipped (already {stage})", output.getvalue())
                self.assertIn("Updated 0; skipped 1.", output.getvalue())

    def test_deprecated_list_cannot_replace_a_completed_stage(self):
        mark_deprecated = RUN_EX["_mark_deprecated"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deprecated.json"
            path.write_text(json.dumps({"files": [{"pdf_md5": "a" * 32,
                                                   "stock_code": "600398",
                                                   "reporting_date": "2020-12-31"}]}), encoding="utf-8")
            storage = MagicMock()
            storage.get_report_digestion_stage.return_value = "ExCashFlowOK"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(mark_deprecated(storage, path, None), 0)
            storage.update_report_digestion_stage.assert_not_called()
