import tempfile
import unittest
from pathlib import Path

from financial_statement_line_items import FinancialStatementLineItemDict


class FinancialStatementLineItemDictTests(unittest.TestCase):
    def write_source(self, content: str) -> Path:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        source_path = Path(temporary_directory.name) / "line-items.txt"
        source_path.write_text(content, encoding="utf-8")
        return source_path

    def test_loads_items_and_ids(self) -> None:
        source_path = self.write_source(
            "101|资产|Assets\n"
            "\t101101|流动资产|Current Assets\n"
            "\t\t101101101|货币资金|Monetary Funds\n"
        )

        items = FinancialStatementLineItemDict(source_path)

        self.assertEqual(items.get_item_id("货币资金"), "101101101")
        self.assertEqual(items.item_ids["流动资产"], "101101")
        self.assertIn("资产", items)
        self.assertEqual(items["资产"].name, "资产")
        self.assertEqual(items["资产"].english_name, "Assets")

    def test_builds_parent_relationships_from_indentation(self) -> None:
        source_path = self.write_source(
            "101|资产|Assets\n"
            "\t101101|流动资产|Current Assets\n"
            "\t\t101101207|应收款项|Receivables\n"
            "\t\t\t101101207101|应收利息|Interest Receivable\n"
            "\t101102|非流动资产|Non-current Assets\n"
        )

        items = FinancialStatementLineItemDict(source_path)

        self.assertIsNone(items["资产"].parent)
        self.assertEqual(items["流动资产"].parent.name, "资产")
        self.assertEqual(items["应收款项"].parent.name, "流动资产")
        self.assertEqual(items["应收利息"].parent.name, "应收款项")
        self.assertEqual(items["非流动资产"].parent.name, "资产")

    def test_get_returns_none_and_get_item_id_rejects_unknown_name(self) -> None:
        source_path = self.write_source("101|资产|Assets\n")
        items = FinancialStatementLineItemDict(source_path)

        self.assertIsNone(items.get("不存在"))
        with self.assertRaisesRegex(KeyError, "Unknown financial statement"):
            items.get_item_id("不存在")

    def test_rejects_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            FinancialStatementLineItemDict("/tmp/does-not-exist-line-items.txt")

    def test_rejects_malformed_and_duplicate_definitions(self) -> None:
        malformed_path = self.write_source("资产\n")
        with self.assertRaisesRegex(ValueError, "Invalid line-item definition"):
            FinancialStatementLineItemDict(malformed_path)

        duplicate_path = self.write_source("101|资产|Assets\n102|资产|Assets\n")
        with self.assertRaisesRegex(ValueError, "Duplicate line-item name"):
            FinancialStatementLineItemDict(duplicate_path)

    def test_loads_default_source_file(self) -> None:
        items = FinancialStatementLineItemDict()

        self.assertEqual(items.get_item_id("货币资金"), "101101101")
        self.assertEqual(items["货币资金"].english_name, "Monetary Funds")
        self.assertEqual(items["应收利息"].parent.name, "其他应收款")


if __name__ == "__main__":
    unittest.main()
