# Easy10K

Convert financial-report PDFs into Markdown, extract consolidated balance-sheet data into PostgreSQL, and browse reports alongside a bilingual financial dashboard.

## Features

- Recursive PDF conversion with page markers, creation-date metadata, and reusable PDF/heading analysis.
- Existing Markdown files are skipped unless `--force` is supplied. Conversion writes a temporary file and publishes the final file only after success.
- Report preprocessing adds stable section anchors, clickable contents entries, and hidden navigation metadata.
- Balance-sheet names are indented using the hierarchy in [line-item-chart.txt](./line-item-chart.txt); numeric columns are right-aligned and monospaced.
- PostgreSQL storage upserts extracted items and skips ignored item ID `99000001`.
- React dashboard with Chinese/English labels, recent searches, trends, and a draggable/resizable report viewer.
- HTML report viewer with a draggable, collapsible navigation panel, page indicator, and persistent custom scrollbar.

## Requirements

- Python and the packages in [requirements.txt](./requirements.txt). The current Docker image uses Python 3.15.2.
- Node.js compatible with the installed Vite version; the current installation requires Node 20.19+ on the 20.x line, or 22.12+.
- PostgreSQL for storing and querying extracted financial data. The Compose configuration uses PostgreSQL 17.
- Docker with Compose if using the container setup.

Run the following commands from the project root unless otherwise noted.

## Local setup

Create a Python environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start the development database:

```bash
docker compose up -d postgres
```

Create a `.env` file in the project root containing:

```dotenv
DATABASE_URL=postgresql://easy10k:easy10k@localhost:5432/easy10k
```

The storage module automatically loads this file when imported, independently of the working directory. Existing environment variables take precedence. The credentials above match the local Compose configuration; do not use them for a public deployment.

Without `DATABASE_URL`, digestion can parse reports but does not persist items. The balance-sheet query endpoint returns HTTP 503 when the database is not configured or cannot be queried.

## Prepare reports

### Download reports for a stock list

```bash
python pdf_download_runner.py
```

[pdf_download_runner.py](./pdf_download_runner.py) reads the first whitespace-separated
field of each line in [stock_list.txt](./stock_list.txt) as the stock code. It runs
stocks sequentially using [pdf_download.py](./pdf_download.py), retaining its
random 1–5 second delay between PDF downloads by default. Use `--delay MIN,MAX`
to customize the range on either script, for example:

```bash
python pdf_download.py 600519 --delay 2,8
python pdf_download_runner.py --delay 2,8
```

The range must satisfy `0 <= MIN <= MAX`; equal bounds give a fixed
delay. Successful lines receive a trailing
`# DOWNLOADED` marker, saved atomically after each completed stock. Blank lines
and comments are ignored; `# DOWNLOADED` lines are skipped. Existing
`reports/download/{stock_code}` directories are reused, and already downloaded PDFs are
skipped individually by the downloader. Duplicate successful entries are also marked.

Announcements without a `下载公告` link are expected skips (`NO_DOWNLOAD_LINK`)
and do not prevent marking a stock as downloaded, even if all its announcements
have no attachment. Network errors, failed PDF transfers, and empty download
batches receive a trailing `# ERROR`, saved atomically, and the runner continues
to the next stock. Existing error markers are replaced rather than duplicated;
successful retries replace `# ERROR` with `# DOWNLOADED`. Error-marked lines are
skipped by default; pass `--retry-errors` to retry them:

```bash
python pdf_download_runner.py --retry-errors
```

Unmarked lines are still processed when this flag is supplied. Existing
directories do not prevent retries. Only the current attempt determines whether
the line is marked successful; errors from earlier runs remain in the JSON history
without preventing a successful retry.
Download details and errors remain in each stock directory's `downloads.json`.

Optional arguments: `--stock-list PATH`, `--reports-dir PATH`, `--timeout SECONDS`,
`--retry-errors`, and `--delay MIN,MAX` (default: `1,5`).
Defaults are relative to the project directory, not the current working directory.
The runner exits nonzero if any stock fails or a stock-code field is invalid.

### Digest audited balance-sheet line items

Run `python digest_bs.py` with `DATABASE_URL` configured to process reports
at `AuditPass`. To digest one report regardless of its current stage, run
`python digest_bs.py --hash 007b7f4055bbfeedac1e0742338e0f00`.
It reads the first amount column of `balance_sheet` Markdown
(skipping a notes column), validates required nonzero items and parent totals,
and converts the displayed currency unit to yuan before saving line items.
Each report's items, JSON `bs_digestion_report`, and `BalanceOK` stage are
committed together. Failed checks save their details at `BalanceErr` without
inserting items; the batch continues with the next report and exits with code 1.

