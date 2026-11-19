-- PostgreSQL schema for the report download, extraction, and digestion pipeline.
-- This creates a separate table; it does not migrate or modify report_registry.
CREATE TABLE IF NOT EXISTS report_digestion (
    pdf_hash TEXT PRIMARY KEY CHECK (btrim(pdf_hash) <> ''),
    -- Keep the original PDF metadata date, including its format/time-zone suffix.
    pdf_creation_date TEXT,
    download_url TEXT,
    local_path TEXT,

    digestion_stage TEXT NOT NULL DEFAULT 'Downloaded'
        CHECK (digestion_stage IN (
            'Downloaded',
            'PreviewSuccess',
            'PreviewError',
            'ExtractionSuccess',
            'ExtractionError',
            'AuditPass',
            'AuditIssues',
            'ManualPass',
            'Deprecated',
            'NoStatements',
            'DigestionSuccess',
            'DigestionError'
        )),

    stock_code VARCHAR(12),
    reporting_date DATE,

    extraction_report TEXT,
    balance_sheet TEXT,
    balance_sheet_ex TEXT,
    income_statement TEXT,
    income_statement_ex TEXT,
    cash_flow_statement TEXT,
    cash_flow_statement_ex TEXT,
    extraction_audit TEXT,
    -- JSON-serialized digestion results.
    digestion_report TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS report_digestion_pdf_hash_digestion_stage_idx
    ON report_digestion (pdf_hash, digestion_stage);

CREATE INDEX IF NOT EXISTS report_digestion_stock_code_idx
    ON report_digestion (stock_code);

-- Upgrade existing tables without changing any stored extraction data.
ALTER TABLE report_digestion ADD COLUMN IF NOT EXISTS extraction_audit TEXT;
ALTER TABLE report_digestion ADD COLUMN IF NOT EXISTS digestion_report TEXT;
ALTER TABLE report_digestion DROP COLUMN IF EXISTS stock_name;

-- Rename the manual approval stage before enforcing the new allowed values.
ALTER TABLE report_digestion DROP CONSTRAINT IF EXISTS report_digestion_digestion_stage_check;
UPDATE report_digestion SET digestion_stage = 'ManualPass'
    WHERE digestion_stage = 'HandPass';
ALTER TABLE report_digestion ADD CONSTRAINT report_digestion_digestion_stage_check
    CHECK (digestion_stage IN (
        'Downloaded', 'PreviewSuccess', 'PreviewError',
        'ExtractionSuccess', 'ExtractionError',
        'AuditPass', 'AuditIssues', 'ManualPass',
        'Deprecated', 'NoStatements',
        'DigestionSuccess', 'DigestionError'
    ));

-- Preserve existing extraction content when upgrading an older installation.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'report_digestion'::regclass
          AND attname = 'extraction_info' AND NOT attisdropped
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'report_digestion'::regclass
          AND attname = 'extraction_report' AND NOT attisdropped
    ) THEN
        ALTER TABLE report_digestion RENAME COLUMN extraction_info TO extraction_report;
    END IF;
END $$;

-- Company details keyed by stock code; metadata may be filled in incrementally.
CREATE TABLE IF NOT EXISTS stock_metadata (
    stock_code VARCHAR(12) PRIMARY KEY CHECK (btrim(stock_code) <> ''),
    stock_name VARCHAR(16) NOT NULL CHECK (btrim(stock_name) <> ''),
    company_name TEXT,
    company_homepage TEXT,
    company_profile TEXT
);
