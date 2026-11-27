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

-- PostgreSQL schema for the report download, extraction, and digestion pipeline.
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
            'AuditOK',
            'AuditErr',
            'Deprecated',
            'NoStatements',
            'BalanceOK',
            'BalanceErr',
            'IncomeOK',
            'IncomeErr',
            'CashFlowOK',
            'CashFlowErr',
            
            'ExExtractOK',
            'ExExtractErr',
            'ExAuditOK',
            'ExAuditErr',
            'ExBalanceOK',
            'ExBalanceErr',
            'ExIncomeOK',
            'ExIncomeErr',
            'ExCashFlowOK',
            'ExCashFlowErr'
        )),

    stock_code VARCHAR(12),
    reporting_date DATE,

    extraction_report TEXT,
    extraction_report_ex TEXT,
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


-- Extracted balance-sheet line items. Monetary values are stored in yuan.
CREATE TABLE IF NOT EXISTS balance_sheet_items (
    stock_code VARCHAR(12) NOT NULL,
    reporting_date DATE NOT NULL,
    item_id INTEGER NOT NULL,
    report_creation_time TIMESTAMP NOT NULL,
    amount NUMERIC(20, 2) NOT NULL,
    UNIQUE (stock_code, reporting_date, item_id, report_creation_time)
);

-- Monetary statement values are normalized to yuan; EPS remains yuan per share.
CREATE TABLE IF NOT EXISTS income_statement_items (
    stock_code VARCHAR(12) NOT NULL,
    reporting_date DATE NOT NULL,
    item_id INTEGER NOT NULL,
    report_creation_time TIMESTAMP NOT NULL,
    amount NUMERIC(28, 8) NOT NULL,
    UNIQUE (stock_code, reporting_date, item_id, report_creation_time)
);

CREATE TABLE IF NOT EXISTS cash_flow_statement_items (
    stock_code VARCHAR(12) NOT NULL,
    reporting_date DATE NOT NULL,
    item_id INTEGER NOT NULL,
    report_creation_time TIMESTAMP NOT NULL,
    amount NUMERIC(28, 8) NOT NULL,
    UNIQUE (stock_code, reporting_date, item_id, report_creation_time)
);