Totals use the greater of 0.01 displayed units and 0.1% of the compared amounts.
Override these with `--absolute-tolerance` and `--relative-tolerance`.
The equity checks compare item 31 with 3110 plus 3111, and item 3110 with its
capital/reserve/retained-earnings components (`311001`–`311009`), subtracting
treasury shares (`311003`). Their IDs and indentation place them under `3110`.
This avoids counting the parent-company equity subtotal twice. Unknown labels
are recorded in `unmapped_rows` for review and cause `BalanceErr`; successful
digestion requires an empty `unmapped_rows` list.

### Digest income statements and cash-flow statements

Run these from the project root with `DATABASE_URL` configured:

```bash
python digest_is.py
python digest_cf.py
```

Income digestion reads reports at `BalanceOK` and sets `IncomeOK`
or `IncomeErr`. Cash-flow digestion reads reports at `IncomeOK`
and sets `CashFlowOK` or `CashFlowErr`. Both support
`--hash PDF_HASH` to process a report regardless of stage and `--dry-run` to
validate without database writes. Each batch processes all queued reports and
exits with code 1 if any report fails.

Income required IDs are `51`, `54`, and `56`: operating profit, profit before
tax, and net profit. Checks reconcile revenue/cost subtotals, operating profit,
tax, net-profit classifications, other comprehensive income, and comprehensive
income attribution. Disclosure-only details such as finance-cost interest and
investment-income components are not assumed to be exhaustive subtotals.

Cash-flow required IDs are `81`, `82`, `83`, `85`, `86`, and `87`. Checks reconcile
each activity's receipt/payment subtotals and net flows, exchange effects, the
net cash increase, and opening-to-closing cash balances. Minority-interest
`其中` disclosures are included within their parent amounts rather than added
again. Opening and closing cash balances cannot be negative. Legitimate zero
profits, losses, and zero or negative net cash flows remain valid.

Both parsers handle notes columns, truncated labels, blank amounts, and `/` or
`-` as zero. They test ambiguous prior-period amounts only when column-shift
evidence exists, accept a unique omission that reconciles without breaking
passing equations, and record the adjustment. Unmapped labels, invalid amounts,
missing metadata, and failed equations prevent success. Equations print to stdout
with Chinese names and item IDs. Tolerances default to the greater of 0.01
displayed units and 0.01% of compared amounts; override them with
`--absolute-tolerance` and `--relative-tolerance`.

Successful items are saved to `income_statement_items` or
`cash_flow_statement_items`, with amounts normalized to yuan. EPS remains yuan
per share without currency-unit scaling. Each report's items, stage, and JSON
`is_digestion_report` or `cfs_digestion_report` are committed together; the JSON
excludes the item list. Failed validation saves its report and error stage
without inserting items. The storage writer initializes the new tables from
`schema.sql` on its first write.

### Audit direct PDF statement extraction

To extract registered reports at the `FileOK` stage with
[extract.py](./extract.py), configure `DATABASE_URL` and run:

```bash
python extract.py
python extract.py -o extracted/pdf
```

Inputs come from `report_digestion.local_path` for rows whose stage is exactly
`FileOK`; no PDF path argument is required. Other stages are left unchanged.
Each file's MD5 hash must match its registered `pdf_hash` before results are saved.
Output folders preserve paths relative to the PDFs' common parent directory,
keeping reports with identical names in different folders separate.
The extractor also reads report-title periods from the first three pages
(`年年度`, `第一季度`, `第三季度`, and `半年度`). Extraction JSON keeps
`title_reporting_date`, `balance_sheet_reporting_date`, and
`reporting_dates_match` (true/false when both sources exist, otherwise null).
The balance-sheet date takes priority; the title date is used when it is absent.
Disagreement between the two sources is flagged by the extraction audit.
Statement headings are accepted only when one of the next five text lines
across the current and next page (before another statement heading)
contains the statement's reporting period/date, a unit or currency label, or
the preparer (`编制单位`). This rejects headings without nearby statement metadata.
Without `-o`, the extractor prints progress and statement summaries.
 A failed PDF is reported on stderr and marked `ExtractErr` while the
remaining PDFs continue, with exit code 1 if any report fails. Successful reports
 are saved at `ExtractOK`.

