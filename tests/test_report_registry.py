import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


class ReportRegistryAPITests(unittest.TestCase):
    @patch("main.LineItemStorage")
    def test_response_serialization_and_pagination(self, storage_class):
        from datetime import date, datetime, timezone
        storage = storage_class.return_value.__enter__.return_value
        storage.get_report_registry.return_value = {
            "items": [{"reporting_date": date(2025, 12, 31), "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}],
            "page": 2, "page_size": 10, "total": 11, "total_pages": 2,
        }
        response = TestClient(main.app).get("/report-registry?page=2&page_size=10")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["reporting_date"], "2025-12-31")
        storage.get_report_registry.assert_called_once_with(2, 10)
        storage_class.return_value.__exit__.assert_called_once()

    @patch("main.LineItemStorage")
    def test_invalid_query_is_rejected_before_database_access(self, storage_class):
        client = TestClient(main.app)
        for query in ("page=0", "page_size=0", "page_size=101", "page=abc"):
            self.assertEqual(client.get(f"/report-registry?{query}").status_code, 422)
        storage_class.assert_not_called()

    @patch("main.LineItemStorage")
    def test_unconfigured_database_is_unavailable(self, storage_class):
        storage_class.return_value.__enter__.return_value.database_url = ""
        with self.assertRaises(HTTPException) as error:
            main.get_report_registry(1, 25)
        self.assertEqual(error.exception.status_code, 503)

    @patch("main.LineItemStorage")
    def test_database_failure_does_not_expose_details(self, storage_class):
        storage_class.return_value.__enter__.return_value.get_report_registry.side_effect = RuntimeError("private details")
        with self.assertLogs("main", level="WARNING"), self.assertRaises(HTTPException) as error:
            main.get_report_registry(1, 25)
        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn("private", error.exception.detail)


class RegistryFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        root_patch = patch.object(main, "PROJECT_DIR", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        storage_patch = patch("main.LineItemStorage")
        self.storage = storage_patch.start().return_value.__enter__.return_value
        self.addCleanup(storage_patch.stop)
        self.client = TestClient(main.app)

    def fetch(self, kind):
        return self.client.get(f"/report-registry/file/{kind}", params={"pdf_hash": "full-hash", "pdf_creation_time": "created"})

    def test_pdf_opens_inline(self):
        path = self.root / "reports/source.pdf"
        path.parent.mkdir()
        path.write_bytes(b"%PDF-1.4\nexample")
        self.storage.get_report_files.return_value = {"pdf_local_path": "reports/source.pdf"}
        response = self.fetch("pdf_local_path")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, path.read_bytes())
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertIn("application/pdf", response.headers["content-type"])
        self.storage.get_report_files.assert_called_once_with("full-hash", "created")

    def test_markdown_and_preprocessed_use_report_viewer(self):
        path = self.root / "reports/report.md"
        path.parent.mkdir()
        path.write_text("# Report content", encoding="utf-8")
        for kind in ("markdown_file", "preprocessed_file"):
            with self.subTest(kind=kind), patch("main.render_report", return_value=main.HTMLResponse("Rendered report")) as renderer:
                self.storage.get_report_files.return_value = {kind: str(path)}
                response = self.fetch(kind)
                self.assertEqual(response.status_code, 200)
                renderer.assert_called_once_with("# Report content", title="report.md")

    def test_missing_records_and_paths_are_not_found(self):
        for record in (None, {"markdown_file": None}, {"markdown_file": "reports/missing.md"}, {"markdown_file": "reports/file.html"}):
            with self.subTest(record=record):
                self.storage.get_report_files.return_value = record
                self.assertEqual(self.fetch("markdown_file").status_code, 404)

    def test_arbitrary_fields_and_path_escape_are_rejected(self):
        self.assertEqual(self.fetch("pdf_download_url").status_code, 422)
        for path in ("../secret.md", "reports/../../secret.md", str(self.root / "secret.md")):
            self.storage.get_report_files.return_value = {"markdown_file": path}
            self.assertEqual(self.fetch("markdown_file").status_code, 403)
        (self.root / "secret.md").write_text("secret", encoding="utf-8")
        (self.root / "reports").mkdir()
        (self.root / "reports/linked.md").symlink_to(self.root / "secret.md")
        self.storage.get_report_files.return_value = {"markdown_file": "reports/linked.md"}
        self.assertEqual(self.fetch("markdown_file").status_code, 403)
