from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import MagicMock, patch


RUN_EX = runpy.run_path(str(Path(__file__).resolve().parents[1] / "run-ex.py"))


class RunExTests(unittest.TestCase):
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
                    self.assertEqual(main(["--outputs-dir", directory]), 0)
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
