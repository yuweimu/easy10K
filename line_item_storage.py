"""PostgreSQL persistence for extracted financial statement line items."""

import importlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import re
from types import TracebackType
from typing import Literal, Self, get_args

from dotenv import load_dotenv


# Resolve beside the program, independently of the working directory.
load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)

ReportDigestionStage = Literal[
    "FileOK", "FileErr", "ExtractionSuccess", "ExtractionError",
    "AuditPass", "AuditIssues", "ManualPass", "Deprecated", "NoStatements", "BalanceSheetOK", "BalanceSheetErr",
    "IncomStatementOK", "IncomeStatementErr", "CashFlowStatementOK", "CashFlowStatementErr",
]

STATEMENT_DIGESTION = {
    "income_statement": ("BalanceSheetOK", "IncomStatementOK", "IncomeStatementErr", "is_digestion_report", "income_statement_items"),
    "cash_flow_statement": ("IncomStatementOK", "CashFlowStatementOK", "CashFlowStatementErr", "cfs_digestion_report", "cash_flow_statement_items"),
}


def _has_missing_quarters(reporting_dates: list[date]) -> bool:
    """Check quarter ends only within the stock's available reporting range."""
    if not reporting_dates:
        return True
    available = set(reporting_dates)
    first, last = min(available), max(available)
    return any(
        first <= quarter_end <= last and quarter_end not in available
        for year in range(first.year, last.year + 1)
        for quarter_end in (date(year, 3, 31), date(year, 6, 30),
                            date(year, 9, 30), date(year, 12, 31))
    )


def _registry_path(value: str | None) -> str | None:
    """Store local file paths relative to the program's working directory."""
    return os.path.relpath(Path(value).resolve()) if value else None


def _normalize_pdf_creation_date(value: str | datetime | None) -> str | datetime | None:
    """Convert PyMuPDF's compact creation timestamp to a database datetime."""
    if not isinstance(value, str):
        return value
    match = re.fullmatch(r"D:?(\d{14})([+-]\d{2}'?\d{2}'?)?", value)
    if not match:
        return value or None
    created = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    offset = match.group(2)
    if offset:
        sign = 1 if offset[0] == "+" else -1
        hours, minutes = map(int, re.findall(r"\d{2}", offset))
        created = created.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
    return created.replace(tzinfo=created.tzinfo or timezone.utc)


