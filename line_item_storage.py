"""PostgreSQL persistence for extracted financial statement line items."""

import importlib
import os
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Self

from dotenv import load_dotenv


# Resolve beside the program, independently of the working directory.
load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)


def _registry_path(value: str | None) -> str | None:
    """Store local file paths relative to the program's working directory."""
    return os.path.relpath(Path(value).resolve()) if value else None


class LineItemStorage:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self._connection = None
        self._table_ready = False
        self._report_registry_ready = False

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
                    item_english_name TEXT NOT NULL,
                    UNIQUE (stock_code, reporting_date, item_id, report_creation_time)
                )
                """
            )
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
                    digestion_status TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (digestion_status IN ('PENDING', 'ERROR', 'DONE')),
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

    def update_digestion_status(
        self,
        pdf_hash: str | None,
        pdf_creation_time: str | None,
        status: str,
    ) -> None:
        """Insert or update a report's status and updated_at timestamp."""
        if status not in {"PENDING", "ERROR", "DONE"}:
            raise ValueError(f"Invalid digestion status: {status}")
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
        item_english_name: str,
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
                    amount, item_english_name
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_code, reporting_date, item_id, report_creation_time)
                DO UPDATE SET
                    amount = EXCLUDED.amount,
                    item_english_name = EXCLUDED.item_english_name
                """,
                (
                    stock_code, reporting_date, item_id, report_creation_time,
                    str(amount).replace(",", ""), item_english_name,
                ),
            )
        connection.commit()

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

    def get_balance_sheet_items(self, stock_code: str) -> list[dict]:
        """Return the same ordered fields used by the balance-sheet API."""
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT reporting_date, item_id, amount
                FROM balance_sheet_items
                WHERE stock_code = %s
                ORDER BY reporting_date, item_id, report_creation_time
                """,
                (stock_code,),
            )
            rows = cursor.fetchall()
        return [dict(zip(("reporting_date", "item_id", "amount"), row)) for row in rows]

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._table_ready = False
        self._report_registry_ready = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
