from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

import main
from extract import extract_report
from report_sources import locate_source, normalized_bbox, render_page


class ReportSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "reports" / "sample.pdf"
        self.path.parent.mkdir()
        with pymupdf.open() as document:
            page = document.new_page(width=420, height=600)
            for y, text in ((45, "合并资产负债表"), (70, "2025年12月31日"), (95, "单位：万元")):
                page.insert_text((30, y), text, fontname="china-s", fontsize=12)
            for x in (30, 210, 300, 390):
                page.draw_line((x, 130), (x, 220))
            for y in (130, 160, 190, 220):
                page.draw_line((30, y), (390, y))
            for y, cells in ((150, ("项目", "期末余额", "期初余额")),
                             (180, ("货币资金", "2,500.00", "1,200.00")),
                             (210, ("其他应收款", "300.00", "200.00"))):
                for x, text in zip((40, 220, 310), cells):
                    page.insert_text((x, y), text, fontname="china-s", fontsize=10)
            document.save(self.path)
        self.pdf_hash = hashlib.md5(self.path.read_bytes(), usedforsecurity=False).hexdigest()
        self.extraction = asdict(extract_report(self.path))
        self.record = {"pdf_hash": self.pdf_hash, "local_path": str(self.path),
                       "download_url": "https://static.cninfo.com.cn/finalpage/sample.PDF",
                       "extraction_report": json.dumps(self.extraction),
                       "balance_sheet": "单位：万元", "amount": "25000000.00",
                       "report_creation_time": "created"}
        self.cache = self.root / "cache"

    def test_extraction_retains_page_and_normalized_row_boxes(self):
        rows = self.extraction["balance_sheet"]["source_rows"]
        row = next(row for row in rows if "货币资金" in row["cells"][0])
        self.assertEqual(row["page_number"], 1)
        self.assertTrue(all(0 <= value <= 1 for value in row["bbox"]))
        source = locate_source(self.record, 110101, self.path, self.cache)
        self.assertEqual(source["bbox"], row["bbox"])
        self.assertEqual(source["amount_unit"], "万元")
        self.assertEqual(source["statement_amount"], "2500.00")

    def test_legacy_source_rows_are_indexed_once(self):
        self.extraction["balance_sheet"].pop("source_rows")
        self.record["extraction_report"] = json.dumps(self.extraction)
        source = locate_source(self.record, 110101, self.path, self.cache)
        self.assertEqual(source["page_number"], 1)
        self.assertTrue((self.cache / self.pdf_hash / "balance-sheet-rows.json").is_file())
        with patch("report_sources.pymupdf.open", side_effect=AssertionError("Cache should avoid reparsing")):
            self.assertEqual(locate_source(self.record, 110101, self.path, self.cache), source)

    def test_missing_or_ambiguous_sources_never_guess(self):
        self.record["amount"] = "999999"
        with self.assertRaisesRegex(ValueError, "not uniquely located"):
            locate_source(self.record, 110101, self.path, self.cache)
        self.record["amount"] = "25000000.00"
        row = next(row for row in self.extraction["balance_sheet"]["source_rows"] if "货币资金" in row["cells"][0])
        self.extraction["balance_sheet"]["source_rows"].append({**row, "page_number": 2})
        self.record["extraction_report"] = json.dumps(self.extraction)
        with self.assertRaisesRegex(ValueError, "not uniquely located"):
            locate_source(self.record, 110101, self.path, self.cache)

    def test_page_and_excerpt_images_are_cached(self):
        full = render_page(self.path, self.pdf_hash, 1, self.cache)
        crop = render_page(self.path, self.pdf_hash, 1, self.cache, top=0.2, bottom=0.4)
        self.assertEqual(pymupdf.Pixmap(full).width, 960)
        self.assertLess(pymupdf.Pixmap(crop).height, pymupdf.Pixmap(full).height)
        with patch("report_sources.pymupdf.open", side_effect=AssertionError("Cached image should be reused")):
            self.assertEqual(render_page(self.path, self.pdf_hash, 1, self.cache), full)

    def test_hash_and_page_bounds_are_verified(self):
        with self.assertRaisesRegex(ValueError, "hash"):
            render_page(self.path, "0" * 32, 1, self.cache)
        with self.assertRaisesRegex(ValueError, "outside"):
            render_page(self.path, self.pdf_hash, 2, self.cache)

    def test_rotated_coordinates_match_displayed_page(self):
        with pymupdf.open(self.path) as document:
            page = document[0]
            page.set_rotation(90)
            bbox = normalized_bbox(page, (30, 160, 390, 190))
            expected = pymupdf.Rect(30, 160, 390, 190) * page.rotation_matrix
            self.assertAlmostEqual(bbox[0], expected.x0 / page.rect.width)
            self.assertAlmostEqual(bbox[3], expected.y1 / page.rect.height)

    def test_api_uses_exact_report_version_and_serves_only_images(self):
        with patch.object(main, "PROJECT_DIR", self.root), patch.object(main, "SOURCE_CACHE_DIR", self.cache), \
                patch("main.LineItemStorage") as factory:
            storage = factory.return_value.__enter__.return_value
            storage.get_balance_sheet_source.return_value = self.record
            storage.get_digestion_pdf_path.return_value = str(self.path)
            client = TestClient(main.app)
            response = client.get("/balance_sheet_items/300750/2025-12-31/110101/source",
                                  params={"report_creation_time": "created"})
            self.assertEqual(response.status_code, 200, response.text)
            storage.get_balance_sheet_source.assert_called_once_with("300750", "2025-12-31", 110101, "created")
            source = response.json()
            self.assertEqual(source["download_url"], self.record["download_url"])
            image = client.get(source["image_url"] + "&width=1600")
            self.assertEqual(image.status_code, 200, image.text[:100] if image.status_code != 200 else "")
            self.assertEqual(image.headers["content-type"], "image/png")
            self.assertIn("immutable", image.headers["cache-control"])
            self.assertTrue(image.content.startswith(b"\x89PNG"))
            self.assertEqual(client.get(f"/report-source/{self.pdf_hash}/page/1.png?width=999").status_code, 422)
            storage.get_digestion_pdf_path.return_value = str(self.root / "outside.pdf")
            self.assertEqual(client.get(source["page_image_url"]).status_code, 403)

    def test_api_unmatched_amount_returns_original_pdf_fallback(self):
        with patch.object(main, "PROJECT_DIR", self.root), patch("main.LineItemStorage") as factory:
            storage = factory.return_value.__enter__.return_value
            storage.get_balance_sheet_source.return_value = {**self.record, "amount": "99999"}
            response = TestClient(main.app).get("/balance_sheet_items/300750/2025-12-31/110101/source",
                                                params={"report_creation_time": "created"})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["pdf_hash"], self.pdf_hash)
            self.assertEqual(response.json()["detail"]["download_url"], self.record["download_url"])

    def test_original_link_survives_missing_local_pdf_and_rejects_unsafe_urls(self):
        with patch.object(main, "PROJECT_DIR", self.root), patch("main.LineItemStorage") as factory:
            storage = factory.return_value.__enter__.return_value
            client = TestClient(main.app)
            storage.get_balance_sheet_source.return_value = {**self.record, "local_path": None}
            response = client.get("/balance_sheet_items/300750/2025-12-31/110101/source",
                                  params={"report_creation_time": "created"})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["detail"]["download_url"], self.record["download_url"])
            storage.get_balance_sheet_source.return_value = {**self.record, "download_url": "javascript:alert(1)"}
            response = client.get("/balance_sheet_items/300750/2025-12-31/110101/source",
                                  params={"report_creation_time": "created"})
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["download_url"])