class LineItemStorage:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self._connection = None
        self._table_ready = False
        self._report_registry_ready = False
        self._report_digestion_ready = False

    def store_downloaded_report(
        self, pdf_hash: str, download_url: str, local_path: str, *,
        stock_code: str | None = None, reporting_date: str | None = None,
        pdf_creation_date: str | None = None,
        download_report: str | None = None,
    ) -> None:
        """Register a completed download and retain any available identifiers."""
        if not all(isinstance(value, str) and value.strip()
                   for value in (pdf_hash, download_url, local_path)):
            raise ValueError("Download hash, URL, and local path must be nonblank strings")
        if not self.database_url:
            return
        connection = self._connect()
        with connection.cursor() as cursor:
            if not self._report_digestion_ready:
                cursor.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            cursor.execute(
                """
                INSERT INTO report_digestion (
                    pdf_hash, download_url, local_path, download_report,
                    stock_code, reporting_date, pdf_creation_date, digestion_stage
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'FileOK')
                ON CONFLICT (pdf_hash) DO UPDATE SET
                    download_url = EXCLUDED.download_url,
                    local_path = EXCLUDED.local_path,
                    download_report = COALESCE(EXCLUDED.download_report, report_digestion.download_report),
                    stock_code = COALESCE(EXCLUDED.stock_code, report_digestion.stock_code),
                    reporting_date = COALESCE(EXCLUDED.reporting_date, report_digestion.reporting_date),
                    pdf_creation_date = COALESCE(EXCLUDED.pdf_creation_date, report_digestion.pdf_creation_date),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (pdf_hash, download_url, _registry_path(local_path), download_report,
                 stock_code or None, reporting_date or None,
                 _normalize_pdf_creation_date(pdf_creation_date)),
            )
        connection.commit()
        self._report_digestion_ready = True

    def store_extracted_report(
        self, pdf_hash: str, extraction_report: str,
        balance_sheet: str | None, income_statement: str | None,
        cash_flow_statement: str | None, *, local_path: str,
        stock_code: str | None = None, reporting_date: str | None = None,
        pdf_creation_date: str | None = None,
    ) -> None:
        """Atomically upsert JSON/Markdown content and mark extraction complete."""
        if not isinstance(pdf_hash, str) or not pdf_hash.strip():
            raise ValueError("PDF hash must be a nonblank string")
        if not self.database_url:
            return
        connection = self._connect()
        with connection.cursor() as cursor:
            if not self._report_digestion_ready:
                cursor.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            cursor.execute(
                """
                INSERT INTO report_digestion (
                    pdf_hash, extraction_report, balance_sheet, income_statement,
                    cash_flow_statement, local_path, stock_code, reporting_date,
                    pdf_creation_date, digestion_stage
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ExtractionSuccess')
                ON CONFLICT (pdf_hash) DO UPDATE SET
                    extraction_report = EXCLUDED.extraction_report,
                    balance_sheet = EXCLUDED.balance_sheet,
                    income_statement = EXCLUDED.income_statement,
                    cash_flow_statement = EXCLUDED.cash_flow_statement,
                    local_path = EXCLUDED.local_path,
                    stock_code = COALESCE(EXCLUDED.stock_code, report_digestion.stock_code),
                    reporting_date = COALESCE(EXCLUDED.reporting_date, report_digestion.reporting_date),
                    pdf_creation_date = COALESCE(EXCLUDED.pdf_creation_date, report_digestion.pdf_creation_date),
                    digestion_stage = 'ExtractionSuccess',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (pdf_hash, extraction_report, balance_sheet, income_statement, cash_flow_statement,
                 _registry_path(local_path), stock_code or None, reporting_date or None,
                 _normalize_pdf_creation_date(pdf_creation_date) or None),
            )
        connection.commit()
        self._report_digestion_ready = True

    def _connect(self):
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        if self._connection is None:
            try:
                psycopg = importlib.import_module("psycopg")
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL persistence requires psycopg; "
                    "install the project requirements first"
                ) from exc
            self._connection = psycopg.connect(self.database_url)
        return self._connection

    def _ensure_table(self) -> None:
        if self._table_ready:
            return
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS balance_sheet_items (
                    stock_code VARCHAR(6) NOT NULL,
                    reporting_date DATE NOT NULL,
                    item_id INTEGER NOT NULL,
                    report_creation_time TEXT NOT NULL,
                    amount NUMERIC(20, 2) NOT NULL,
                    UNIQUE (stock_code, reporting_date, item_id, report_creation_time)
                )
                """
            )
            cursor.execute("ALTER TABLE balance_sheet_items DROP COLUMN IF EXISTS item_english_name")
        connection.commit()
        self._table_ready = True

    def ensure_report_registry(self) -> None:
        """Create the registry and remove the obsolete digestion timestamp field."""
        if self._report_registry_ready:
            return
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS report_registry (
                    pdf_hash TEXT NOT NULL,
                    pdf_creation_time TEXT NOT NULL,
                    stock_code VARCHAR(12),
                    reporting_date DATE,
                    pdf_download_url TEXT,
                    pdf_local_path TEXT,
                    markdown_file TEXT,
                    preprocessed_file TEXT,
                    digestion_status TEXT NOT NULL DEFAULT 'PENDING',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (pdf_hash, pdf_creation_time)
                )
                """
            )
            cursor.execute(
                "DROP TRIGGER IF EXISTS report_registry_digestion_status_changed ON report_registry"
            )
            cursor.execute(
                "ALTER TABLE report_registry DROP COLUMN IF EXISTS digestion_time"
            )
            cursor.execute(
                "DROP FUNCTION IF EXISTS report_registry_set_digestion_time()"
            )
            cursor.execute(
                "ALTER TABLE report_registry DROP CONSTRAINT IF EXISTS report_registry_digestion_status_check"
            )
        connection.commit()
        self._report_registry_ready = True

    def store_report_profile(
        self,
        pdf_hash: str,
        pdf_creation_time: str,
        markdown_file: str | None,
        preprocessed_file: str | None,
        *,
        stock_code: str | None = None,
        reporting_date: str | None = None,
        pdf_download_url: str | None = None,
        pdf_local_path: str | None = None,
    ) -> None:
        """Upsert report metadata, preserving existing values when fields are absent."""
        if not all(isinstance(value, str) and value.strip()
                   for value in (pdf_hash, pdf_creation_time)):
            raise ValueError("Report registry keys must be nonblank strings")
        if not self.database_url:
            return
        self.ensure_report_registry()
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO report_registry (
                    pdf_hash, pdf_creation_time, stock_code, reporting_date,
                    pdf_download_url, pdf_local_path, markdown_file, preprocessed_file, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (pdf_hash, pdf_creation_time)
                DO UPDATE SET
                    stock_code = COALESCE(EXCLUDED.stock_code, report_registry.stock_code),
                    reporting_date = COALESCE(EXCLUDED.reporting_date, report_registry.reporting_date),
                    pdf_download_url = COALESCE(EXCLUDED.pdf_download_url, report_registry.pdf_download_url),
                    pdf_local_path = COALESCE(EXCLUDED.pdf_local_path, report_registry.pdf_local_path),
                    markdown_file = COALESCE(EXCLUDED.markdown_file, report_registry.markdown_file),
                    preprocessed_file = COALESCE(EXCLUDED.preprocessed_file, report_registry.preprocessed_file),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (pdf_hash, pdf_creation_time, stock_code or None, reporting_date or None,
                 pdf_download_url or None, _registry_path(pdf_local_path),
                 _registry_path(markdown_file), _registry_path(preprocessed_file)),
            )
        connection.commit()

    def get_report_digestion_status(self, pdf_hash: str, pdf_creation_time: str) -> str | None:
        """Look up one PDF version before conversion; None means no registry entry."""
        if not self.database_url or not pdf_hash or not pdf_creation_time:
            return None
        self.ensure_report_registry()
        with self._connect().cursor() as cursor:
            cursor.execute(
                "SELECT digestion_status FROM report_registry WHERE pdf_hash = %s AND pdf_creation_time = %s",
                (pdf_hash, pdf_creation_time),
            )
            row = cursor.fetchone()
        return row[0] if row is not None else None

    def update_digestion_status(
        self,
        pdf_hash: str | None,
        pdf_creation_time: str | None,
        status: str,
    ) -> None:
        """Insert or update a report's status and updated_at timestamp."""
        if not self.database_url or not all(
            isinstance(value, str) and value.strip()
            for value in (pdf_hash, pdf_creation_time)
        ):
            return
        self.ensure_report_registry()
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO report_registry (pdf_hash, pdf_creation_time, digestion_status)
                VALUES (%s, %s, %s)
                ON CONFLICT (pdf_hash, pdf_creation_time)
                DO UPDATE SET
                    digestion_status = EXCLUDED.digestion_status,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (pdf_hash, pdf_creation_time, status),
            )
        connection.commit()

    def store_item(
        self,
        stock_code: str | None,
        reporting_date: str | None,
        item_id: int,
        report_creation_time: str,
        amount: str | float | Decimal,
    ) -> None:
        """Upsert an item, excluding ignored IDs and unconfigured persistence."""
        if item_id <= 0 or item_id == 99000001 or not self.database_url:
            return
        self._ensure_table()
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO balance_sheet_items (
                    stock_code, reporting_date, item_id, report_creation_time,
                    amount
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (stock_code, reporting_date, item_id, report_creation_time)
                DO UPDATE SET
                    amount = EXCLUDED.amount
                """,
                (
                    stock_code, reporting_date, item_id, report_creation_time,
                    str(amount).replace(",", ""),
                ),
            )
        connection.commit()

    def get_report_files(self, pdf_hash: str, pdf_creation_time: str) -> dict | None:
        """Return registered local file paths for exactly one report version."""
        with self._connect().cursor() as cursor:
            cursor.execute(
                "SELECT pdf_local_path, markdown_file, preprocessed_file FROM report_registry "
                "WHERE pdf_hash = %s AND pdf_creation_time = %s",
                (pdf_hash, pdf_creation_time),
            )
            row = cursor.fetchone()
        return dict(zip(("pdf_local_path", "markdown_file", "preprocessed_file"), row)) if row else None

    def get_report_registry(self, page: int = 1, page_size: int = 25) -> dict:
        """Read a stable, newest-first page of registry entries without schema writes."""
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("Page must be positive and page_size must be between 1 and 100")
        fields = (
            "pdf_hash", "pdf_creation_time", "stock_code", "reporting_date",
            "pdf_download_url", "pdf_local_path", "markdown_file", "preprocessed_file",
            "digestion_status", "updated_at",
        )
        with self._connect().cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM report_registry")
            total = cursor.fetchone()[0]
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            cursor.execute(
                """
                SELECT pdf_hash, pdf_creation_time, stock_code, reporting_date,
                       pdf_download_url, pdf_local_path, markdown_file, preprocessed_file,
                       digestion_status, updated_at
                FROM report_registry
                ORDER BY updated_at DESC NULLS LAST, pdf_hash, pdf_creation_time
                LIMIT %s OFFSET %s
                """,
                (page_size, (page - 1) * page_size),
            )
            items = [dict(zip(fields, row)) for row in cursor.fetchall()]
        return dict(items=items, total=total, page=page, page_size=page_size, total_pages=total_pages)

    def get_digestion_pdf_path(self, pdf_hash: str) -> str | None:
        """Resolve a registered PDF by hash, never a client-supplied file path."""
        with self._connect().cursor() as cursor:
            cursor.execute("SELECT local_path FROM report_digestion WHERE pdf_hash = %s", (pdf_hash,))
            row = cursor.fetchone()
        return row[0] if row else None

    def get_downloaded_reports(self) -> list[dict]:
        """Read registered PDFs awaiting extraction without modifying the database."""
        fields = ("pdf_hash", "local_path")
        with self._connect().cursor() as cursor:
            cursor.execute(
                "SELECT pdf_hash, local_path FROM report_digestion "
                "WHERE digestion_stage = %s ORDER BY pdf_hash",
                ("FileOK",),
            )
            return [dict(zip(fields, row)) for row in cursor.fetchall()]

    def get_extracted_reports(self) -> list[dict]:
        """Read completed extraction inputs without modifying the database."""
        fields = ("pdf_hash", "local_path", "extraction_report")
        with self._connect().cursor() as cursor:
            cursor.execute(
                "SELECT pdf_hash, local_path, extraction_report FROM report_digestion "
                "WHERE digestion_stage = %s ORDER BY pdf_hash",
                ("ExtractionSuccess",),
            )
            return [dict(zip(fields, row)) for row in cursor.fetchall()]

    def get_audit_pass_reports(self, pdf_hash: str | None = None) -> list[dict]:
        """Read AuditPass balance sheets, or one explicit hash at any stage."""
        fields = ("pdf_hash", "balance_sheet", "stock_code", "reporting_date", "pdf_creation_date")
        condition = "WHERE digestion_stage = %s "
        parameters = ("AuditPass",)
        if pdf_hash is not None:
            condition = "WHERE pdf_hash = %s "
            parameters = (pdf_hash,)
        with self._connect().cursor() as cursor:
            cursor.execute(
                "SELECT " + ", ".join(fields) + " FROM report_digestion " +
                condition + "ORDER BY pdf_hash", parameters,
            )
            return [dict(zip(fields, row)) for row in cursor.fetchall()]

    def store_line_item_digestion(self, row: dict, report: dict, *, allow_any_stage: bool = False) -> None:
        """Commit validated items, the JSON check report, and the stage together."""
        pdf_hash = row["pdf_hash"]
        if not isinstance(pdf_hash, str) or not pdf_hash.strip():
            raise ValueError("PDF hash must be a nonblank string")
        payload = json.dumps({key: value for key, value in report.items() if key != "items"},
                             ensure_ascii=False, allow_nan=False)
        passed = report["passed"]
        if passed:
            self._ensure_table()
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                if not self._report_digestion_ready:
                    cursor.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
                cursor.execute(
                    "SELECT digestion_stage FROM report_digestion WHERE pdf_hash = %s FOR UPDATE", (pdf_hash,))
                current = cursor.fetchone()
                if current is None:
                    raise ValueError(f"Report {pdf_hash} not found")
                if not allow_any_stage and current[0] != "AuditPass":
                    raise ValueError(f"Report {pdf_hash} is no longer at AuditPass")
                if passed:
                    cursor.executemany(
                        "INSERT INTO balance_sheet_items "
                        "(stock_code, reporting_date, item_id, report_creation_time, amount) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (stock_code, reporting_date, item_id, report_creation_time) "
                        "DO UPDATE SET amount = EXCLUDED.amount",
                        [(row["stock_code"], row["reporting_date"], item["item_id"], row["pdf_creation_date"],
                          item["amount"]) for item in report["items"]],
                    )
                cursor.execute(
                    "UPDATE report_digestion SET bs_digestion_report = %s, digestion_stage = %s, "
                    "updated_at = CURRENT_TIMESTAMP WHERE pdf_hash = %s",
                    (payload, "BalanceSheetOK" if passed else "BalanceSheetErr", pdf_hash),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        self._report_digestion_ready = True

    def get_statement_reports(self, statement: str, *, pdf_hash: str | None = None,
                              retry: bool = False) -> list[dict]:
        """Read queued statements, retrying only the matching error stage when requested."""
        if statement not in STATEMENT_DIGESTION:
            raise ValueError("Unsupported statement")
        fields = ("pdf_hash", statement, "stock_code", "reporting_date", "pdf_creation_date")
        stage = STATEMENT_DIGESTION[statement][2] if retry else STATEMENT_DIGESTION[statement][0]
        condition, value = ("pdf_hash", pdf_hash) if pdf_hash is not None else ("digestion_stage", stage)
        with self._connect().cursor() as cursor:
            cursor.execute("SELECT " + ", ".join(fields) + f" FROM report_digestion WHERE {condition} = %s ORDER BY pdf_hash", (value,))
            return [dict(zip(fields, row)) for row in cursor.fetchall()]

    def store_statement_digestion(self, statement: str, row: dict, report: dict, *, allow_any_stage=False) -> None:
        """Commit one statement's items, validation JSON, and processing stage atomically."""
        if statement not in STATEMENT_DIGESTION:
            raise ValueError("Unsupported statement")
        queued, success, failure, report_field, table = STATEMENT_DIGESTION[statement]
        pdf_hash = row["pdf_hash"]
        if not isinstance(pdf_hash, str) or not pdf_hash.strip():
            raise ValueError("PDF hash must be a nonblank string")
        payload = json.dumps({key: value for key, value in report.items() if key != "items"}, ensure_ascii=False, allow_nan=False)
        passed = report["passed"]
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                if not self._report_digestion_ready:
                    cursor.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
                cursor.execute("SELECT digestion_stage FROM report_digestion WHERE pdf_hash = %s FOR UPDATE", (pdf_hash,))
                current = cursor.fetchone()
                if current is None:
                    raise ValueError(f"Report {pdf_hash} not found")
                if not allow_any_stage and current[0] != queued:
                    raise ValueError(f"Report {pdf_hash} is no longer at {queued}")
                if passed:
                    if not all(row.get(field) for field in ("stock_code", "reporting_date", "pdf_creation_date")):
                        raise ValueError("Missing metadata for statement item storage")
                    cursor.executemany(
                        f"INSERT INTO {table} (stock_code, reporting_date, item_id, report_creation_time, amount, amount_unit) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (stock_code, reporting_date, item_id, report_creation_time) "
                        "DO UPDATE SET amount = EXCLUDED.amount, amount_unit = EXCLUDED.amount_unit",
                        [(row["stock_code"], row["reporting_date"], item["item_id"], row["pdf_creation_date"],
                          item["amount"], item.get("amount_unit", "元")) for item in report["items"]],
                    )
                cursor.execute(f"UPDATE report_digestion SET {report_field} = %s, digestion_stage = %s, "
                               "updated_at = CURRENT_TIMESTAMP WHERE pdf_hash = %s", (payload, success if passed else failure, pdf_hash))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        self._report_digestion_ready = True

    def get_report_digestion_stage(self, pdf_hash: str) -> str | None:
        """Look up the processing stage by content hash before PDF extraction."""
        if not self.database_url:
            return None
        connection = self._connect()
        with connection.cursor() as cursor:
            if not self._report_digestion_ready:
                cursor.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
                connection.commit()
                self._report_digestion_ready = True
            cursor.execute(
                "SELECT digestion_stage FROM report_digestion WHERE pdf_hash = %s",
                (pdf_hash,),
            )
            row = cursor.fetchone()
        return row[0] if row else None

    def update_report_digestion_stage(self, pdf_hash: str, stage: ReportDigestionStage) -> dict | None:
        """Manually update a report's stage, preserving all extracted content."""
        if stage not in get_args(ReportDigestionStage):
            raise ValueError("Invalid report digestion stage")
        connection = self._connect()
        with connection.cursor() as cursor:
            if not self._report_digestion_ready:
                cursor.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            cursor.execute(
                "UPDATE report_digestion SET digestion_stage = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE pdf_hash = %s RETURNING pdf_hash, digestion_stage, updated_at",
                (stage, pdf_hash),
            )
            row = cursor.fetchone()
        connection.commit()
        self._report_digestion_ready = True
        return dict(zip(("pdf_hash", "digestion_stage", "updated_at"), row)) if row else None

    def store_extraction_audit(self, pdf_hash: str, issues: list[str], statements: dict) -> None:
        """Save audit details and the resulting stage together for one report."""
        if not isinstance(pdf_hash, str) or not pdf_hash.strip():
            raise ValueError("PDF hash must be a nonblank string")
        payload = json.dumps({"issues": issues, "statements": statements}, ensure_ascii=False)
        stage = "AuditIssues" if issues else "AuditPass"
        connection = self._connect()
        with connection.cursor() as cursor:
            if not self._report_digestion_ready:
                cursor.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            cursor.execute(
                "UPDATE report_digestion SET extraction_audit = %s, digestion_stage = %s, "
                "updated_at = CURRENT_TIMESTAMP WHERE pdf_hash = %s",
                (payload, stage, pdf_hash),
            )
        connection.commit()
        self._report_digestion_ready = True

    def get_report_digestion(self, page: int = 1, page_size: int = 25, *, show_passed: bool = False,
                             only_manual_pass: bool = False, report_group: str | None = None,
                             pdf_hash: str | None = None, stock_code: str | None = None,
                             digestion_stage: str | None = None) -> dict:
        """Read a filtered, newest-first page without schema writes."""
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("Page must be positive and page_size must be between 1 and 100")
        if report_group not in (None, "InProgress", "ExtractionSuccess", "ExtractionError", "AuditPass", "AuditIssues",
                                "BalanceSheetErr", "IncomeStatementErr", "CashFlowStatementErr", "CashFlowStatementOK",
                                "ManualPass", "Deprecated", "Search"):
            raise ValueError("Unknown report group")
        if digestion_stage is not None and digestion_stage not in get_args(ReportDigestionStage):
            raise ValueError("Invalid report digestion stage")
        fields = (
            "pdf_hash", "pdf_creation_date", "download_url", "local_path", "download_report", "digestion_stage",
            "stock_code", "reporting_date", "extraction_report",
            "balance_sheet", "balance_sheet_ex", "income_statement", "income_statement_ex",
            "cash_flow_statement", "cash_flow_statement_ex", "extraction_audit",
            "bs_digestion_report", "is_digestion_report", "cfs_digestion_report", "updated_at",
        )
        where = "" if show_passed else "WHERE digestion_stage NOT IN ('ManualPass', 'AuditPass') "
        if only_manual_pass:
            where = "WHERE digestion_stage = 'ManualPass' "
        if report_group == "InProgress":
            where = ("WHERE digestion_stage NOT IN ('ManualPass', 'AuditPass', 'Deprecated', 'ExtractionSuccess', "
                     "'ExtractionError', 'AuditIssues', 'BalanceSheetErr', 'IncomeStatementErr', 'CashFlowStatementErr', "
                     "'CashFlowStatementOK') ")
        elif report_group in ("ExtractionSuccess", "ExtractionError", "AuditPass", "AuditIssues",
                              "BalanceSheetErr", "IncomeStatementErr", "CashFlowStatementErr", "CashFlowStatementOK",
                              "ManualPass", "Deprecated"):
            where = "WHERE digestion_stage = %s "
        parameters = (report_group,) if report_group in ("ExtractionSuccess", "ExtractionError", "AuditPass", "AuditIssues",
                                                        "BalanceSheetErr", "IncomeStatementErr", "CashFlowStatementErr", "CashFlowStatementOK",
                                                        "ManualPass", "Deprecated") else ()
        if report_group == "Search":
            where = ""
        for column, value in (("pdf_hash", pdf_hash), ("stock_code", stock_code), ("digestion_stage", digestion_stage)):
            if value and value.strip():
                where += ("AND " if where else "WHERE ") + column + " = %s "
                parameters += (value.strip(),)
        with self._connect().cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM report_digestion " + where, parameters)
            total = cursor.fetchone()[0]
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            cursor.execute(
                "SELECT " + ", ".join(fields) + " FROM report_digestion " + where +
                "ORDER BY updated_at DESC NULLS LAST, pdf_hash LIMIT %s OFFSET %s",
                parameters + (page_size, (page - 1) * page_size),
            )
            items = [dict(zip(fields, row)) for row in cursor.fetchall()]
        return dict(items=items, total=total, page=page, page_size=page_size, total_pages=total_pages)

    def get_balance_sheet_stock_codes(self) -> list[dict]:
        return self.get_statement_stock_codes("balance_sheet")

    @staticmethod
    def _statement_table(statement: str) -> str:
        if statement not in ("balance_sheet", "income_statement", "cash_flow_statement"):
            raise ValueError("Unsupported statement")
        return statement + "_items"

    def get_statement_stock_codes(self, statement: str) -> list[dict]:
        """Return stock names and counts without excluding stocks missing metadata."""
        table = self._statement_table(statement)
        with self._connect().cursor() as cursor:
            cursor.execute(
                "SELECT stocks.stock_code, stocks.report_count, metadata.stock_name, stocks.reporting_dates FROM ("
                "SELECT stock_code, COUNT(DISTINCT (reporting_date, report_creation_time)) "
                f"AS report_count, ARRAY_AGG(DISTINCT reporting_date) AS reporting_dates FROM {table} "
                "WHERE stock_code IS NOT NULL AND stock_code <> '' "
                "GROUP BY stock_code) AS stocks "
                "LEFT JOIN stock_metadata AS metadata ON metadata.stock_code = stocks.stock_code "
                "ORDER BY stocks.stock_code"
            )
            return [{"stock_code": row[0], "report_count": row[1], "stock_name": row[2],
                     "has_missing_reporting_dates": _has_missing_quarters(row[3])}
                    for row in cursor.fetchall()]

    def get_statement_items(self, statement: str, stock_code: str) -> list[dict]:
        """Read statement values, preserving EPS units and exact report versions."""
        table = self._statement_table(statement)
        fields = ("reporting_date", "item_id", "amount", "report_creation_time", "amount_unit")
        with self._connect().cursor() as cursor:
            cursor.execute(
                f"SELECT {', '.join(fields)} FROM {table} WHERE stock_code = %s "
                "ORDER BY reporting_date, item_id, report_creation_time", (stock_code,),
            )
            return [dict(zip(fields, row)) for row in cursor.fetchall()]

    def get_balance_sheet_items(self, stock_code: str) -> list[dict]:
        """Return the same ordered fields used by the balance-sheet API."""
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT reporting_date, item_id, amount, report_creation_time
                FROM balance_sheet_items
                WHERE stock_code = %s
                ORDER BY reporting_date, item_id, report_creation_time
                """,
                (stock_code,),
            )
            rows = cursor.fetchall()
        return [dict(zip(("reporting_date", "item_id", "amount", "report_creation_time"), row)) for row in rows]

    def get_balance_sheet_source(self, stock_code, reporting_date, item_id, report_creation_time):
        """Resolve the exact displayed report version and its registered PDF."""
        fields = ("pdf_hash", "local_path", "extraction_report", "balance_sheet", "amount", "report_creation_time", "download_url")
        with self._connect().cursor() as cursor:
            cursor.execute(
                "SELECT r.pdf_hash, r.local_path, r.extraction_report, r.balance_sheet, b.amount, b.report_creation_time, r.download_url "
                "FROM balance_sheet_items b JOIN report_digestion r "
                "ON r.stock_code = b.stock_code AND r.reporting_date = b.reporting_date "
                "AND r.pdf_creation_date = b.report_creation_time "
                "WHERE b.stock_code = %s AND b.reporting_date = %s AND b.item_id = %s "
                "AND b.report_creation_time = %s "
                "ORDER BY r.updated_at DESC, r.pdf_hash LIMIT 2",
                (stock_code, reporting_date, item_id, report_creation_time),
            )
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise ValueError("Multiple PDFs match this report version; source verification is ambiguous")
        return dict(zip(fields, rows[0])) if rows else None

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._table_ready = False
        self._report_registry_ready = False
        self._report_digestion_ready = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
