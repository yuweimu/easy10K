import unittest

from report_metadata import extract_report_identifiers, match_filename_reporting_date, match_stock_code


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

    def test_skips_lines_with_more_than_28_nonempty_characters(self):
        self.assertIsNone(match_stock_code("公司股票代码：002304这是一个超过长度限制的长文本内容"))

    def test_markdown_identifier_extraction(self):
        self.assertEqual(
            extract_report_identifiers("A 股代码 | 002304\n1、合并资产负债表\n2012年6月30日"),
            ("002304", "2012-06-30"),
        )

class FilenameReportingDateTests(unittest.TestCase):
    def test_report_filename_patterns(self):
        for filename, expected in (
            ("**2023 **年半年度报告.pdf", "2023-06-30"),
            ("2024 年第一季度报告.pdf", "2024-03-31"),
            ("2024年一季度报告.pdf", "2024-03-31"),
            ("2024年一季报.pdf", "2024-03-31"),
            ("2017 年年度报告.pdf", "2017-12-31"),
            ("2019 年第三季度报告.pdf", "2019-09-30"),
            ("2024三季报.pdf", "2024-09-30"),
            ("2024年报.pdf", "2024-12-31"),
        ):
            with self.subTest(filename=filename):
                self.assertEqual(match_filename_reporting_date(filename), expected)


if __name__ == "__main__":
    unittest.main()
