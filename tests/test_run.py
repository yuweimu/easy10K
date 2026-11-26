from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import patch

import run


class RunCliTests(unittest.TestCase):
    def test_stock_code_is_required_positional_argument(self):
        with patch("run.LineItemStorage") as storage:
            for arguments in ([], ["--stock_code", "600398"]):
                with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        run.main(arguments)
                    self.assertEqual(error.exception.code, 2)
            storage.assert_not_called()

    def test_positional_stock_code_is_used(self):
        with patch("run.LineItemStorage") as storage_class, patch("run._copy_error_reports", return_value=0), patch("run._print_stage_summary"):
            storage = storage_class.return_value.__enter__.return_value
            storage.database_url = "postgresql://test"
            storage.get_report_pipeline_rows.return_value = []
            self.assertEqual(run.main(["600398"]), 0)
            self.assertEqual(storage.get_report_pipeline_rows.call_count, 2)
            storage.get_report_pipeline_rows.assert_called_with("600398")
