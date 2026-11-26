import copy
from contextlib import redirect_stdout
from decimal import Decimal
import io
import json
import unittest
from unittest.mock import patch

from digest_bs import REQUIRED_IDS, check_report, main, parse_amount
from line_item_chart import LineItemChart


class DigestLineItemsTests(unittest.TestCase):
    def test_cli_ex_uses_external_balance_sheet(self):
        with patch("digest_bs.LineItemStorage") as factory, redirect_stdout(io.StringIO()):
            storage = factory.return_value.__enter__.return_value
            storage.get_audit_pass_reports.return_value = [self.row()]
            self.assertEqual(main(["--ex"]), 0)
            storage.get_audit_pass_reports.assert_called_once_with(pdf_hash=None, retry=False, ex=True)
            self.assertTrue(storage.store_line_item_digestion.call_args.kwargs["ex"])

    def setUp(self):
        self.chart = LineItemChart()
        self.values = {11: "300", 1101: "100", 110101: "40", 110128: "10", 110150: "50",
                       1102: "200", 110231: "180", 110242: "10", 110266: "10",
                       21: "120", 2101: "60", 210101: "60",
                       2102: "60", 210201: "60", 31: "180", 311001: "100", 311003: "10",
                       311004: "10", 311007: "5", 311008: "60", 311009: "5", 3110: "170", 3111: "10"}
        self.names = {item.item_id: name for name, item in self.chart.items.items()}

    def row(self, *, values=None, unit="元", notes=False):
        values = self.values if values is None else values
        lines = ["# 合并资产负债表", f"单位：{unit}",
                 "| 项目 | 附注 | 期末余额 | 期初余额 |" if notes else "| 项目 | 期末余额 | 期初余额 |",
                 "| --- | --- | --- | --- |" if notes else "| --- | --- | --- |"]
        for item_id, amount in values.items():
            cells = [self.names[item_id]] + (["六、1"] if notes else []) + [amount, "999999"]
            lines.append("| " + " | ".join(cells) + " |")
        return {"pdf_hash": "hash", "balance_sheet": "\n".join(lines), "stock_code": "600519",
                "reporting_date": "2025-12-31", "pdf_creation_date": "20260313165727"}

    def test_valid_report_checks_first_column_and_equity_subtotals(self):
        for notes in (False, True):
            with self.subTest(notes=notes):
                result = check_report(self.row(notes=notes), self.chart)
                self.assertTrue(result["passed"], result["issues"])
                self.assertEqual(result["unmapped_rows"], [])
                self.assertEqual(len(result["items"]), len(self.values))
                totals = {check["item_id"]: check for check in result["checks"] if check["check"] == "parent_sum"}
                self.assertEqual(totals[31]["children"], [3110, 3111])
                self.assertEqual(totals[3110]["children_sum"], "170")
                self.assertEqual(totals[3110]["children"], [311001, 311003, 311004, 311007, 311008, 311009])
                self.assertEqual(totals[11]["children_sum"], "300")
                json.dumps(result)

    def test_each_required_item_must_be_present_and_nonzero(self):
        for item_id in REQUIRED_IDS:
            for value in (None, "", "0", "—"):
                with self.subTest(item_id=item_id, value=value):
                    values = dict(self.values)
                    if value is None:
                        del values[item_id]
                    else:
                        values[item_id] = value
                    result = check_report(self.row(values=values), self.chart)
                    self.assertFalse(result["passed"])
                    summary = next(check for check in result["checks"] if check["check"] == "required_nonzero")
                    self.assertIn(item_id, summary["failed_item_ids"])
                    self.assertFalse(summary["passed"])

    def test_required_nonzero_checks_are_summarized_once(self):
        with redirect_stdout(io.StringIO()):
            valid = check_report(self.row(), self.chart)
            invalid = check_report(self.row(values={**self.values, 110101: "0", 110128: ""}), self.chart)
        for result, failed_ids in ((valid, []), (invalid, [110101, 110128])):
            summaries = [check for check in result["checks"] if check["check"] == "required_nonzero"]
            self.assertEqual(summaries, [{"check": "required_nonzero", "checked": len(REQUIRED_IDS),
                                          "passed": not failed_ids, "failed_item_ids": failed_ids}])
            required_issues = [issue for issue in result["issues"] if issue.startswith("Required items")]
            self.assertEqual(len(required_issues), int(bool(failed_ids)))
            self.assertTrue(any(check["check"] == "parent_sum" for check in result["checks"]))

    def test_parent_mismatch_fails_and_tolerance_is_recorded(self):
        values = dict(self.values)
        values[11] = "301"
        result = check_report(self.row(values=values), self.chart)
        self.assertFalse(result["passed"])
        check = next(c for c in result["checks"] if c["check"] == "parent_sum" and c["item_id"] == 11)
        self.assertEqual(check["difference"], "1")
        self.assertFalse(check["passed"])
        values[11] = "300.01"
        self.assertTrue(check_report(self.row(values=values), self.chart)["passed"])
        self.assertTrue(check_report(self.row(values={**values, 11: "301"}), self.chart,
                                     absolute_tolerance=Decimal("1"))["passed"])

    def test_parent_comparisons_are_stored_without_stdout_noise(self):
        output = io.StringIO()
        with redirect_stdout(output):
            passed = check_report(self.row(), self.chart)
            failed = check_report(self.row(values={**self.values, 11: "301"}), self.chart)
        self.assertIn("FAIL", output.getvalue())
        self.assertTrue(any(check["check"] == "parent_sum" and check["item_id"] == 11
                            for check in passed["checks"]))
        self.assertFalse(failed["passed"])
        self.assertTrue(any(check.get("item_id") == 11 and not check["passed"]
                            for check in failed["checks"]))

    def test_current_assets_uses_combined_receivables_when_present(self):
        for extra, expected_sum, expected_pass in (
            ({110110: "30", 110111: "10", 110113: "20"}, "130", True),
            ({110110: "30", 110111: "10"}, "130", True),
            ({110110: "30", 110113: "20"}, "130", True),
            ({110110: "30"}, "130", True),
            ({110110: "0", 110111: "0", 110113: "0"}, "100", True),
            ({110110: "30", 110111: "10", 110113: ""}, "130", True),
            ({110110: "10", 110111: "10", 110113: ""}, "110", True),
            ({110110: "", 110111: "10", 110113: "20"}, "100", False),
        ):
            with self.subTest(extra=extra), redirect_stdout(io.StringIO()):
                values = {**self.values, **extra, 1101: expected_sum, 11: str(Decimal(expected_sum) + 200)}
                result = check_report(self.row(values=values), self.chart)
                check = next(check for check in result["checks"]
                             if check["check"] == "parent_sum" and check["item_id"] == 1101)
                self.assertEqual(check["children_sum"], expected_sum)
                self.assertNotIn(110111, check["children"])
                self.assertNotIn(110113, check["children"])
                self.assertTrue(check["passed"])
                self.assertEqual(result["passed"], expected_pass, result["issues"])
                if all(extra.get(item_id) not in (None, "", "0")
                       for item_id in (110110, 110111, 110113)):
                    self.assertIn(110110, check["children"])
                    combined = next(check for check in result["checks"] if check.get("item_id") == 110110)
                    self.assertEqual(combined["passed"], expected_pass)
                else:
                    self.assertFalse(any(check.get("item_id") == 110110
                                         for check in result["checks"]))

    def test_receivables_remain_current_assets_children_without_combined_item(self):
        with redirect_stdout(io.StringIO()):
            result = check_report(self.row(values={**self.values, 110111: "10", 110113: "20",
                                                   1101: "130", 11: "330"}), self.chart)
        check = next(check for check in result["checks"] if check.get("item_id") == 1101)
        self.assertTrue(result["passed"], result["issues"])
        self.assertIn(110111, check["children"])
        self.assertIn(110113, check["children"])

    def test_combined_receivables_must_equal_components_when_all_exist(self):
        for combined, expected_pass in (("30", True), ("30.001", True), ("31", False)):
            with self.subTest(combined=combined), redirect_stdout(io.StringIO()) as output:
                values = {**self.values, 110110: combined, 110111: "10", 110113: "20", 1101: "130", 11: "330"}
                result = check_report(self.row(values=values), self.chart)
                check = next(check for check in result["checks"]
                             if check["check"] == "parent_sum" and check["item_id"] == 110110)
                self.assertEqual(check["children"], [110111, 110113])
                self.assertEqual(check["children_sum"], "30")
                self.assertEqual(check["passed"], expected_pass)
                self.assertEqual(result["passed"], expected_pass)
                self.assertIn("item-equation", output.getvalue())
                if not expected_pass:
                    self.assertTrue(any("Item 110110" in issue for issue in result["issues"]))

    def test_units_are_applied_only_after_validation(self):
        for unit, multiplier in (("元", 1), ("万元", 10000), ("百万元", 1000000)):
            result = check_report(self.row(unit=unit), self.chart)
            self.assertTrue(result["passed"], result["issues"])
            cash = next(item for item in result["items"] if item["item_id"] == 110101)
            self.assertEqual(Decimal(cash["amount"]), 40 * multiplier)
        result = check_report(self.row(unit="未知单位"), self.chart)
        self.assertFalse(result["passed"])
        self.assertIn("Missing or conflicting currency unit", result["issues"])

    def test_current_assets_accepts_either_interest_and_dividends_hierarchy(self):
        cases = [(reporting_date, subtotal, direct, passed)
                 for reporting_date in ("2016-12-31", "2017-12-31", "2025-12-31")
                 for subtotal, direct, passed in (("112", True, True), ("100", False, True),
                                                 ("112.001", True, True), ("105", False, False))]
        for reporting_date, subtotal, direct, passed in cases:
            with self.subTest(reporting_date=reporting_date, subtotal=subtotal), redirect_stdout(io.StringIO()):
                row = self.row(values={**self.values, 11012801: "5", 11012802: "7",
                                       1101: subtotal, 11: str(Decimal(subtotal) + 200)})
                row["reporting_date"] = reporting_date
                result = check_report(row, self.chart)
                check = next(check for check in result["checks"] if check.get("item_id") == 1101)
                self.assertEqual(check["children_sum"], "112" if direct else "100")
                self.assertEqual(11012801 in check["children"], direct)
                self.assertEqual(11012802 in check["children"], direct)
                self.assertEqual(result["passed"], passed, result["issues"])

    def test_legacy_receivables_use_same_hierarchy_for_shifted_amounts(self):
        for subtotal, keep_interest in (("112", True), ("100", True), ("107", False)):
            with self.subTest(subtotal=subtotal), redirect_stdout(io.StringIO()):
                row = self.row(values={**self.values, 11012801: "5", 11012802: "7",
                                       1101: subtotal, 11: str(Decimal(subtotal) + 200)}, notes=True)
                row["reporting_date"] = "2017-06-30"
                row["balance_sheet"] = row["balance_sheet"].replace(
                    f"| {self.names[11012801]} | 六、1 | 5 | 999999 |",
                    f"| {self.names[11012801]} | | 5.00 | |")
                row["balance_sheet"] = row["balance_sheet"].replace(
                    f"| {self.names[1101]} | 六、1 | {subtotal} | 999999 |",
                    f"| {self.names[1101]} | {subtotal}.00 | 999999.00 | |")
                result = check_report(row, self.chart)
                self.assertTrue(result["passed"], result["issues"])
                self.assertEqual(any(item["item_id"] == 11012801 for item in result["items"]),
                                 keep_interest)
                if not keep_interest:
                    self.assertEqual(result["column_adjustments"][0]["parent_id"], 1101)

    def test_current_liabilities_accepts_either_interest_and_dividends_hierarchy(self):
        cases = [(reporting_date, subtotal, direct, passed)
                 for reporting_date in ("2016-12-31", "2017-12-31", "2025-12-31")
                 for subtotal, direct, passed in (("82", True, True), ("70", False, True),
                                                 ("82.001", True, True), ("75", False, False))]
        for reporting_date, subtotal, direct, passed in cases:
            with self.subTest(reporting_date=reporting_date, subtotal=subtotal), redirect_stdout(io.StringIO()):
                row = self.row(values={**self.values, 210135: "10", 21013501: "5", 21013502: "7",
                                       2101: subtotal, 21: str(Decimal(subtotal) + 60)})
                row["reporting_date"] = reporting_date
                result = check_report(row, self.chart)
                check = next(check for check in result["checks"] if check.get("item_id") == 2101)
                self.assertEqual(check["children_sum"], "82" if direct else "70")
                self.assertEqual(21013501 in check["children"], direct)
                self.assertEqual(21013502 in check["children"], direct)
                self.assertEqual(result["passed"], passed, result["issues"])

    def test_legacy_payables_use_same_hierarchy_for_shifted_amounts(self):
        for subtotal, keep_interest in (("82", True), ("70", True), ("77", False)):
            with self.subTest(subtotal=subtotal), redirect_stdout(io.StringIO()):
                row = self.row(values={**self.values, 210135: "10", 21013501: "5", 21013502: "7",
                                       2101: subtotal, 21: str(Decimal(subtotal) + 60)}, notes=True)
                row["reporting_date"] = "2017-06-30"
                row["balance_sheet"] = row["balance_sheet"].replace(
                    f"| {self.names[21013501]} | 六、1 | 5 | 999999 |",
                    f"| {self.names[21013501]} | | 5.00 | |")
                row["balance_sheet"] = row["balance_sheet"].replace(
                    f"| {self.names[2101]} | 六、1 | {subtotal} | 999999 |",
                    f"| {self.names[2101]} | {subtotal}.00 | 999999.00 | |")
                result = check_report(row, self.chart)
                self.assertTrue(result["passed"], result["issues"])
                self.assertEqual(any(item["item_id"] == 21013501 for item in result["items"]),
                                 keep_interest)
                if not keep_interest:
                    self.assertEqual(result["column_adjustments"][0]["parent_id"], 2101)

    def test_missing_metadata_and_malformed_amount_fail(self):
        for field in ("stock_code", "reporting_date", "pdf_creation_date", "balance_sheet"):
            row = self.row()
            row[field] = None
            self.assertFalse(check_report(row, self.chart)["passed"])
        result = check_report(self.row(values={**self.values, 110101: "not-a-number"}), self.chart)
        self.assertFalse(result["passed"])
        self.assertTrue(any("Invalid amount" in issue for issue in result["issues"]))

    def test_blank_section_headers_do_not_override_totals(self):
        row = self.row()
        row["balance_sheet"] = row["balance_sheet"].replace("| 资产总计", "| 流动资产： | | |\n| 资产总计")
        self.assertTrue(check_report(row, self.chart)["passed"])

    def test_conflicting_duplicate_amounts_fail(self):
        row = self.row()
        row["balance_sheet"] += "\n| 货币资金 | 999 | 0 |"
        result = check_report(row, self.chart)
        self.assertFalse(result["passed"])
        self.assertTrue(any("Conflicting amounts" in issue for issue in result["issues"]))

    def test_truncated_parent_equity_names_are_parsed_as_3110(self):
        for label in ("归属于母公司股东权益合", "归属于母公司所有者权益（或股东"):
            with self.subTest(label=label), redirect_stdout(io.StringIO()):
                row = self.row()
                row["balance_sheet"] = row["balance_sheet"].replace(
                    f"| {self.names[3110]} |", f"| {label} |")
                result = check_report(row, self.chart)
                self.assertTrue(result["passed"], result["issues"])
                self.assertEqual(result["unmapped_rows"], [])
                item = next(item for item in result["items"] if item["item_id"] == 3110)
                self.assertEqual(item["statement_amount"], self.values[3110])

    def test_truncated_liabilities_and_equity_total_is_ignored_as_item_zero(self):
        for label in ("负债和所有者权益（或股东权益", "负债和所有者权益（或股东权益）",
                      "负债和所有者权益(或股东权益"):
            with self.subTest(label=label), redirect_stdout(io.StringIO()):
                row = self.row()
                row["balance_sheet"] += f"\n| {label} | 999 | 0 |"
                result = check_report(row, self.chart)
                self.assertTrue(result["passed"], result["issues"])
                self.assertEqual(result["unmapped_rows"], [])
                self.assertFalse(any(item["item_id"] == 0 for item in result["items"]))

    def test_truncated_equity_total_suffix_is_ignored_when_unmapped(self):
        for label in ("益）合计", "股东权益）合计", "益)合计"):
            with self.subTest(label=label), redirect_stdout(io.StringIO()):
                row = self.row()
                row["balance_sheet"] += f"\n| {label} | 170 | 0 |"
                result = check_report(row, self.chart)
                self.assertEqual(result["unmapped_rows"], [])
                self.assertTrue(result["passed"], result["issues"])

    def test_long_item_name_suffixes_are_ignored_but_short_or_unrelated_names_fail(self):
        for label in ("益）合计", "股东权益)合计", "当期损益的金融负债", "金融资产", "融负债"):
            with self.subTest(label=label), redirect_stdout(io.StringIO()):
                row = self.row()
                row["balance_sheet"] += f"\n| {label} | 999 | 0 |"
                result = check_report(row, self.chart)
                self.assertTrue(result["passed"], result["issues"])
                self.assertEqual(result["unmapped_rows"], [])
                self.assertEqual(len(result["items"]), len(self.values))
        for label in ("负债", "资产", "合计", "未知科目", "当期损益", "随便写益）合计"):
            # Existing chart matches remain real items; only unmapped labels use the suffix rule.
            if self.chart.get(label) is not None:
                continue
            with self.subTest(label=label), redirect_stdout(io.StringIO()):
                row = self.row()
                row["balance_sheet"] += f"\n| {label} | 999 | 0 |"
                result = check_report(row, self.chart)
                self.assertFalse(result["passed"])
                self.assertEqual(result["unmapped_rows"][0]["label"], label)

    def test_unmapped_rows_fail_even_when_other_checks_pass(self):
        for amount in ("100", "0", ""):
            with self.subTest(amount=amount), redirect_stdout(io.StringIO()):
                row = self.row()
                row["balance_sheet"] += f"\n| 未知科目 | {amount} | 0 |"
                result = check_report(row, self.chart)
                self.assertFalse(result["passed"])
                self.assertEqual(len(result["unmapped_rows"]), 1)
                self.assertEqual(result["unmapped_rows"][0]["label"], "未知科目")
                self.assertIn("Unmapped balance-sheet rows: 1", result["issues"])
                self.assertTrue(all(check["passed"] for check in result["checks"]))

    def test_cli_unmapped_rows_are_saved_as_failed_digestion(self):
        with patch("digest_bs.LineItemStorage") as factory, patch("builtins.print"):
            storage = factory.return_value.__enter__.return_value
            row = self.row()
            row["balance_sheet"] += "\n| 未知科目 | 100 | 0 |"
            storage.get_audit_pass_reports.return_value = [row]
            self.assertEqual(main([]), 1)
            report = storage.store_line_item_digestion.call_args.args[1]
            self.assertFalse(report["passed"])
            self.assertTrue(report["unmapped_rows"])

    def test_label_annotations_are_removed_before_chart_lookup(self):
        for label in ("交易性金融资产【执行新金融工具准则】",
                      "交易性金融资产【执行新金融",
                      "交易性金融资产 【执行新金融工具准则】 ",
                      "交易性金融资产【执行新金融工具准则】【附注】"):
            with self.subTest(label=label), redirect_stdout(io.StringIO()):
                row = self.row(values={**self.values, 110107: "5", 1101: "105", 11: "305"})
                row["balance_sheet"] = row["balance_sheet"].replace("交易性金融资产", label)
                row["balance_sheet"] = row["balance_sheet"].replace("货币资金", "货币资金【附注】")
                result = check_report(row, self.chart)
                self.assertTrue(result["passed"], result["issues"])
                self.assertEqual(result["unmapped_rows"], [])
                amounts = {item["item_id"]: Decimal(item["amount"]) for item in result["items"]}
                self.assertEqual(amounts[110107], 5)
                self.assertEqual(amounts[110101], 40)

    def test_shifted_rows_use_current_amount_instead_of_notes_column(self):
        row = self.row(values={**self.values, 110239: "25", 1102: "225", 11: "325"}, notes=True)
        lines = []
        for line in row["balance_sheet"].splitlines():
            if line.startswith("|") and any(name in line for name in ("使用权资产", "无形资产", "非流动资产合计", "资产总计")):
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                line = f"| {cells[0]} | {Decimal(cells[2]):.2f} | 999999.00 | |"
            lines.append(line)
        row["balance_sheet"] = "\n".join(lines)
        with redirect_stdout(io.StringIO()):
            result = check_report(row, self.chart)
        self.assertTrue(result["passed"], result["issues"])
        amounts = {item["item_id"]: Decimal(item["amount"]) for item in result["items"]}
        self.assertEqual(amounts[110239], 25)
        self.assertEqual(amounts[110242], 10)
        self.assertEqual(amounts[1102], 225)
        self.assertEqual(amounts[11], 325)

    def test_ambiguous_prior_period_amount_is_omitted_only_when_subtotal_reconciles(self):
        for subtotal, keep in (("60", False), ("80", True), ("70", True)):
            with self.subTest(subtotal=subtotal), redirect_stdout(io.StringIO()):
                row = self.row(values={**self.values, 210147: "20", 2101: subtotal,
                                       21: str(Decimal(subtotal) + 60)}, notes=True)
                row["balance_sheet"] = row["balance_sheet"].replace(
                    "| 一年内到期的非流动负债 | 六、1 | 20 | 999999 |",
                    "| 一年内到期的非流动负债 | | 20.00 | |")
                # Shifted subtotal proves that this section has lost its notes column.
                row["balance_sheet"] = row["balance_sheet"].replace(
                    f"| 流动负债合计 | 六、1 | {subtotal} | 999999 |",
                    f"| 流动负债合计 | {Decimal(subtotal):.2f} | 999999.00 | |")
                result = check_report(row, self.chart)
                items = {item["item_id"]: item for item in result["items"]}
                self.assertEqual(210147 in items, keep)
                check = next(check for check in result["checks"] if check.get("item_id") == 2101)
                self.assertEqual(check["passed"], subtotal != "70")
                if not keep:
                    adjustment = result["column_adjustments"][0]
                    self.assertEqual(adjustment["item_id"], 210147)
                    self.assertEqual(Decimal(adjustment["sum_with"]), 80)
                    self.assertEqual(Decimal(adjustment["sum_without"]), 60)
                else:
                    self.assertNotIn("column_adjustments", result)

    def test_multiple_reconciling_omissions_are_left_unresolved(self):
        row = self.row(values={**self.values, 210147: "20", 210149: "20", 2101: "80", 21: "140"}, notes=True)
        for label in ("一年内到期的非流动负债", "其他流动负债"):
            row["balance_sheet"] = row["balance_sheet"].replace(
                f"| {label} | 六、1 | 20 | 999999 |", f"| {label} | | 20.00 | |")
        row["balance_sheet"] = row["balance_sheet"].replace(
            "| 流动负债合计 | 六、1 | 80 | 999999 |", "| 流动负债合计 | 80.00 | 999999.00 | |")
        with redirect_stdout(io.StringIO()):
            result = check_report(row, self.chart)
        self.assertFalse(result["passed"])
        self.assertNotIn("column_adjustments", result)
        self.assertTrue({210147, 210149}.issubset({item["item_id"] for item in result["items"]}))

    def test_unshifted_rows_are_not_omitted_to_force_a_subtotal_match(self):
        row = self.row(values={**self.values, 210147: "20"}, notes=True)
        row["balance_sheet"] = row["balance_sheet"].replace(
            "| 一年内到期的非流动负债 | 六、1 | 20 | 999999 |",
            "| 一年内到期的非流动负债 | | 20.00 | |")
        with redirect_stdout(io.StringIO()):
            result = check_report(row, self.chart)
        self.assertFalse(result["passed"])
        self.assertNotIn("column_adjustments", result)
        self.assertTrue(any(item["item_id"] == 210147 for item in result["items"]))

    def test_integer_and_section_note_references_are_not_amounts(self):
        for note in ("5", "6.1", "六、1"):
            with self.subTest(note=note), redirect_stdout(io.StringIO()):
                row = self.row(notes=True)
                row["balance_sheet"] = row["balance_sheet"].replace("六、1", note)
                result = check_report(row, self.chart)
                self.assertTrue(result["passed"], result["issues"])
                cash = next(item for item in result["items"] if item["item_id"] == 110101)
                self.assertEqual(Decimal(cash["amount"]), 40)

    def test_numeric_formats(self):
        for text, expected in (("1,234.50", "1234.50"), ("（123.45）", "-123.45"),
                               ("−12.50", "-12.50"), ("**0.00**", "0.00"),
                               ("/", "0"), ("-", "0"), (" / ", "0")):
            self.assertEqual(parse_amount(text), Decimal(expected))
        for value in ("", "—", "--"):
            self.assertIsNone(parse_amount(value))
        for value in ("NaN", "Infinity", "abc", "1,2"):
            with self.assertRaises(ValueError):
                parse_amount(value)

    def test_slash_and_hyphen_are_zero_in_report_checks(self):
        for marker in ("/", "-"):
            with self.subTest(marker=marker), redirect_stdout(io.StringIO()):
                result = check_report(self.row(values={**self.values, 110203: marker}), self.chart)
                self.assertTrue(result["passed"], result["issues"])
                item = next(item for item in result["items"] if item["item_id"] == 110203)
                self.assertEqual(Decimal(item["amount"]), 0)
                result = check_report(self.row(values={**self.values, 110101: marker}), self.chart)
                self.assertFalse(result["passed"])
                summary = next(check for check in result["checks"] if check["check"] == "required_nonzero")
                self.assertIn(110101, summary["failed_item_ids"])
                self.assertFalse(any("Invalid amount" in issue for issue in result["issues"]))

    def test_cli_hash_selects_one_report_and_allows_any_stage(self):
        pdf_hash = "007b7f4055bbfeedac1e0742338e0f00"
        with patch("digest_bs.LineItemStorage") as factory, patch("builtins.print"):
            storage = factory.return_value.__enter__.return_value
            row = {**self.row(), "pdf_hash": pdf_hash}
            storage.get_audit_pass_reports.return_value = [row]
            self.assertEqual(main(["--hash", pdf_hash.upper()]), 0)
            storage.get_audit_pass_reports.assert_called_once_with(pdf_hash=pdf_hash, retry=False)
            storage.store_line_item_digestion.assert_called_once()
            self.assertEqual(storage.store_line_item_digestion.call_args.kwargs, {"allow_any_stage": True})
            self.assertEqual(storage.store_line_item_digestion.call_args.args[0]["pdf_hash"], pdf_hash)

    def test_cli_hash_without_report_does_not_write(self):
        pdf_hash = "007b7f4055bbfeedac1e0742338e0f00"
        with patch("digest_bs.LineItemStorage") as factory, patch("builtins.print") as output:
            storage = factory.return_value.__enter__.return_value
            storage.get_audit_pass_reports.return_value = []
            self.assertEqual(main(["--hash", pdf_hash]), 1)
            storage.store_line_item_digestion.assert_not_called()
            self.assertIn("Summary: 0 succeeded, 1 failed", output.call_args.args[0])

    def test_cli_rejects_invalid_hash_before_database_access(self):
        for value in ("", "xyz", "a" * 31, "g" * 32):
            with self.subTest(value=value), patch("digest_bs.LineItemStorage") as factory, patch("sys.stderr"):
                with self.assertRaises(SystemExit) as error:
                    main(["--hash", value])
                self.assertEqual(error.exception.code, 2)
                factory.assert_not_called()

    def test_cli_json_output_omits_items(self):
        with patch("digest_bs.LineItemStorage") as factory, patch("builtins.print") as output:
            storage = factory.return_value.__enter__.return_value
            storage.get_audit_pass_reports.return_value = [self.row()]
            self.assertEqual(main([]), 0)
        reports = [json.loads(call.args[0]) for call in output.call_args_list if call.args[0].startswith("{")]
        self.assertEqual(reports, [])
        self.assertTrue(any(call.args[0].endswith(": PASS") for call in output.call_args_list))

    def test_cli_processes_audit_pass_queue_and_saves_failure_details(self):
        with patch("digest_bs.LineItemStorage") as factory, patch("builtins.print") as output:
            storage = factory.return_value.__enter__.return_value
            invalid = self.row(values={**self.values, 110101: "0"})
            invalid["pdf_hash"] = "bad"
            storage.get_audit_pass_reports.return_value = [self.row(), invalid]
            self.assertEqual(main([]), 1)
            storage.get_audit_pass_reports.assert_called_once_with(pdf_hash=None, retry=False)
            saved = storage.store_line_item_digestion.call_args_list
            self.assertEqual(len(saved), 2)
            self.assertTrue(saved[0].args[1]["passed"])
            self.assertFalse(saved[1].args[1]["passed"])
            self.assertTrue(saved[1].args[1]["issues"])
            json_reports = [json.loads(call.args[0]) for call in output.call_args_list
                            if call.args[0].startswith("{")]
            self.assertEqual(len(json_reports), 1)
            self.assertFalse(json_reports[0]["passed"])

    def test_cli_handles_empty_queue_and_database_failure(self):
        with patch("digest_bs.LineItemStorage") as factory, patch("builtins.print"):
            storage = factory.return_value.__enter__.return_value
            storage.get_audit_pass_reports.return_value = []
            self.assertEqual(main([]), 0)
            storage.store_line_item_digestion.assert_not_called()
            storage.get_audit_pass_reports.side_effect = RuntimeError("unavailable")
            self.assertEqual(main([]), 1)

    def test_cli_records_storage_failure_and_continues(self):
        with patch("digest_bs.LineItemStorage") as factory, patch("builtins.print"):
            storage = factory.return_value.__enter__.return_value
            storage.get_audit_pass_reports.return_value = [self.row(), {**self.row(), "pdf_hash": "next"}]
            saved = []

            def save(row, report):
                saved.append(copy.deepcopy(report))
                if len(saved) == 1:
                    raise RuntimeError("write failed")

            storage.store_line_item_digestion.side_effect = save
            self.assertEqual(main([]), 1)
            self.assertEqual(len(saved), 3)
            self.assertFalse(saved[1]["passed"])
            self.assertIn("write failed", saved[1]["issues"][-1])
            self.assertTrue(saved[2]["passed"])
            storage.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
