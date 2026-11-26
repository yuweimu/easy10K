from contextlib import redirect_stdout
from decimal import Decimal
import io
import json
import unittest
from unittest.mock import patch

import digest_is as income
import digest_cf as cash
from line_item_chart import LineItemChart
from statement_digestion import normalize_label, parse_statement


class StatementDigestionTests(unittest.TestCase):
    def setUp(self):
        self.chart = LineItemChart()
        self.income = {41: "1000", 4101: "900", 4103: "100", 42: "600", 4201: "500",
                       4217: "50", 4221: "30", 4225: "20", 422501: "5", 422503: "3",
                       43: "10", 44: "5", 47: "-2", 48: "-3", 51: "410", 52: "2", 53: "12",
                       54: "400", 55: "100", 56: "300", 560101: "300", 560103: "0",
                       560201: "280", 560203: "20", 57: "7", 5701: "6", 570101: "4",
                       57010101: "4", 570103: "2", 57010311: "2", 5703: "1", 58: "307",
                       5801: "286", 5803: "21", 5901: "0.1256", 5903: "0.1201"}
        self.cash = {810101: "100", 810125: "50", 8101: "150", 810201: "40", 810219: "20",
                     8102: "60", 81: "90", 820101: "10", 8201: "10", 820203: "20", 8202: "20",
                     82: "-10", 830101: "30", 83010101: "10", 8301: "30", 830203: "50",
                     83020301: "15", 8302: "50", 83: "-20", 84: "1", 85: "61", 86: "100", 87: "161"}

    def row(self, statement, values, *, notes=False, unit="元", labels=None):
        labels = labels or {}
        lines = [f"单位：{unit}", "| 项目 | 附注 | 本期 | 上期 |" if notes else "| 项目 | 本期 | 上期 |",
                 "| --- | --- | --- | --- |" if notes else "| --- | --- | --- |"]
        for item_id, amount in values.items():
            name = self.chart._items_by_id[item_id].name
            if item_id in (44, 47, 48, 49):
                name += "（损失以“-”号填列）"
            name = labels.get(item_id, name)
            cells = [name] + (["六、1"] if notes else []) + [amount, "99999"]
            lines.append("| " + " | ".join(cells) + " |")
        return {"pdf_hash": "a" * 32, "stock_code": "600519", "reporting_date": "2025-09-30",
                "pdf_creation_date": "created", statement: "\n".join(lines)}

    def check(self, module, statement, values, **kwargs):
        with redirect_stdout(io.StringIO()):
            return module.check_report(self.row(statement, values, **kwargs), self.chart)

    def test_income_all_equations_and_duplicate_interest_context(self):
        for notes in (False, True):
            result = self.check(income, "income_statement", self.income, notes=notes,
                                labels={4101: "其中：营业收入", 422501: "其中：利息费用", 422503: "利息收入"})
            self.assertTrue(result["passed"], result["issues"])
            self.assertEqual(result["unmapped_rows"], [])
            items = {item["item_id"]: item for item in result["items"]}
            self.assertEqual(items[4103]["statement_amount"], "100")
            self.assertEqual(items[422503]["statement_amount"], "3")
            self.assertFalse(any(check.get("item_id") in (4225, 44, 59) for check in result["checks"]))
            self.assertTrue(all(check["passed"] for check in result["checks"]))

    def test_leading_financial_asset_marker_is_ignored(self):
        for label in ("☆交易性金融资产", "☆其他债权投资", "☆债权投资", "☆其他非流动金融资产"):
            with self.subTest(label=label):
                self.assertEqual(normalize_label(label), label[1:])

    def test_eps_labels_with_slash_variants_map_to_per_share_items(self):
        for slash in ("/", "／", "╱"):
            with self.subTest(slash=slash):
                markdown = "\n".join((
                    "| 项目 | 附注 | 本期 | 上期 |",
                    "| --- | --- | --- | --- |",
                    f"| （一）基本每股收益（元{slash}股） | | 0.68 | 0.50 |",
                    f"| （二）稀释每股收益（元{slash}股） | | 0.64 | 0.48 |",
                ))
                state = parse_statement(markdown, self.chart, "income_statement")
                self.assertEqual(state.amounts[5901], Decimal("0.68"))
                self.assertEqual(state.amounts[5903], Decimal("0.64"))
                self.assertEqual(state.unmapped_rows, [])

    def test_other_comprehensive_income_legacy_labels_are_mapped(self):
        markdown = "\n".join((
            "| 项目 | 本期 | 上期 |",
            "| --- | --- | --- |",
            "| （一）以后不能重分类进损益的 其他综合收益 | 1 |  |",
            "| 2.权益法下在被投资单位不 能重分类进损益的其他综合收益中 享有的份额 | 2 |  |",
        ))
        state = parse_statement(markdown, self.chart, "income_statement")
        self.assertEqual(state.amounts[570101], Decimal("1"))
        self.assertEqual(state.amounts[57010103], Decimal("2"))
        self.assertEqual(state.unmapped_rows, [])

    def test_income_each_required_total_and_tax_dependency(self):
        for item_id in (*income.REQUIRED_IDS, 55):
            with self.subTest(item_id=item_id):
                result = self.check(income, "income_statement", {key: value for key, value in self.income.items() if key != item_id})
                self.assertFalse(result["passed"])

    def test_income_legacy_business_tax_label_maps_to_tax_surcharges(self):
        markdown = "\n".join((
            "| 项目 | 本期 | 上期 |",
            "| --- | --- | --- |",
            "| 二、营业总成本 | 600 |  |",
            "| 其中：营业成本 | 500 |  |",
            "| 营业税金及附加 | 50 |  |",
            "| 销售费用 | 30 |  |",
            "| 管理费用 | 20 |  |",
        ))
        state = parse_statement(markdown, self.chart, "income_statement")
        self.assertEqual(state.amounts[4217], Decimal("50"))
        self.assertEqual(state.unmapped_rows, [])

    def test_no_total_revenue_or_total_cost_row_uses_details_without_double_counting(self):
        values = {key: value for key, value in self.income.items() if key not in (41, 42, 4103)}
        values[4101] = "1000"
        result = self.check(income, "income_statement", values)
        self.assertTrue(result["passed"], result["issues"])

    def test_eps_never_scaled_with_statement_currency_unit(self):
        result = self.check(income, "income_statement", self.income, unit="万元")
        self.assertTrue(result["passed"], result["issues"])
        items = {item["item_id"]: item for item in result["items"]}
        self.assertEqual(items[41]["amount"], "10000000")
        self.assertEqual(items[5901]["amount"], "0.1256")
        self.assertEqual(items[5901]["amount_unit"], "元/股")

    def test_profit_losses_and_zero_profit_are_valid(self):
        for profit in ("-100", "0"):
            values = {41: "100", 4101: "100", 42: str(100 - Decimal(profit)),
                      4201: str(100 - Decimal(profit)), 51: profit, 54: profit, 55: "0", 56: profit}
            result = self.check(income, "income_statement", values)
            self.assertTrue(result["passed"], result["issues"])

    def test_loss_sign_conventions_and_refunds(self):
        values = {41: "100", 4101: "100", 42: "50", 4201: "50", 48: "3", 51: "47", 54: "47", 55: "-2", 56: "49"}
        result = self.check(income, "income_statement", values, labels={48: "减：信用减值损失"})
        self.assertTrue(result["passed"], result["issues"])
        cost_check = next(check for check in result["checks"]
                          if check["check"] == "children_sum" and check["item_id"] == 42)
        self.assertNotIn(48, cost_check["children"])
        result = self.check(income, "income_statement", values,
                            labels={48: "信用减值损失（损失以“-”号填列）"})
        self.assertTrue(result["passed"], result["issues"])
        values.update({48: "-3"})
        result = self.check(income, "income_statement", values)
        self.assertTrue(result["passed"], result["issues"])

    def test_net_profit_classifications_are_separate_equations(self):
        result = self.check(income, "income_statement", self.income)
        checks = {check["check"]: check for check in result["checks"]}
        self.assertEqual(checks["net_profit_by_operations"]["children_sum"], "300")
        self.assertEqual(checks["net_profit_by_ownership"]["children_sum"], "300")
        result = self.check(income, "income_statement", {**self.income, 560203: "21"})
        self.assertFalse(result["passed"])

    def test_net_profit_with_superscript_footnote_reconciles(self):
        result = self.check(income, "income_statement",
                            {**self.income, 56: "300<sup>1</sup>"})
        self.assertTrue(result["passed"], result["issues"])
        check = next(check for check in result["checks"] if check["check"] == "comprehensive_income")
        self.assertEqual(check["children_sum"], "307")

    def test_cash_all_equations_and_included_minority_disclosures(self):
        for notes in (False, True):
            result = self.check(cash, "cash_flow_statement", self.cash, notes=notes)
            self.assertTrue(result["passed"], result["issues"])
            inflows = next(check for check in result["checks"] if check.get("item_id") == 8301)
            self.assertEqual(inflows["children"], [830101])
            self.assertEqual(inflows["children_sum"], "30")
            result = self.check(cash, "cash_flow_statement", {**self.cash, 83010101: "31"})
            self.assertFalse(result["passed"])
            self.assertTrue(any("minority-interest" in issue for issue in result["issues"]))

    def test_600398_allows_source_report_minority_detail_exception(self):
        values = {**self.cash, 830101: "125", 83010101: "250", 8301: "125",
                  830203: "10", 83020301: "20", 8302: "10", 83: "115",
                  85: "196", 87: "296"}
        row = self.row("cash_flow_statement", values)
        row["stock_code"] = "600398"
        result = cash.check_report(row, self.chart)
        self.assertTrue(result["passed"], result["issues"])

    def test_300308_source_report_exception_is_limited_to_one_pdf(self):
        values = {**self.cash, 830101: "611745.51", 83010101: "35692000",
                  8301: "611745.51", 83: "611695.51", 85: "611776.51",
                  87: "611876.51"}
        row = self.row("cash_flow_statement", values)
        row["stock_code"] = "300308"
        row["pdf_hash"] = "e53644c29920cde9758664c310b60156"
        result = cash.check_report(row, self.chart)
        self.assertTrue(result["passed"], result["issues"])

        row["pdf_hash"] = "b" * 32
        result = cash.check_report(row, self.chart)
        self.assertFalse(result["passed"])
        self.assertTrue(any("minority-interest" in issue for issue in result["issues"]))

    def test_rows_with_wrong_table_width_are_reported(self):
        markdown = "\n".join((
            "| 项目 | 本期 | 上期 |",
            "| 营业总收入 | 100 | 100 |",
            "| 营业收入 | 100 |",
        ))
        state = parse_statement(markdown, self.chart, "income_statement")
        self.assertTrue(any("expected 3 table columns, got 2" in issue
                            for issue in state.issues))

    def test_comprehensive_income_ownership_must_reconcile_even_when_total_matches(self):
        values = {**self.income, 5801: "285", 5803: "22"}
        result = self.check(income, "income_statement", values)
        self.assertFalse(result["passed"])
        self.assertTrue(any(check["check"] == "comprehensive_income_by_ownership" and not check["passed"]
                            for check in result["checks"]))

    def test_blank_minority_comprehensive_total_is_derived_from_minority_profit(self):
        values = {key: value for key, value in self.income.items()
                  if not str(key).startswith(("56", "57", "58"))}
        values.update({51: "369611314.60", 54: "369611314.60", 55: "0",
                       56: "369611314.60", 560201: "370592915.28",
                       560203: "-981600.68", 57: "0", 5801: "370592915.28",
                       58: "369611314.60"})
        result = self.check(income, "income_statement", values)
        check = next(check for check in result["checks"]
                     if check.get("item_id") == 5803)
        self.assertTrue(check["passed"])
        self.assertEqual(check["amount"], "-981600.68")
        self.assertEqual(check["children_sum"], "-981600.68")

    def test_blank_ownership_profit_does_not_crash_comprehensive_repair(self):
        values = {**self.income, 560203: "", 5803: ""}
        result = self.check(income, "income_statement", values)
        self.assertFalse(result["passed"])
        self.assertFalse(any("Unable to check report" in issue for issue in result["issues"]))

    def test_negative_financial_sector_cash_components_are_valid(self):
        result = self.check(cash, "cash_flow_statement", {**self.cash, 810103: "-5", 810125: "55"})
        self.assertTrue(result["passed"], result["issues"])

    def test_blank_financing_activity_and_zero_net_cash_are_valid(self):
        values = {key: ("" if str(key).startswith("83") else value) for key, value in self.cash.items()}
        values.update({85: "81", 87: "181"})
        result = self.check(cash, "cash_flow_statement", values)
        self.assertTrue(result["passed"], result["issues"])

    def test_cash_each_required_item_is_required_and_closing_cash_nonnegative(self):
        for item_id in cash.REQUIRED_IDS:
            with self.subTest(item_id=item_id):
                values = {key: value for key, value in self.cash.items() if key != item_id}
                self.assertFalse(self.check(cash, "cash_flow_statement", values)["passed"])
        result = self.check(cash, "cash_flow_statement", {**self.cash, 87: "-1"})
        self.assertTrue(any("cannot be negative" in issue for issue in result["issues"]))

    def test_fx_and_cash_roll_forward_are_checked(self):
        for item_id in (84, 85, 86, 87, 8101, 8202):
            with self.subTest(item_id=item_id):
                result = self.check(cash, "cash_flow_statement", {**self.cash, item_id: str(Decimal(self.cash[item_id]) + 5)})
                self.assertFalse(result["passed"])

    def test_unmapped_malformed_and_missing_metadata_fail(self):
        for module, field, values in ((income, "income_statement", self.income), (cash, "cash_flow_statement", self.cash)):
            for addition in ("\n| 未知科目 | 0 | 0 |", "\n| 空科目 |  |  |"):
                row = self.row(field, values)
                row[field] += addition
                with redirect_stdout(io.StringIO()):
                    result = module.check_report(row, self.chart)
                self.assertTrue(result["passed"], result["issues"])
                self.assertEqual(result["unmapped_rows"], [])
            row = self.row(field, values)
            row[field] += "\n| 未知科目 | 1000000 | 1000000 |"
            with redirect_stdout(io.StringIO()):
                result = module.check_report(row, self.chart)
            self.assertFalse(result["passed"])
            self.assertTrue(result["unmapped_rows"])
            row = self.row(field, values)
            row[field] += "\n| 其他科目 | abc | 0 |"
            with redirect_stdout(io.StringIO()):
                result = module.check_report(row, self.chart)
            self.assertFalse(result["passed"])
            self.assertTrue(result["unmapped_rows"])
            for metadata in ("stock_code", "reporting_date", "pdf_creation_date", field):
                row = self.row(field, values)
                row[metadata] = None
                with redirect_stdout(io.StringIO()):
                    self.assertFalse(module.check_report(row, self.chart)["passed"])
            bad = {**values, next(iter(values)): "bad amount"}
            result = self.check(module, field, bad)
            self.assertFalse(result["passed"])
            self.assertTrue(any("Invalid amount" in issue for issue in result["issues"]))

    def test_tolerances_apply_to_signed_reconciliations(self):
        row = self.row("income_statement", {**self.income, 51: "411"})
        with redirect_stdout(io.StringIO()):
            strict = income.check_report(row, self.chart)
            relaxed = income.check_report(row, self.chart, absolute_tolerance=Decimal("1"))
        self.assertFalse(strict["passed"])
        self.assertTrue(relaxed["passed"], relaxed["issues"])

    def test_multiple_possible_omissions_are_not_guessed(self):
        values = {**self.cash, 820205: "5", 820209: "5", 8202: "25", 82: "-15", 85: "56", 87: "156"}
        row = self.row("cash_flow_statement", values, notes=True)
        for item_id in (820205, 820209):
            name = self.chart._items_by_id[item_id].name
            row["cash_flow_statement"] = row["cash_flow_statement"].replace(
                f"| {name} | 六、1 | 5 | 99999 |", f"| {name} | | 5.00 | |")
        row["cash_flow_statement"] = row["cash_flow_statement"].replace(
            "| 投资活动现金流出小计 | 六、1 | 25 | 99999 |", "| 投资活动现金流出小计 | 25.00 | 99999.00 | |")
        with redirect_stdout(io.StringIO()):
            result = cash.check_report(row, self.chart)
        self.assertFalse(result["passed"])
        self.assertNotIn("column_adjustments", result)
        self.assertTrue(all(any(item["item_id"] == item_id for item in result["items"]) for item_id in (820205, 820209)))

    def test_empty_required_income_totals_and_empty_closing_cash_fail(self):
        values = {41: "0", 4101: "0", 42: "0", 4201: "0", 51: "", 54: "", 55: "0", 56: ""}
        self.assertFalse(self.check(income, "income_statement", values)["passed"])
        self.assertFalse(self.check(cash, "cash_flow_statement", {**self.cash, 87: ""})["passed"])

    def test_shifted_current_amount_and_ambiguous_prior_period_are_resolved(self):
        for module, field, original, extra_id, parent_id in (
                (income, "income_statement", self.income, 4205, 42),
                (cash, "cash_flow_statement", self.cash, 820205, 8202)):
            for shifted, keep, extra in ((True, False, "5"), (False, True, "5"), (True, True, "0")):
                with self.subTest(field=field, shifted=shifted, extra=extra):
                    row = self.row(field, {**original, extra_id: extra}, notes=True)
                    extra_name = self.chart._items_by_id[extra_id].name
                    parent_name = self.chart._items_by_id[parent_id].name
                    row[field] = row[field].replace(f"| {extra_name} | 六、1 | {extra} | 99999 |", f"| {extra_name} | | {extra}.00 | |")
                    if shifted:
                        row[field] = row[field].replace(f"| {parent_name} | 六、1 | {original[parent_id]} | 99999 |",
                                                        f"| {parent_name} | {original[parent_id]}.00 | 99999.00 | |")
                    with redirect_stdout(io.StringIO()):
                        result = module.check_report(row, self.chart)
                    self.assertEqual(any(item["item_id"] == extra_id for item in result["items"]), keep)
                    self.assertEqual(result["passed"], shifted)
                    if not keep:
                        self.assertEqual(result["column_adjustments"][0]["item_id"], extra_id)

    @patch("statement_digestion.LineItemStorage")
    def test_cli_processes_entire_queue_hash_override_and_dry_run(self, factory):
        storage = factory.return_value.__enter__.return_value
        for module, field, values in ((income, "income_statement", self.income), (cash, "cash_flow_statement", self.cash)):
            storage.reset_mock()
            storage.get_statement_reports.return_value = [self.row(field, values), self.row(field, values)]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(module.main([]), 0)
            self.assertEqual(storage.store_statement_digestion.call_count, 2)
            storage.store_statement_digestion.reset_mock()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(["--hash", "A" * 32, "--dry-run"]), 0)
            storage.get_statement_reports.assert_called_with(field, pdf_hash="a" * 32, retry=False)
            storage.store_statement_digestion.assert_not_called()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(["--retry", "--dry-run"]), 0)
            storage.get_statement_reports.assert_called_with(field, pdf_hash=None, retry=True)

    @patch("statement_digestion.LineItemStorage")
    def test_cli_ex_forwards_external_source_for_both_statements(self, factory):
        storage = factory.return_value.__enter__.return_value
        for module, field, values in ((income, "income_statement", self.income),
                                      (cash, "cash_flow_statement", self.cash)):
            storage.reset_mock()
            storage.get_statement_reports.return_value = [self.row(field, values)]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(["--ex"]), 0)
            storage.get_statement_reports.assert_called_once_with(field, pdf_hash=None, retry=False, ex=True)
            self.assertTrue(storage.store_statement_digestion.call_args.kwargs["ex"])

    @patch("statement_digestion.LineItemStorage")
    def test_cli_saves_failure_and_continues_after_storage_error(self, factory):
        storage = factory.return_value.__enter__.return_value
        storage.get_statement_reports.return_value = [self.row("income_statement", self.income)] * 2
        storage.store_statement_digestion.side_effect = [RuntimeError("database unavailable"), None, None]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(income.main(["--hash", "a" * 32]), 1)
        self.assertEqual(storage.store_statement_digestion.call_count, 3)
        self.assertTrue(storage.store_statement_digestion.call_args.kwargs["allow_any_stage"])
        reports = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
        self.assertFalse(reports[0]["passed"])
        self.assertEqual(len(reports), 1)
        self.assertIn(":" + " PASS", output.getvalue())
        self.assertTrue(all("items" not in report for report in reports))
