import unittest
import argparse
from contextlib import redirect_stderr
import io
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import runpy
import tempfile
from unittest.mock import MagicMock, patch

import pdf_download
from pdf_download import (BASE_URL, REPORT_PAGES, decode_html, fetch_page_links,
                          fetch_report_links, parse_datelist, parse_download_link,
                          fetch_download_link, fetch_report_download_links,
                          download_pdf, download_reports, report_filename, parse_delay, main,
                          record_download)


class PDFDownloadTests(unittest.TestCase):
    def setUp(self):
        # Unit tests must not connect to a DATABASE_URL from the developer's .env.
        patcher = patch("pdf_download.record_download", return_value="test-hash")
        self.record = patcher.start()
        self.addCleanup(patcher.stop)

    def test_startup_loads_project_dotenv(self):
        def load_environment(path, *, override):
            os.environ.setdefault("DATABASE_URL", "postgresql://dotenv-test")
            return True

        with patch.dict(os.environ, {}, clear=True), patch(
            "dotenv.load_dotenv", side_effect=load_environment,
        ) as load:
            module = runpy.run_path(pdf_download.__file__)
            load.assert_called_once_with(
                Path(pdf_download.__file__).resolve().with_name(".env"), override=False,
            )
            self.assertEqual(module["LineItemStorage"]().database_url, "postgresql://dotenv-test")

    def test_startup_preserves_shell_database_url(self):
        from dotenv import load_dotenv as real_load_dotenv

        def load_environment(path, *, override):
            return real_load_dotenv(stream=io.StringIO("DATABASE_URL=postgresql://dotenv-test\n"),
                                    override=override)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://shell-test"}), patch(
            "dotenv.load_dotenv", side_effect=load_environment,
        ):
            module = runpy.run_path(pdf_download.__file__)
            self.assertEqual(module["LineItemStorage"]().database_url, "postgresql://shell-test")

    def test_record_completed_pdf_hash_url_and_path(self):
        with tempfile.TemporaryDirectory() as directory, patch("pdf_download.LineItemStorage") as storage:
            destination = Path(directory) / "report.pdf"
            content = b"%PDF-1.7\ncomplete\n%%EOF"
            destination.write_bytes(content)
            digest = record_download(destination, "https://files.example/report.pdf")
            self.assertEqual(digest, hashlib.md5(content, usedforsecurity=False).hexdigest())
            storage.return_value.__enter__.return_value.store_downloaded_report.assert_called_once_with(
                digest, "https://files.example/report.pdf", str(destination.resolve()),
            )

    def test_registration_failure_logged_and_existing_file_can_retry(self):
        link = {"title": "report", "url": BASE_URL, "download_url": "https://files.example/report.pdf"}
        def download(url, destination, timeout, referer):
            destination.write_bytes(b"complete PDF")
            return {}
        self.record.side_effect = [RuntimeError("database unavailable"), "recovered-hash"]
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch("pdf_download.download_pdf", side_effect=download) as fetch:
            first = download_reports("600519", {"annual": [link]}, Path(directory))
            self.assertEqual(first["downloads"][-1]["status"], "ERROR")
            self.assertIn("database unavailable", first["downloads"][-1]["error"])
            second = download_reports("600519", {"annual": [link]}, Path(directory))
            self.assertEqual(second["downloads"][-1]["status"], "SKIPPED")
            self.assertEqual(second["downloads"][-1]["pdf_hash"], "recovered-hash")
            fetch.assert_called_once()

    def test_failed_download_is_not_registered(self):
        link = {"title": "report", "url": BASE_URL, "download_url": "https://files.example/report.pdf"}
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch("pdf_download.download_pdf", side_effect=TimeoutError("timeout")):
            result = download_reports("600519", {"annual": [link]}, Path(directory))
        self.assertEqual(result["downloads"][0]["status"], "ERROR")
        self.record.assert_not_called()

    def test_parse_delay(self):
        for value, expected in [("1,5", (1, 5)), ("2.5,8", (2.5, 8)), ("0,0", (0, 0)), ("3,3", (3, 3)), (" 1 , 5 ", (1, 5))]:
            with self.subTest(value=value):
                self.assertEqual(parse_delay(value), expected)
        for value in ["1", "1,2,3", "x,5", "5,1", "-1,5", "nan,5", "1,inf", ",5"]:
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                parse_delay(value)

    def test_default_download_root(self):
        self.assertEqual(pdf_download.DEFAULT_REPORTS_DIR,
                         Path(pdf_download.__file__).resolve().parent / "reports" / "download")
        with tempfile.TemporaryDirectory() as directory, patch(
            "pdf_download.DEFAULT_REPORTS_DIR", Path(directory) / "reports" / "download",
        ):
            download_reports("600519", {})
            self.assertTrue((Path(directory) / "reports/download/600519/downloads.json").is_file())

    def test_download_cli_delay_default_and_custom(self):
        for args, expected in [([], (1, 5)), (["--delay", "2.5,8"], (2.5, 8))]:
            with self.subTest(args=args), patch("pdf_download.fetch_report_download_links", return_value={}), patch("pdf_download.download_reports", return_value={}) as download, patch("builtins.print"):
                main(["600519", *args])
                self.assertEqual(download.call_args.kwargs, {"delay_min": expected[0], "delay_max": expected[1]})
        with patch("pdf_download.fetch_report_download_links") as fetch, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["600519", "--delay", "5,1"])
            self.assertEqual(error.exception.code, 2)
            fetch.assert_not_called()

    def test_missing_download_anchor_is_an_expected_skip(self):
        listings = {"annual": [{"title": "浙江鼎力报告", "url": BASE_URL + "/missing"}]}
        with patch("pdf_download.fetch_report_links", return_value=listings), patch("pdf_download.fetch_html", return_value=("<p>公告内容详见附件</p>", BASE_URL + "/missing")), redirect_stderr(io.StringIO()):
            result = fetch_report_download_links("603338")
        entry = result["annual"][0]
        self.assertEqual(entry["skip_reason"], "NO_DOWNLOAD_LINK")
        self.assertNotIn("error", entry)
        with tempfile.TemporaryDirectory() as directory, patch("pdf_download.download_pdf") as download:
            log = download_reports("603338", result, Path(directory))
        download.assert_not_called()
        self.assertEqual(log["downloads"][0]["status"], "SKIPPED")

    def test_interrupted_download_never_publishes_partial_file(self):
        errors = [TimeoutError("timeout"), ConnectionResetError("connection reset"),
                  IncompleteRead(b"partial", 100), KeyboardInterrupt(), SystemExit()]
        for error in errors:
            for existing in [False, True]:
                with self.subTest(error=type(error).__name__, existing=existing), tempfile.TemporaryDirectory() as directory, patch("pdf_download.urlopen") as open_url:
                    response = MagicMock()
                    response.read.side_effect = [b"%PDF-1.7\npartial content", error]
                    open_url.return_value.__enter__.return_value = response
                    destination = Path(directory) / "report.pdf"
                    if existing:
                        destination.write_bytes(b"original PDF")
                    with self.assertRaises(type(error)):
                        download_pdf("https://files.example/report.pdf", destination)
                    if existing:
                        self.assertEqual(destination.read_bytes(), b"original PDF")
                    else:
                        self.assertFalse(destination.exists())
                    self.assertEqual(list(Path(directory).glob(".*.part")), [])

    def test_silent_truncation_without_content_length_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch("pdf_download.urlopen") as open_url:
            response = MagicMock()
            response.read.side_effect = [b"%PDF-1.7\ntruncated", b""]
            response.headers.get.return_value = None
            open_url.return_value.__enter__.return_value = response
            destination = Path(directory) / "report.pdf"
            with self.assertRaisesRegex(ValueError, "missing final %%EOF"):
                download_pdf("https://files.example/report.pdf", destination)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_final_pdf_absent_until_download_completes(self):
        content = b"%PDF-1.7\ncomplete content\n%%EOF\r\n"
        with tempfile.TemporaryDirectory() as directory, patch("pdf_download.urlopen") as open_url:
            destination = Path(directory) / "report.pdf"
            chunks = iter([content[:15], content[15:], b""])
            def read(size):
                self.assertFalse(destination.exists())
                return next(chunks)
            response = MagicMock()
            response.read.side_effect = read
            response.headers.get.return_value = None
            open_url.return_value.__enter__.return_value = response
            download_pdf("https://files.example/report.pdf", destination)
            self.assertEqual(destination.read_bytes(), content)

    def test_title_filename_preserves_chinese(self):
        self.assertEqual(report_filename("贵州茅台2020年第三季度报告"), "贵州茅台2020年第三季度报告.pdf")
        self.assertEqual(report_filename("报告.PDF"), "报告.pdf")
        self.assertEqual(report_filename('../报告:测试/附件\\一?*'), "_报告_测试_附件_一__.pdf")
        self.assertLessEqual(len(report_filename("报告" * 100).encode("utf-8")), 184)
        self.assertEqual(report_filename("..."), "report.pdf")

    def test_same_title_different_urls_are_saved_separately(self):
        links = [{"title": "贵州茅台2020年第三季度报告", "url": BASE_URL,
                  "download_url": f"https://files.example/{i}.pdf"} for i in [1, 2]]
        def download(url, destination, timeout, referer):
            destination.write_bytes(url.encode())
            return {}
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch("pdf_download.download_pdf", side_effect=download) as fetch, patch("pdf_download.time.sleep"):
            result = download_reports("600519", {"third_quarter": links}, Path(directory))
            names = [entry["file"] for entry in result["downloads"]]
            self.assertEqual(names[0], "贵州茅台2020年第三季度报告.pdf")
            self.assertNotEqual(names[0], names[1])
            self.assertTrue(all((Path(directory) / "600519" / name).exists() for name in names))
            repeated = download_reports("600519", {"third_quarter": links}, Path(directory))
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual([entry["file"] for entry in repeated["downloads"][-2:]], names)

    def test_download_pdf_streams_and_publishes_complete_file(self):
        content = b"%PDF-1.7\nexample\n%%EOF"
        response = MagicMock()
        response.read.side_effect = [content[:10], content[10:], b""]
        response.headers.get.return_value = str(len(content))
        response.geturl.return_value = "https://files.example/final.pdf"
        with tempfile.TemporaryDirectory() as directory, patch("pdf_download.urlopen") as open_url:
            open_url.return_value.__enter__.return_value = response
            destination = Path(directory) / "report.pdf"
            info = download_pdf("https://files.example/report.pdf", destination)
            self.assertEqual(destination.read_bytes(), content)
            self.assertEqual(info["size_bytes"], len(content))
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_failed_download_cleans_partial_and_preserves_existing_file(self):
        for data, length in [(b"<html>blocked</html>", None), (b"%PDF-short", "1000")]:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory, patch("pdf_download.urlopen") as open_url:
                response = MagicMock()
                response.read.side_effect = [data, b""]
                response.headers.get.return_value = length
                open_url.return_value.__enter__.return_value = response
                destination = Path(directory) / "existing.pdf"
                destination.write_bytes(b"original")
                with self.assertRaises(ValueError):
                    download_pdf("https://files.example/report.pdf", destination)
                self.assertEqual(destination.read_bytes(), b"original")
                self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_serial_downloads_delay_log_and_skip_on_rerun(self):
        listings = {"annual": [
            {"title": "年度报告", "url": BASE_URL + "/a", "download_url": "https://files.example/a.pdf"},
            {"title": "旧年度报告", "url": BASE_URL + "/b", "download_url": "https://files.example/b.pdf"},
        ]}
        events = []
        def download(url, destination, timeout, referer):
            events.append(url)
            destination.write_bytes(b"%PDF-test")
            return {"size_bytes": 9, "resolved_url": url}
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch("pdf_download.download_pdf", side_effect=download) as fetch, patch("pdf_download.random.uniform", return_value=3) as uniform, patch("pdf_download.time.sleep", side_effect=lambda delay: events.append(delay)) as sleep:
            result = download_reports("600519", listings, Path(directory))
            self.assertEqual(events, ["https://files.example/a.pdf", 3, "https://files.example/b.pdf"])
            uniform.assert_called_once_with(1, 5)
            self.assertEqual([entry["status"] for entry in result["downloads"]], ["DONE", "DONE"])
            self.assertTrue(all(entry["downloaded_at"].endswith("+00:00") for entry in result["downloads"]))
            log = Path(directory) / "600519/downloads.json"
            self.assertEqual(json.loads(log.read_text()), result)
            result = download_reports("600519", listings, Path(directory))
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once_with(3)
            self.assertEqual([entry["status"] for entry in result["downloads"][-2:]], ["SKIPPED", "SKIPPED"])

    def test_failed_file_logged_and_next_attempt_delayed(self):
        link = {"title": "报告", "url": BASE_URL, "download_url": "https://files.example/a.pdf"}
        other = {**link, "download_url": "https://files.example/b.pdf"}
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch("pdf_download.download_pdf", side_effect=[TimeoutError("timeout"), {"size_bytes": 5}]), patch("pdf_download.time.sleep") as sleep:
            result = download_reports("600519", {"annual": [link, other]}, Path(directory))
            self.assertEqual([entry["status"] for entry in result["downloads"]], ["ERROR", "DONE"])
            self.assertIn("timeout", result["downloads"][0]["error"])
            self.assertEqual(sleep.call_count, 1)
            self.assertTrue(1 <= sleep.call_args.args[0] <= 5)

    def test_custom_delay_range(self):
        links = [{"title": f"报告{i}", "url": BASE_URL, "download_url": f"https://files.example/{i}.pdf"} for i in [1, 2]]
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch("pdf_download.download_pdf", return_value={}), patch("pdf_download.random.uniform", return_value=12.5) as uniform, patch("pdf_download.time.sleep") as sleep:
            download_reports("600519", {"annual": links}, Path(directory), delay_min=10, delay_max=20)
            uniform.assert_called_once_with(10, 20)
            sleep.assert_called_once_with(12.5)

    def test_invalid_delay_ranges_rejected_before_network_or_directory_creation(self):
        with tempfile.TemporaryDirectory() as directory, patch("pdf_download.download_pdf") as download:
            for low, high in [(-1, 5), (6, 5), (float("nan"), 5), (1, float("inf"))]:
                with self.subTest(low=low, high=high), self.assertRaisesRegex(ValueError, "delay range"):
                    download_reports("600519", {}, Path(directory), delay_min=low, delay_max=high)
            download.assert_not_called()
            self.assertFalse((Path(directory) / "600519").exists())

    def test_duplicate_urls_and_english_versions_are_not_downloaded(self):
        link = {"title": "报告", "url": BASE_URL, "download_url": "https://files.example/a.pdf"}
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch("pdf_download.download_pdf", return_value={}) as fetch, patch("pdf_download.time.sleep") as sleep:
            excluded = [{**link, "title": title, "download_url": f"https://files.example/excluded{i}.pdf"}
                        for i, title in enumerate(["报告(英文版)", "年度报告（H股）", "H 股年度报告", "h股报告", "年度报告（港股）", "港 股报告"])]
            result = download_reports("600519", {"annual": [link, link, *excluded]}, Path(directory))
            self.assertEqual(len(result["downloads"]), 1)
            fetch.assert_called_once()
            sleep.assert_not_called()

    def test_empty_download_log_and_invalid_stock_code(self):
        with tempfile.TemporaryDirectory() as directory:
            result = download_reports("000568", {}, Path(directory))
            self.assertEqual(result["downloads"], [])
            self.assertTrue((Path(directory) / "000568/downloads.json").exists())
            with self.assertRaises(ValueError):
                download_reports("../bad", {}, Path(directory))

    def test_download_anchor_text_and_relative_url(self):
        html = '<a href="/wrong">公告正文</a><a href="//files.example/report.pdf?a=1&amp;b=2"><b>下载</b> 公告</a>'
        self.assertEqual(parse_download_link(html, BASE_URL), "https://files.example/report.pdf?a=1&b=2")
        self.assertEqual(parse_download_link('<a href="report.pdf">下载公告</a>', BASE_URL + "/detail/1"), BASE_URL + "/detail/report.pdf")

    def test_missing_or_unsafe_download_link(self):
        for html in ['<a href="/other">其他</a>', '<a href="javascript:bad()">下载公告</a>']:
            with self.subTest(html=html), self.assertRaisesRegex(ValueError, "No 下载公告"):
                parse_download_link(html, BASE_URL)
        with patch("pdf_download.fetch_html", return_value=("no link", BASE_URL)):
            with self.assertRaisesRegex(RuntimeError, "Failed to parse"):
                fetch_download_link(BASE_URL)

    def test_resolve_downloads_skips_english_and_deduplicates(self):
        listings = {
            "annual": [{"title": "年度报告(英文版)", "url": BASE_URL + "/en"},
                       {"title": "年度报告（H股）", "url": BASE_URL + "/h"},
                       {"title": "年度报告（港股）", "url": BASE_URL + "/hk"},
                       {"title": "H 股年度报告", "url": BASE_URL + "/h-spaced"},
                       {"title": "年度报告", "url": BASE_URL + "/cn"}],
            "half_year": [{"title": "另一份公告", "url": BASE_URL + "/cn"}],
            "first_quarter": [], "third_quarter": [],
        }
        with patch("pdf_download.fetch_report_links", return_value=listings), patch("pdf_download.fetch_download_link", return_value="https://files.example/report.pdf") as fetch:
            result = fetch_report_download_links("600519", 8)
        fetch.assert_called_once_with(BASE_URL + "/cn", 8)
        self.assertEqual(len(result["annual"]), 1)
        self.assertEqual(result["annual"][0]["download_url"], "https://files.example/report.pdf")
        self.assertEqual(result["annual"][0]["url"], BASE_URL + "/cn")
        self.assertEqual(result["first_quarter"], [])

    def test_detail_failure_is_reported_without_aborting_other_links(self):
        listings = {"annual": [{"title": "缺失附件", "url": BASE_URL + "/missing"},
                               {"title": "年度报告", "url": BASE_URL + "/valid"}]}
        with patch("pdf_download.fetch_report_links", return_value=listings), patch("pdf_download.fetch_download_link", side_effect=[RuntimeError("missing download"), "https://files.example/report.pdf"]), redirect_stderr(io.StringIO()) as warnings:
            result = fetch_report_download_links("600519")
        self.assertEqual(result["annual"][0]["download_url"], "")
        self.assertIn("missing download", result["annual"][0]["error"])
        self.assertEqual(result["annual"][1]["download_url"], "https://files.example/report.pdf")
        self.assertIn("Warning", warnings.getvalue())

    def test_missing_download_url_logged_without_request_or_sleep(self):
        links = [{"title": "附件缺失的报告摘要", "url": BASE_URL + "/missing",
                  "download_url": "", "error": "No 下载公告 link found"},
                 {"title": "年度报告", "url": BASE_URL + "/valid",
                  "download_url": "https://files.example/report.pdf"}]
        with tempfile.TemporaryDirectory() as directory, patch("pdf_download.download_pdf", return_value={}) as fetch, patch("pdf_download.time.sleep") as sleep, redirect_stderr(io.StringIO()):
            result = download_reports("601318", {"annual": links}, Path(directory))
            self.assertEqual([entry["status"] for entry in result["downloads"]], ["SKIPPED", "DONE"])
            self.assertEqual(result["downloads"][0]["skip_reason"], "NO_DOWNLOAD_LINK")
            self.assertNotIn("file", result["downloads"][0])
            fetch.assert_called_once()
            sleep.assert_not_called()

    def test_only_datelist_anchors_including_nested_divs(self):
        html = '''<a href="/outside">outside</a><div class="other datelist">
        <a href="/report?id=1&amp;x=2"><b>年度报告</b> &amp; 附件</a>
        <div><a href="//example.com/two">two</a><br></div>
        <a href="three">three</a></div><a href="/outside2">outside</a>
        <div class="datelist"><a href="https://example.com/four">four</a></div>'''
        links = parse_datelist(html, BASE_URL + "/corp/list")
        self.assertEqual([link["url"] for link in links],
                         [BASE_URL + "/report?id=1&x=2", "https://example.com/two",
                          BASE_URL + "/corp/three", "https://example.com/four"])
        self.assertEqual(links[0]["title"], "年度报告 & 附件")

    def test_empty_container_invalid_links_and_missing_container(self):
        self.assertEqual(parse_datelist('<div class="datelist"></div>', BASE_URL), [])
        self.assertEqual(parse_datelist('<div class="datelist"><a>none</a><a href="">empty</a><a href="javascript:void(0)">js</a></div>', BASE_URL), [])
        with self.assertRaisesRegex(ValueError, "No div.datelist"):
            parse_datelist('<div class="not-datelist"></div>', BASE_URL)

    def test_decoding(self):
        for encoding in ["gb2312", "gbk", "utf-8"]:
            text = f'<meta charset="{encoding}"><div>年度报告</div>'
            self.assertEqual(decode_html(text.encode(encoding)), text)
        self.assertEqual(decode_html("年度报告".encode("gb18030"), "gb2312"), "年度报告")

    def test_fetch_uses_encoding_redirect_and_timeout(self):
        response = MagicMock()
        response.read.return_value = '<div class="datelist"><a href="report">年度报告</a></div>'.encode("gb2312")
        response.headers.get_content_charset.return_value = "gb2312"
        response.geturl.return_value = BASE_URL + "/redirect/list"
        with patch("pdf_download.urlopen") as open_url:
            open_url.return_value.__enter__.return_value = response
            links = fetch_page_links(BASE_URL, 8)
            self.assertEqual(open_url.call_args.kwargs["timeout"], 8)
        self.assertEqual(links, [{"title": "年度报告", "url": BASE_URL + "/redirect/report"}])

    def test_four_pages_and_leading_zero_stock_code(self):
        with patch("pdf_download.fetch_page_links", return_value=[]) as fetch:
            result = fetch_report_links("000568")
        self.assertEqual(list(result), list(REPORT_PAGES))
        self.assertEqual(fetch.call_count, 4)
        for endpoint, page_type in REPORT_PAGES.values():
            fetch.assert_any_call(f"{BASE_URL}/corp/go.php/{endpoint}/stockid/000568/page_type/{page_type}.phtml", 30)

    def test_invalid_input_does_not_fetch(self):
        with patch("pdf_download.fetch_page_links") as fetch:
            for code in [600519, "60051", "6005197", "../600519", "６００５１９"]:
                with self.subTest(code=code), self.assertRaises(ValueError):
                    fetch_report_links(code)
            for timeout in [0, -1, float("inf"), float("nan")]:
                with self.assertRaises(ValueError):
                    fetch_report_links("600519", timeout)
            fetch.assert_not_called()

    def test_network_failure_is_explicit(self):
        with patch("pdf_download.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(RuntimeError, "Failed to fetch or parse"):
                fetch_page_links(BASE_URL)


if __name__ == "__main__":
    unittest.main()
