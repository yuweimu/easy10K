from contextlib import redirect_stderr, redirect_stdout
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import ANY, patch

from cninfo_download import (cninfo_pdf_urls, download_reports, extract_local_report_metadata,
                             find_org_id, main)


class CNInfoDownloadTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("cninfo_download.LineItemStorage")
        self.storage = patcher.start().return_value.__enter__.return_value
        self.addCleanup(patcher.stop)

    def test_extracts_metadata_from_local_path_and_filename(self):
        self.assertEqual(
            extract_local_report_metadata("reports/cninfo/300750/2024年年度报告.pdf"),
            ("300750", "2024-12-31"),
        )
        self.assertEqual(
            extract_local_report_metadata("reports/cninfo/no-code/report.pdf"),
            (None, None),
        )

    @patch("cninfo_download.download_pdf")
    @patch("cninfo_download.time.sleep")
    def test_metadata_is_stored_and_missing_values_are_logged(self, sleep, fetch):
        fetch.side_effect = lambda url, destination, **kwargs: destination.write_bytes(b"PDF") and None
        output = io.StringIO()
        reports = [{"title": "report", "download_url": "https://example.com/report.pdf"}]
        with tempfile.TemporaryDirectory() as root, redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(download_reports("300750", reports, root, delay=(0, 0)), 0)
            entry = json.loads((Path(root) / "300750" / "download_reports.json").read_text())["downloads"][reports[0]["download_url"]]
        self.storage.store_downloaded_report.assert_called_once()
        call_kwargs = self.storage.store_downloaded_report.call_args.kwargs
        self.assertEqual(call_kwargs,
                         {"stock_code": "300750", "reporting_date": None, "pdf_creation_date": None,
                          "download_report": ANY})
        self.assertEqual(json.loads(call_kwargs["download_report"])["title"], "report")
        self.storage.update_report_digestion_stage.assert_called_once_with(
            entry["pdf_hash"], "FileErr")
        self.assertEqual((entry["stock_code"], entry["reporting_date"]), ("300750", None))
        self.assertEqual(entry["warnings"], [
            "reporting_date unavailable from local file name",
            "pdf_creation_time unavailable from PDF metadata",
        ])
        self.assertIn("Warning:", output.getvalue())

    @patch("cninfo_download.post_json")
    @patch("cninfo_download.cninfo_pdf_urls")
    def test_completed_log_skips_all_requests_unless_refreshed(self, reports, post):
        reports.return_value = []
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(output), redirect_stderr(io.StringIO()):
            directory = Path(root) / "300750"
            directory.mkdir()
            (directory / "report.pdf").write_bytes(b"PDF")
            entry = {"title": "2016年年度报告", "file": "report.pdf", "status": "DONE",
                     "pdf_hash": "hash", "download_url": "https://example.com/report.pdf"}
            log = {"stock_code": "300750", "downloads": {entry["download_url"]: entry}}
            (directory / "download_reports.json").write_text(json.dumps(log))
            self.assertEqual(main(["300750", "--output-dir", root]), 0)
            self.assertEqual(main(["300750", "--output-dir", root, "--urls-only"]), 0)
            reports.assert_not_called()
            post.assert_not_called()
            self.storage.store_downloaded_report.assert_not_called()
            self.assertEqual(main(["300750", "--output-dir", root, "--refresh", "--urls-only"]), 0)
            reports.assert_called_once()

    @patch("cninfo_download.download_pdf")
    @patch("cninfo_download.post_json")
    def test_enroll_only_uses_local_files_without_network(self, post, fetch):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(output), redirect_stderr(io.StringIO()):
            directory = Path(root) / "300750"
            directory.mkdir()
            (directory / "2024年一季报.pdf").write_bytes(b"PDF")
            url = "https://example.com/report.pdf"
            (directory / "download_reports.json").write_text(json.dumps({
                "stock_code": "300750", "downloads": {url: {
                    "title": "2024年一季报", "download_url": url,
                    "file": "2024年一季报.pdf", "status": "DOWNLOADED",
                }}}, ensure_ascii=False))
            self.assertEqual(main(["300750", "--output-dir", root, "--enroll-only"]), 0)
        post.assert_not_called()
        fetch.assert_not_called()
        self.storage.store_downloaded_report.assert_called_once()
        self.assertIn("Enrolled local PDF", output.getvalue())

    @patch("cninfo_download.cninfo_pdf_urls", return_value=[])
    def test_missing_file_and_incomplete_listing_do_not_skip_query(self, reports):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(output), redirect_stderr(io.StringIO()):
            directory = Path(root) / "300750"
            directory.mkdir()
            log = {"stock_code": "300750", "downloads": {"url": {
                "title": "2016年年度报告", "file": "report.pdf", "status": "DONE", "pdf_hash": "hash"}}}
            path = directory / "download_reports.json"
            path.write_text(json.dumps(log))
            self.assertEqual(main(["300750", "--output-dir", root, "--urls-only"]), 0)
            (directory / "report.pdf").write_bytes(b"PDF")
            log["listing_complete"] = False
            path.write_text(json.dumps(log))
            self.assertEqual(main(["300750", "--output-dir", root, "--urls-only"]), 0)
        self.assertEqual(reports.call_count, 2)

    @patch("cninfo_download.post_json")
    def test_lookup_requires_exact_code(self, post):
        post.return_value = [{"code": "300751", "orgId": "wrong"},
                             {"code": "300750", "orgId": "GD165627"}]
        self.assertEqual(find_org_id("300750"), "GD165627")
        with self.assertRaisesRegex(ValueError, "not found"):
            find_org_id("000001")

    @patch("cninfo_download.time.sleep")
    @patch("cninfo_download.post_json")
    def test_pagination_filters_summaries_and_deduplicates(self, post, sleep):
        report = {"announcementTitle": "2024年年度报告", "adjunctUrl": "finalpage/report.PDF"}
        post.side_effect = [
            [{"code": "300750", "orgId": "GD165627"}],
            {"totalAnnouncement": 31, "announcements": [report,
                {"announcementTitle": "年度报告摘要", "adjunctUrl": "summary.pdf"},
                {"announcementTitle": "附件", "adjunctUrl": "attachment.doc"}]},
            {"totalAnnouncement": 31, "announcements": [report,
                {"announcementTitle": "2024年季度报告", "adjunctUrl": "quarter.pdf"}]},
        ]
        rows = list(cninfo_pdf_urls("300750"))
        self.assertEqual([row["download_url"] for row in rows],
                         ["https://static.cninfo.com.cn/finalpage/report.PDF",
                          "https://static.cninfo.com.cn/quarter.pdf"])
        self.assertEqual(post.call_args_list[1].args[1]["stock"], "300750,GD165627")
        self.assertEqual(post.call_args_list[2].args[1]["pageNum"], 2)
        sleep.assert_called_once_with(1)

    @patch("cninfo_download.post_json")
    def test_titles_marked_cancelled_or_body_are_excluded(self, post):
        post.return_value = {"totalAnnouncement": 3, "announcements": [
            {"announcementTitle": "2024年年度报告", "adjunctUrl": "report.pdf"},
            {"announcementTitle": "2024年年度报告 已取消", "adjunctUrl": "cancelled.pdf"},
            {"announcementTitle": "2024年年度报告 正文", "adjunctUrl": "body.pdf"},
        ]}
        rows = list(cninfo_pdf_urls("300750", "GD165627"))
        self.assertEqual([row["download_url"] for row in rows],
                         ["https://static.cninfo.com.cn/report.pdf"])

    @patch("cninfo_download.post_json")
    def test_english_version_titles_are_excluded(self, post):
        titles = ["2024年年度报告（英文）", "2024年年度报告(英文版)",
                  "2024年年度报告-英文", "2024年年度报告—英文版",
                  "2024年年度报告－英文", "2024年年度报告_英文版",
                  "2024年年度报告（英 文）", "2024年年度报告（中文版）",
                  "2024年年度报告（修订版）"]
        post.return_value = {"totalAnnouncement": len(titles), "announcements": [
            {"announcementTitle": title, "adjunctUrl": f"{index}.pdf"}
            for index, title in enumerate(titles)
        ]}
        rows = list(cninfo_pdf_urls("300750", "GD165627"))
        self.assertEqual([row["title"] for row in rows], titles[-2:])

    @patch("cninfo_download.time.sleep")
    @patch("cninfo_download.post_json")
    def test_all_pages_include_2016_annual_and_later_reports(self, post, sleep):
        def report(title, path):
            return {"announcementTitle": title, "adjunctUrl": path}
        post.side_effect = [
            {"totalAnnouncement": 61, "announcements": [
                report("2025年第三季度报告", "new.pdf"),
                report("2015年年度报告（修订版）", "old-correction.pdf")]},
            {"totalAnnouncement": 61, "announcements": [
                report("2017年第一季度报告", "2017-q1.pdf"),
                report("2016年年度报告摘要", "summary.pdf")]},
            {"totalAnnouncement": 61, "announcements": [
                report("2016 年年度报告（修订版）", "2016-annual.pdf"),
                report("2016年年报", "2016-short.pdf"),
                report("2016年第三季度报告", "2016-q3.pdf"),
                report("2016年半年度报告", "2016-half.pdf"),
                report("2015年年度报告", "2015.pdf"),
                report("无年份年度报告", "undated.pdf")]},
        ]
        rows = list(cninfo_pdf_urls("300750", "GD165627"))
        self.assertEqual([row["download_url"].rsplit("/", 1)[-1] for row in rows],
                         ["new.pdf", "2017-q1.pdf", "2016-annual.pdf", "2016-short.pdf"])
        self.assertEqual([call.args[1]["pageNum"] for call in post.call_args_list], [1, 2, 3])
        self.assertTrue(all(call.args[1]["seDate"].startswith("2016-01-01~")
                            for call in post.call_args_list))
        self.assertEqual(sleep.call_count, 2)

    @patch("cninfo_download.post_json")
    def test_explicit_org_and_shanghai_column(self, post):
        post.return_value = {"totalAnnouncement": 0, "announcements": []}
        self.assertEqual(list(cninfo_pdf_urls("600519", "org")), [])
        post.assert_called_once()
        self.assertEqual(post.call_args.args[1]["column"], "sse")

    @patch("cninfo_download.cninfo_pdf_urls")
    def test_cli_prints_titles_and_urls(self, reports):
        reports.return_value = [{"title": "报告", "download_url": "https://example.com/report.pdf"}]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["300750", "--urls-only", "--refresh"]), 0)
        self.assertEqual(output.getvalue(), "报告\thttps://example.com/report.pdf\n")

    @patch("cninfo_download.download_pdf")
    @patch("cninfo_download.time.sleep")
    @patch("cninfo_download.random.uniform", return_value=2.5)
    def test_delay_before_every_fetch_and_skip_existing_files(self, uniform, sleep, fetch):
        events = []
        output = io.StringIO()
        sleep.side_effect = lambda seconds: events.append("sleep")
        def write_pdf(url, destination, **kwargs):
            events.append("fetch")
            destination.write_bytes(b"PDF")
        fetch.side_effect = write_pdf
        reports = [{"title": title, "download_url": f"https://example.com/{index}.pdf"}
                   for index, title in enumerate(("已有报告", "2024年年度报告", "2025年第一季度报告"))]
        with tempfile.TemporaryDirectory() as root, redirect_stdout(output), redirect_stderr(io.StringIO()):
            directory = Path(root) / "300750"
            directory.mkdir()
            (directory / "已有报告.pdf").write_bytes(b"existing")
            self.assertEqual(download_reports("300750", reports, root), 0)
            self.assertEqual(fetch.call_args_list[0].args[1], directory / "2024年年度报告.pdf")
            self.assertEqual(fetch.call_args_list[1].args[1], directory / "2025年第一季度报告.pdf")
        self.assertEqual(events, ["sleep", "fetch", "sleep", "fetch"])
        self.assertIn("Network download request: NO\thttps://example.com/0.pdf", output.getvalue())
        self.assertIn("Network download request: YES\thttps://example.com/1.pdf", output.getvalue())
        self.assertIn("Network download request: YES\thttps://example.com/2.pdf", output.getvalue())
        self.assertEqual(uniform.call_count, 2)
        uniform.assert_called_with(1, 5)

    @patch("cninfo_download.time.sleep")
    @patch("cninfo_download.download_pdf", side_effect=[ValueError("Not a PDF"), None])
    def test_download_failure_does_not_stop_later_reports_or_overwrite_same_title(self, fetch, sleep):
        reports = [{"title": "2024年年度报告", "download_url": f"https://example.com/{index}.pdf"}
                   for index in range(2)]
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(download_reports("300750", reports, root, delay=(0, 0)), 1)
            first, second = (call.args[1] for call in fetch.call_args_list)
            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, Path(root) / "300750")
        self.assertEqual(sleep.call_count, 2)

    @patch("cninfo_download.post_json")
    def test_bad_code_fails_without_network(self, post):
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["abc"]), 1)
        self.assertIn("six digits", error.getvalue())
        post.assert_not_called()

    @patch("cninfo_download.time.sleep")
    @patch("cninfo_download.download_pdf")
    def test_log_retries_failure_then_skips_success_and_recovers_missing_file(self, fetch, sleep):
        report = {"title": "2024年年度报告", "download_url": "https://example.com/report.pdf"}
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            fetch.side_effect = ValueError("Incomplete PDF")
            self.assertEqual(download_reports("300750", [report], root), 1)
            log_path = Path(root) / "300750" / "download_reports.json"
            entry = json.loads(log_path.read_text())["downloads"][report["download_url"]]
            self.assertEqual((entry["status"], entry["attempts"], entry["error"]),
                             ("ERROR", 1, "Incomplete PDF"))
            self.assertIn("updated_at", entry)

            def write_pdf(url, destination, **kwargs):
                destination.write_bytes(b"%PDF-1.4\n%%EOF")
                return {"size_bytes": destination.stat().st_size}
            fetch.side_effect = write_pdf
            # Retry from the log even if the API no longer lists the report.
            self.assertEqual(download_reports("300750", [], root), 0)
            entry = json.loads(log_path.read_text())["downloads"][report["download_url"]]
            self.assertEqual((entry["status"], entry["attempts"]), ("DONE", 2))
            self.assertNotIn("error", entry)
            self.assertEqual(download_reports("300750", [report, report], root), 0)
            self.assertEqual(fetch.call_count, 2)
            (log_path.parent / entry["file"]).unlink()
            self.assertEqual(download_reports("300750", [report], root), 0)
            self.assertEqual(fetch.call_count, 3)

    @patch("cninfo_download.time.sleep")
    @patch("cninfo_download.download_pdf")
    def test_persistent_filenames_survive_reordered_duplicate_titles(self, fetch, sleep):
        reports = [{"title": "2024年年度报告", "download_url": f"https://example.com/{index}.pdf"}
                   for index in range(2)]
        fetch.side_effect = lambda url, destination, **kwargs: destination.write_bytes(b"PDF") and None
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(download_reports("300750", reports, root), 0)
            log_path = Path(root) / "300750" / "download_reports.json"
            original = json.loads(log_path.read_text())["downloads"]
            self.assertEqual(download_reports("300750", reversed(reports), root), 0)
            current = json.loads(log_path.read_text())["downloads"]
            self.assertEqual([current[url]["file"] for url in current], [original[url]["file"] for url in original])
            self.assertEqual(len({entry["file"] for entry in current.values()}), 2)
        self.assertEqual(fetch.call_count, 2)

    @patch("cninfo_download.time.sleep")
    @patch("cninfo_download.download_pdf")
    def test_interrupted_fetch_is_recorded_for_retry(self, fetch, sleep):
        report = {"title": "2024年年度报告", "download_url": "https://example.com/report.pdf"}
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            fetch.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                download_reports("300750", [report], root)
            log_path = Path(root) / "300750" / "download_reports.json"
            self.assertEqual(json.loads(log_path.read_text())["downloads"][report["download_url"]]["status"], "DOWNLOADING")
            fetch.side_effect = lambda url, destination, **kwargs: destination.write_bytes(b"PDF") and None
            self.assertEqual(download_reports("300750", [], root), 0)
        self.assertEqual(fetch.call_count, 2)

    @patch("cninfo_download.time.sleep")
    @patch("cninfo_download.download_pdf")
    def test_existing_pdf_hash_is_logged_and_enrollment_retries_without_fetch(self, fetch, sleep):
        report = {"title": "2024年年度报告", "download_url": "https://example.com/report.pdf"}
        content = b"%PDF-1.4\n%%EOF"
        expected_hash = hashlib.md5(content, usedforsecurity=False).hexdigest()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            directory = Path(root) / "300750"
            directory.mkdir()
            destination = directory / "2024年年度报告.pdf"
            destination.write_bytes(content)
            self.storage.store_downloaded_report.side_effect = RuntimeError("database unavailable")
            self.assertEqual(download_reports("300750", [report], root), 1)
            entry = json.loads((directory / "download_reports.json").read_text())["downloads"][report["download_url"]]
            self.assertEqual(entry["pdf_hash"], expected_hash)
            self.assertEqual(entry["local_path"], str(destination.resolve()))
            self.assertEqual((entry["status"], entry["error_stage"]), ("ERROR", "enrollment"))
            self.storage.store_downloaded_report.side_effect = None
            self.assertEqual(download_reports("300750", [], root), 0)
            self.storage.store_downloaded_report.assert_called_with(
                expected_hash, report["download_url"], str(destination.resolve()),
                stock_code="300750", reporting_date="2024-12-31", pdf_creation_date=None,
                download_report=ANY,
            )
            self.assertEqual(self.storage.store_downloaded_report.call_count, 2)
            entry = json.loads((directory / "download_reports.json").read_text())["downloads"][report["download_url"]]
            self.assertEqual(entry["status"], "DONE")
            self.assertNotIn("error", entry)
        fetch.assert_not_called()
        sleep.assert_not_called()

    @patch("cninfo_download.time.sleep")
    @patch("cninfo_download.download_pdf")
    def test_successful_fetch_is_enrolled_and_failed_fetch_is_not(self, fetch, sleep):
        reports = [{"title": f"2024年报告{index}", "download_url": f"https://example.com/{index}.pdf"}
                   for index in range(2)]
        def download(url, destination, **kwargs):
            if url.endswith("0.pdf"):
                raise ValueError("Not a PDF")
            destination.write_bytes(b"PDF")
        fetch.side_effect = download
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(download_reports("300750", reports, root), 1)
            self.storage.store_downloaded_report.assert_called_once_with(
                hashlib.md5(b"PDF", usedforsecurity=False).hexdigest(), reports[1]["download_url"],
                str((Path(root) / "300750" / "2024年报告1.pdf").resolve()),
                stock_code="300750", reporting_date="2024-12-31", pdf_creation_date=None,
                download_report=ANY)
