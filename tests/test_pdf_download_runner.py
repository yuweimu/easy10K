from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pdf_download_runner import main, mark_downloaded, run_downloads


class DownloadRunnerTests(unittest.TestCase):
    def test_expected_missing_attachments_still_mark_downloaded(self):
        warning = "Failed to parse https://example.com: No 下载公告 link found on https://example.com"
        for entries in [
            [{"status": "SKIPPED", "skip_reason": "NO_DOWNLOAD_LINK"}],
            [{"status": "DONE"}, {"status": "ERROR", "error": warning}],
            [{"status": "ERROR", "error": warning}],
        ]:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
                root = Path(directory)
                path = root / "stocks.txt"
                path.write_text("603338 浙江鼎力\n")
                with patch("pdf_download_runner.fetch_report_download_links", return_value={}), patch("pdf_download_runner.download_reports", return_value={"downloads": entries}):
                    result = run_downloads(path, root / "reports")
                self.assertEqual(result["downloaded"], 1)
                self.assertEqual(result["failed"], 0)
                self.assertIn("# DOWNLOADED", path.read_text())

    def test_first_field_success_duplicates_and_second_run(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            root = Path(directory)
            stock_list = root / "stock_list.txt"
            stock_list.write_text("000568 泸州老窖\n600519 贵州茅台\n000568 重复\n", encoding="utf-8")
            with patch("pdf_download_runner.fetch_report_download_links", return_value={}) as fetch, patch("pdf_download_runner.download_reports", return_value={"downloads": [{"status": "DONE"}]}) as download:
                counts = run_downloads(stock_list, root / "reports")
                self.assertEqual(fetch.call_count, 2)
                fetch.assert_any_call("000568", 30)
                download.assert_any_call("600519", {}, (root / "reports").resolve(), 30, delay_min=1, delay_max=5)
                self.assertEqual(counts["downloaded"], 2)
                self.assertEqual(stock_list.read_text().count("# DOWNLOADED"), 3)
                run_downloads(stock_list, root / "reports")
                self.assertEqual(fetch.call_count, 2)

    def test_existing_directory_marked_lines_comments_and_invalid_entries(self):
        text = "# comment\n\n000568 已有目录\n600519 茅台 # DOWNLOADED\nbad 无效\n"
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            root = Path(directory)
            stock_list = root / "stock_list.txt"
            stock_list.write_text(text)
            (root / "reports/000568").mkdir(parents=True)
            with patch("pdf_download_runner.fetch_report_download_links", return_value={}) as fetch, patch("pdf_download_runner.download_reports", return_value={"downloads": [{"status": "DONE"}]}):
                counts = run_downloads(stock_list, root / "reports")
            fetch.assert_called_once_with("000568", 30)
            self.assertEqual(counts, {"downloaded": 1, "skipped": 1, "failed": 0, "invalid": 1})
            self.assertEqual(stock_list.read_text(), text.replace("000568 已有目录", "000568 已有目录 # DOWNLOADED"))

    def test_failures_marked_error_and_next_stock_processed(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            root = Path(directory)
            stock_list = root / "stock_list.txt"
            stock_list.write_text("000568 A\n600519 B\n601318 C\n")
            with patch("pdf_download_runner.fetch_report_download_links", side_effect=[RuntimeError("network"), {}, {}]), patch("pdf_download_runner.download_reports", side_effect=[{"downloads": [{"status": "ERROR"}]}, {"downloads": [{"status": "DONE"}]}]):
                counts = run_downloads(stock_list, root / "reports")
            self.assertEqual(counts["failed"], 2)
            self.assertEqual(stock_list.read_text(), "000568 A # ERROR\n600519 B # ERROR\n601318 C # DOWNLOADED\n")

    def test_empty_batch_marked_error(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            root = Path(directory)
            stock_list = root / "stock_list.txt"
            stock_list.write_text("600519 A")
            with patch("pdf_download_runner.fetch_report_download_links", return_value={}), patch("pdf_download_runner.download_reports", return_value={"downloads": []}):
                self.assertEqual(run_downloads(stock_list, root / "reports")["failed"], 1)
            self.assertEqual(stock_list.read_text(), "600519 A # ERROR")

    def test_error_marker_is_not_duplicated_and_success_replaces_it(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            root = Path(directory)
            path = root / "stocks.txt"
            path.write_text("600519 A # ERROR\n600519 duplicate\n")
            with patch("pdf_download_runner.fetch_report_download_links", side_effect=TimeoutError("network")) as fetch:
                run_downloads(path, root / "reports", retry_errors=True)
            fetch.assert_called_once()
            self.assertEqual(path.read_text(), "600519 A # ERROR\n600519 duplicate # ERROR\n")
            with patch("pdf_download_runner.fetch_report_download_links", return_value={}), patch("pdf_download_runner.download_reports", return_value={"downloads": [{"status": "DONE"}]}):
                result = run_downloads(path, root / "reports", retry_errors=True)
            self.assertEqual(result["downloaded"], 1)
            self.assertEqual(path.read_text(), "600519 A # DOWNLOADED\n600519 duplicate # DOWNLOADED\n")

    def test_retry_existing_directory_ignores_historical_errors(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            root = Path(directory)
            path = root / "stocks.txt"
            path.write_text("600519 A # ERROR\n")
            (root / "reports/600519").mkdir(parents=True)
            history = [{"status": "ERROR", "error": "old network failure"}]
            (root / "reports/600519/downloads.json").write_text(json.dumps({"downloads": history}))
            with patch("pdf_download_runner.fetch_report_download_links", return_value={}) as fetch, patch("pdf_download_runner.download_reports", return_value={"downloads": history + [{"status": "SKIPPED"}, {"status": "DONE"}]}):
                run_downloads(path, root / "reports", retry_errors=True)
            fetch.assert_called_once_with("600519", 30)
            self.assertEqual(path.read_text(), "600519 A # DOWNLOADED\n")

    def test_error_lines_skipped_by_default_without_requests_or_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "stocks.txt"
            original = "600519 A # ERROR\n603338 B # ERROR\n"
            path.write_text(original)
            with patch("pdf_download_runner.fetch_report_download_links") as fetch:
                result = run_downloads(path, root / "reports")
            fetch.assert_not_called()
            self.assertEqual(result["skipped"], 2)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(path.read_text(), original)

    def test_cli_passes_retry_errors_option(self):
        for args, expected in [([], False), (["--retry-errors"], True)]:
            with self.subTest(args=args), patch("pdf_download_runner.run_downloads", return_value={"failed": 0, "invalid": 0}) as run, patch("builtins.print"):
                self.assertEqual(main(args), 0)
                self.assertEqual(run.call_args.kwargs["retry_errors"], expected)

    def test_custom_delay_cli_and_runner_forwarding(self):
        with patch("pdf_download_runner.run_downloads", return_value={"failed": 0, "invalid": 0}) as run, patch("builtins.print"):
            main(["--delay", "2.5,8"])
            self.assertEqual(run.call_args.kwargs["delay_min"], 2.5)
            self.assertEqual(run.call_args.kwargs["delay_max"], 8)
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            root = Path(directory)
            path = root / "stocks.txt"
            path.write_text("600519 A\n")
            with patch("pdf_download_runner.fetch_report_download_links", return_value={}), patch("pdf_download_runner.download_reports", return_value={"downloads": [{"status": "DONE"}]}) as download:
                run_downloads(path, root / "reports", delay_min=2.5, delay_max=8)
            self.assertEqual(download.call_args.kwargs, {"delay_min": 2.5, "delay_max": 8})

    def test_atomic_mark_preserves_crlf_and_missing_final_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stocks.txt"
            path.write_bytes("000568 名称\r\n600519 名称".encode())
            lines = ["000568 名称\r\n", "600519 名称"]
            mark_downloaded(path, lines, 0)
            mark_downloaded(path, lines, 1)
            self.assertEqual(path.read_bytes(), "000568 名称 # DOWNLOADED\r\n600519 名称 # DOWNLOADED".encode())
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_concurrent_list_edits_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stocks.txt"
            path.write_text("600519 new text\n")
            with self.assertRaisesRegex(RuntimeError, "changed during download"):
                mark_downloaded(path, ["600519 old text\n"], 0)
            self.assertEqual(path.read_text(), "600519 new text\n")

    def test_keyboard_interrupt_does_not_mark_line(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            root = Path(directory)
            path = root / "stocks.txt"
            path.write_text("600519 A\n")
            with patch("pdf_download_runner.fetch_report_download_links", side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    run_downloads(path, root / "reports")
            self.assertEqual(path.read_text(), "600519 A\n")


if __name__ == "__main__":
    unittest.main()
