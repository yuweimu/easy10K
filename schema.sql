-- PostgreSQL schema for the report download, extraction, and digestion pipeline.
-- This creates a separate table; it does not migrate or modify report_registry.
CREATE TABLE IF NOT EXISTS report_digestion (
    pdf_hash TEXT PRIMARY KEY CHECK (btrim(pdf_hash) <> ''),
    -- Normalized PDF metadata creation timestamp.
    pdf_creation_date TIMESTAMPTZ,
    download_url TEXT,
    local_path TEXT,
    download_report TEXT,

    digestion_stage TEXT NOT NULL DEFAULT 'FileOK'
        CHECK (digestion_stage IN (
            'FileOK',
            'FileErr',
            'ExtractOK',
            'ExtractErr',
            'AuditPass',
            'AuditIssues',
            'ManualPass',
            'Deprecated',
            'NoStatements',
            'BalanceOK',
            'BalanceErr',
            'IncomeOK',
            'IncomeErr',
            'CashFlowOK',
            'CashFlowErr'
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
    -- JSON-serialized digestion results for each financial statement.
    bs_digestion_report TEXT,
    is_digestion_report TEXT,
    cfs_digestion_report TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS report_digestion_pdf_hash_digestion_stage_idx
    ON report_digestion (pdf_hash, digestion_stage);

CREATE INDEX IF NOT EXISTS report_digestion_stock_code_idx
    ON report_digestion (stock_code);

-- Upgrade existing tables while preserving parseable PDF metadata.
ALTER TABLE report_digestion ADD COLUMN IF NOT EXISTS extraction_audit TEXT;
ALTER TABLE report_digestion ADD COLUMN IF NOT EXISTS download_report TEXT;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'report_digestion'
          AND column_name = 'pdf_creation_date'
          AND data_type = 'text'
    ) THEN
        ALTER TABLE report_digestion
            ALTER COLUMN pdf_creation_date TYPE TIMESTAMPTZ
            USING CASE
                WHEN pdf_creation_date ~ '^D:[0-9]{14}$'
                    THEN to_timestamp(substring(pdf_creation_date FROM 3), 'YYYYMMDDHH24MISS')
                WHEN pdf_creation_date ~ '^[0-9]{14}$'
                    THEN to_timestamp(pdf_creation_date, 'YYYYMMDDHH24MISS')
                ELSE NULL
            END;
    END IF;
END $$;
-- Preserve balance-sheet checks when upgrading the former shared report column.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'report_digestion'::regclass
          AND attname = 'digestion_report' AND NOT attisdropped
    ) THEN
        IF EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = 'report_digestion'::regclass
              AND attname = 'bs_digestion_report' AND NOT attisdropped
        ) THEN
            UPDATE report_digestion
                SET bs_digestion_report = COALESCE(bs_digestion_report, digestion_report);
            ALTER TABLE report_digestion DROP COLUMN digestion_report;
        ELSE
            ALTER TABLE report_digestion RENAME COLUMN digestion_report TO bs_digestion_report;
        END IF;
    END IF;
END $$;
ALTER TABLE report_digestion ADD COLUMN IF NOT EXISTS bs_digestion_report TEXT;
ALTER TABLE report_digestion ADD COLUMN IF NOT EXISTS is_digestion_report TEXT;
ALTER TABLE report_digestion ADD COLUMN IF NOT EXISTS cfs_digestion_report TEXT;
ALTER TABLE report_digestion DROP COLUMN IF EXISTS stock_name;

-- Rename legacy stages before enforcing the new allowed values.
ALTER TABLE report_digestion DROP CONSTRAINT IF EXISTS report_digestion_digestion_stage_check;
UPDATE report_digestion SET digestion_stage = 'FileOK'
    WHERE digestion_stage IN ('Downloaded', 'PreviewSuccess');
UPDATE report_digestion SET digestion_stage = 'FileErr'
    WHERE digestion_stage = 'PreviewError';
UPDATE report_digestion SET digestion_stage = 'ManualPass'
    WHERE digestion_stage = 'HandPass';