Use [audit_extraction.py](./audit_extraction.py) to screen metadata, table
structure, page spans, and decimal-value coverage. By default it reads
`report_digestion` rows where `digestion_stage = 'ExtractOK'` using
`DATABASE_URL` from the environment or `.env`. It audits the stored
`extraction_report` JSON without opening PDFs. For each report it saves the
`issues` and `statements` fields as JSON in `extraction_audit`, and sets
`digestion_stage` to `AuditPass` when issues are empty or `AuditIssues` otherwise.
These updates are committed together per report. Previously audited rows are
excluded from subsequent runs unless their stage returns to `ExtractOK`.
On the report digestion page, select a stage beneath the current stage badge
and click **Save** to update it manually. Setting `ExtractOK` makes the
report eligible for another audit. Stage changes preserve extraction and audit
content; the audit is displayed only at `AuditPass` or `AuditIssues`.
Use `ManualPass` to record a human's manual pass decision; it has a success badge
and is preserved during extraction reruns.
The table has six tabs: **InProgress** (all stages except `ExtractOK`,
`AuditPass`, `ManualPass`, and `Deprecated`), **ExtractOK**, **AuditPass**,
**ManualPass**, **Deprecated**, and **Search**. Search covers all stages and lets
you filter by full PDF hash, exact stock code, and/or stage. Combined filters must
all match. Clear resets the filters. It opens on **InProgress**;
switching tabs resets to page one, with totals and pagination for that tab.
The output `audit.json` includes each row's `pdf_hash`; malformed extractions
are recorded without stopping the batch.

```bash
python audit_extraction.py
python audit_extraction.py --input-dir reports --output-dir extracted/pdf
```

Pass `--input-dir` to recursively extract and audit local PDFs instead.
The default output is anchored to the project directory, so the script can be
launched from another working directory. In directory mode, each PDF gets a mirrored output subdirectory
containing `extraction_report.json` and separate statement Markdown files. Generated files
are replaced on reruns, and `audit.json` is updated after every report. A failed
file is recorded without stopping the batch.

In directory mode, use `--cached` to audit saved extractions without reconverting PDFs that already
have `extraction_report.json`. Cached mode does not check source freshness; omit it after
changing PDFs or extraction logic. Use `--fail-on-issues` to return exit code 1
when any report is flagged (for automation). By default, audit findings do not
cause a nonzero exit code. An empty input directory produces an empty audit.

These checks flag suspicious results; they do not certify every financial value
or replace a visual review of source statements.

## Run the application

Start the API in one terminal, with the Python environment activated:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Start the frontend in another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open [the dashboard](http://localhost:5173/) and search for a stock code. Vite proxies `/api/...` to the backend on port 8000, removing the `/api` prefix.

The report API serves `preprocessed/{stock_code}/{reporting_date}.md` as HTML. For example:

- [Direct report endpoint](http://localhost:8000/reports/300750/2025-12-31)
- [Report through the frontend proxy](http://localhost:5173/api/reports/300750/2025-12-31)
- [Interactive API documentation](http://localhost:8000/docs)

Report viewing does not require a database, but the matching preprocessed file must exist. Navigation is hidden when valid navigation metadata is absent.

### API endpoints

| Method | Backend path | Purpose |
| --- | --- | --- |
| GET | `/reports/{stock_code}/{reporting_date}` | Render a preprocessed report as HTML. |
| GET | `/line-item-chart` | Return item IDs and Chinese/English names. |
| GET | `/balance_sheet_items/{stock_code}` | Query stored financial items. |
| GET | `/health` | Basic application health response. |
| POST | `/submit/` | Example text-submission endpoint. |

### Docker setup

The [Compose configuration](./docker-compose.yml) starts PostgreSQL, runs the digest job, then starts the API after the digest job succeeds:

```bash
docker compose up --build
```

The digest service currently targets `/data/reports/dongpeng-2026q3.md`, mounted from `reports/dongpeng-2026q3.md`. Ensure that file exists or update the service's command for your own input before starting the full stack.

The API image copies `markdown`, `preprocessed`, and the report-viewer assets at build time. Generate the reports before building and rebuild the image after changing them. The Compose file does not run the frontend; start it separately as described above. Database data persists in the `postgres_data` volume.

## Tests and frontend build

```bash
python -m unittest discover -s tests
```

Tests cover parsing, storage, preprocessing, recursive conversion, overwrite protection, temporary-file publishing, and viewer behavior. PDF conversion and database access are mocked in focused tests; JavaScript checks require Node.js.

Build the frontend:

```bash
cd frontend
npm run build
```

`VITE_API_BASE_URL` configures frontend data requests before building. The embedded report URL currently uses `/api/reports/...`, so a production deployment must also provide a matching reverse proxy. Vite's development proxy is not included in the production build.

## Main components

| File / directory | Responsibility |
| --- | --- |
| [line_item_chart.py](./line_item_chart.py) | Load the bilingual item hierarchy. |
| [line_item_storage.py](./line_item_storage.py) | Environment loading and PostgreSQL persistence. |
| [main.py](./main.py) | FastAPI endpoints. |
| [report_renderer.py](./report_renderer.py) | Markdown rendering and viewer security headers. |
| [report_view/](./report_view/) | Report HTML, CSS, and JavaScript. |
| [frontend/](./frontend/) | React dashboard. |

Local report directories and `.env` are ignored by Git. Keep private reports and credentials out of version control.
