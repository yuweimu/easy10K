import json
import unittest

from line_item_storage import LineItemStorage


class DebugE409(unittest.TestCase):
    def test_dump(self):
        with LineItemStorage() as storage:
            rows = storage.get_extracted_reports(pdf_hash="e409b0614e096801de35be38cbbf3c77")
            output = [f"ROWS {len(rows)}"]
            if rows:
                output.append(f"KEYS {sorted(rows[0])}")
                report = json.loads(rows[0].get("extraction_report") or "{}")
                for key in ("balance_sheet", "cash_flow_statement"):
                    statement = report.get(key) or {}
                    output.append(f"{key} title {statement.get('title')} rows {len(statement.get('rows') or [])}")
                    for index, row in enumerate(statement.get("rows") or [], 1):
                        if key == "cash_flow_statement" or index < 8 or index > 45:
                            output.append(f"{index} {row!r}")
            open("/tmp/e409-debug.txt", "w", encoding="utf-8").write("\\n".join(output))
