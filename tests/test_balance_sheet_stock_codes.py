import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class BalanceSheetStockCodesTests(unittest.TestCase):
    @patch("main.LineItemStorage")
    def test_other_statement_directories_and_items_are_separate(self, storage_class):
        storage = storage_class.return_value.__enter__.return_value
        client = TestClient(main.app)
        for statement in ("income_statement", "cash_flow_statement"):
            storage.get_statement_stock_codes.return_value = [{"stock_code": "300750", "report_count": 2}]
            storage.get_statement_items.return_value = [{"reporting_date": "2025-12-31", "item_id": 5901,
                                                        "amount": "0.1256", "report_creation_time": "created", "amount_unit": "元/股"}]
            response = client.get(f"/{statement}_items")
            self.assertEqual(response.status_code, 200)
            storage.get_statement_stock_codes.assert_called_with(statement)
            response = client.get(f"/{statement}_items/300750")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()[0]["amount_unit"], "元/股")
            storage.get_statement_items.assert_called_with(statement, "300750")
        storage.get_balance_sheet_items.assert_not_called()

    @patch("main.LineItemStorage")
    def test_other_statement_database_errors(self, storage_class):
        storage = storage_class.return_value.__enter__.return_value
        client = TestClient(main.app)
        for statement in ("income_statement", "cash_flow_statement"):
            for suffix, method in (("", storage.get_statement_stock_codes), ("/300750", storage.get_statement_items)):
                storage.database_url = ""
                self.assertEqual(client.get(f"/{statement}_items{suffix}").status_code, 503)
                storage.database_url = "postgresql://test"
                method.side_effect = RuntimeError("private details")
                with self.assertLogs("main", level="WARNING"):
                    response = client.get(f"/{statement}_items{suffix}")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("private details", response.text)

    @patch("main.LineItemStorage")
    def test_directory_returns_codes_and_empty_list(self, storage_class):
        storage = storage_class.return_value.__enter__.return_value
        client = TestClient(main.app)
        for codes in ([{"stock_code": "000001", "report_count": 3, "stock_name": "平安银行", "has_missing_reporting_dates": True},
                       {"stock_code": "300750", "report_count": 7, "stock_name": None, "has_missing_reporting_dates": False}], []):
            with self.subTest(codes=codes):
                storage.get_balance_sheet_stock_codes.return_value = codes
                response = client.get("/balance_sheet_items")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), codes)
        storage.get_balance_sheet_items.assert_not_called()

    @patch("main.LineItemStorage")
    def test_unavailable_database_returns_service_error(self, storage_class):
        storage = storage_class.return_value.__enter__.return_value
        client = TestClient(main.app)
        storage.database_url = ""
        self.assertEqual(client.get("/balance_sheet_items").status_code, 503)
        storage.get_balance_sheet_stock_codes.assert_not_called()
        storage.database_url = "postgresql://test"
        storage.get_balance_sheet_stock_codes.side_effect = RuntimeError("private details")
        with self.assertLogs("main", level="WARNING"):
            response = client.get("/balance_sheet_items")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private details", response.text)
