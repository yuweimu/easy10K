import unittest

from report_metadata import extract_report_identifiers, match_stock_code


class StockCodeTests(unittest.TestCase):
    def test_a_share_labels(self):
        for label in ["A 股代码", "A股代码", "A 股 代码", "a 股代码", "**A 股代码**"]:
            with self.subTest(label=label):
                self.assertEqual(match_stock_code(f"{label}：002304"), "002304")

    def test_pdf_row_with_b_share_column(self):
        self.assertEqual(match_stock_code("A 股代码              002304                B 股代码"), "002304")

    def test_existing_labels(self):
        for label in ["证券代码", "公司代码", "股票代码"]:
            with self.subTest(label=label):
                self.assertEqual(match_stock_code(f"{label}：000568"), "000568")

    def test_not_an_a_share_stock_code(self):
        for text in ["B 股代码：200001", "A 股代码：00230", "A 股代码：0023041", "邮政代码：002304"]:
            with self.subTest(text=text):
                self.assertIsNone(match_stock_code(text))

    def test_markdown_identifier_extraction(self):
        self.assertEqual(
            extract_report_identifiers("A 股代码 | 002304\n1、合并资产负债表\n2012年6月30日"),
            ("002304", "2012-06-30"),
        )


if __name__ == "__main__":
    unittest.main()