UPDATE report_digestion SET digestion_stage = CASE digestion_stage
        WHEN 'DigestionSuccess' THEN 'BalanceOK'
        WHEN 'DigestionError' THEN 'BalanceErr'
    END
    WHERE digestion_stage IN ('DigestionSuccess', 'DigestionError');
UPDATE report_digestion SET digestion_stage = CASE digestion_stage
        WHEN 'ExtractionSuccess' THEN 'ExtractOK'
        WHEN 'ExtractionError' THEN 'ExtractErr'
        WHEN 'BalanceSheetOK' THEN 'BalanceOK'
        WHEN 'BalanceSheetErr' THEN 'BalanceErr'
        WHEN 'IncomStatementOK' THEN 'IncomeOK'
        WHEN 'IncomeStatementErr' THEN 'IncomeErr'
        WHEN 'CashFlowStatementOK' THEN 'CashFlowOK'
        WHEN 'CashFlowStatementErr' THEN 'CashFlowErr'
    END
    WHERE digestion_stage IN (
        'ExtractionSuccess', 'ExtractionError', 'BalanceSheetOK', 'BalanceSheetErr',
        'IncomStatementOK', 'IncomeStatementErr', 'CashFlowStatementOK', 'CashFlowStatementErr'
    );
ALTER TABLE report_digestion ADD CONSTRAINT report_digestion_digestion_stage_check
    CHECK (digestion_stage IN (
        'FileOK', 'FileErr',
        'ExtractOK', 'ExtractErr',
        'AuditPass', 'AuditIssues', 'ManualPass',
        'Deprecated', 'NoStatements',
        'BalanceOK', 'BalanceErr',
        'IncomeOK', 'IncomeErr',
        'CashFlowOK', 'CashFlowErr'
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

-- Monetary statement values are normalized to yuan; EPS remains yuan per share.
CREATE TABLE IF NOT EXISTS income_statement_items (
    stock_code VARCHAR(12) NOT NULL,
    reporting_date DATE NOT NULL,
    item_id INTEGER NOT NULL,
    report_creation_time TEXT NOT NULL,
    amount NUMERIC(28, 8) NOT NULL,
    amount_unit TEXT NOT NULL CHECK (amount_unit IN ('元', '元/股')),
    UNIQUE (stock_code, reporting_date, item_id, report_creation_time)
);

CREATE TABLE IF NOT EXISTS cash_flow_statement_items (
    stock_code VARCHAR(12) NOT NULL,
    reporting_date DATE NOT NULL,
    item_id INTEGER NOT NULL,
    report_creation_time TEXT NOT NULL,
    amount NUMERIC(28, 8) NOT NULL,
    amount_unit TEXT NOT NULL DEFAULT '元' CHECK (amount_unit = '元'),
    UNIQUE (stock_code, reporting_date, item_id, report_creation_time)
);

-- Company details keyed by stock code; metadata may be filled in incrementally.
CREATE TABLE IF NOT EXISTS stock_metadata (
    stock_code VARCHAR(12) PRIMARY KEY CHECK (btrim(stock_code) <> ''),
    stock_name VARCHAR(16) NOT NULL CHECK (btrim(stock_name) <> ''),
    enrollment TEXT NOT NULL DEFAULT 'Pending'
        CHECK (enrollment IN ('Pending', 'InProgress', 'Error', 'Done')),
    company_name TEXT,
    company_homepage TEXT,
    company_profile TEXT
);

ALTER TABLE stock_metadata
    ADD COLUMN IF NOT EXISTS enrollment TEXT NOT NULL DEFAULT 'Pending'
        CHECK (enrollment IN ('Pending', 'InProgress', 'Error', 'Done'));

ALTER TABLE stock_metadata
    ALTER COLUMN enrollment SET DEFAULT 'Pending';

ALTER TABLE stock_metadata
    DROP CONSTRAINT IF EXISTS stock_metadata_enrollment_check,
    ADD CONSTRAINT stock_metadata_enrollment_check
        CHECK (enrollment IN ('Pending', 'InProgress', 'Error', 'Done'));
