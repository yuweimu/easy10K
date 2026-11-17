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

### 1. Convert PDFs to Markdown

On a conversion error, available Markdown (including partial output) is saved beside the source PDF with the same basename for investigation. An existing sibling Markdown file is backed up as `{basename}.previous-*.md` before replacement. Errors are recorded as `PDF2MD_ERROR` when the registry is available and the required PDF keys are known; the batch continues with the next file.

Use stock-code subdirectories and reporting-date filenames for reports intended for the viewer:

```text
reports/
└── 300750/
    └── 2025-12-31.pdf
```

Convert a single PDF:

```bash
python mdgenerate.py reports/300750/2025-12-31.pdf --output markdown/300750/2025-12-31.md
```

Or recursively convert every `.pdf`/`.PDF` file, preserving subdirectories:

```bash
python mdgenerate.py reports --output markdown
```

Without `--output`, both single-file and directory conversion save reports to `./reports/markdown/{stock_code}/{stock_code}.{reporting_date}.{pdf_creation_time}.md`, relative to the current working directory. The PDF is first converted to a temporary Markdown file with `pymupdf4llm`, then stock code and reporting date are extracted from that file using the same balance-sheet rules as digestion. Creation time comes from PDF metadata. An undetermined reporting date uses `no-date` in the filename. If the stock code or creation time is missing, specify an explicit `--output`. Because the default filename is determined after conversion, existing-output protection is checked when publishing the completed temporary file. Explicit directory output continues to preserve source subdirectories and filenames.

Existing output files are skipped. To replace them:

```bash
python mdgenerate.py reports --output markdown --force
```

Failed conversions leave existing output untouched and clean up their temporary files. An abruptly terminated process may leave a hidden `.tmp` file, but not a partially written final Markdown file.

### 2. Preprocess the reports for browsing

```bash
python mdpreprocess.py
```

Defaults are `markdown` for input and `preprocessed` for output. Custom directories are supported:

```bash
python mdpreprocess.py --input-dir markdown --preprocessed-dir preprocessed
```

The preprocessor reads `INPUT_DIR/*/*.md`, so files must be exactly one subdirectory below the input root. It preserves source files and rewrites the corresponding preprocessed copies.

Generated reports include:

- A `__reportanchor-index` anchor at the contents heading, where present.
- Stable section anchors such as `__reportanchor-financial-statements`.
- Parent-scoped second-level subtitle anchors, shown as indented child links in the viewer navigation.
- Contents links to matching sections.
- For reports without contents, navigation built from standalone Chinese-numbered headings such as `一、主要财务数据`.
- A hidden `REPORT_NAVIGATION` comment after `ORIGINAL_PDF_FILE_CREATION_DATE`, containing only section-link elements when that metadata and contents are present.
- Balance-sheet item indentation and numeric/caption alignment.

### 3. Digest original Markdown into PostgreSQL

```bash
python mddigest.py markdown/300750/2025-12-31.md
```

Or recursively digest a directory:

```bash
python mddigest.py markdown
```

Each report gets a fresh parser and storage state. The parser extracts stock code, reporting date, creation time, currency unit, and recognized consolidated balance-sheet items. Storage creates its table when the first item is persisted and upserts on stock code, reporting date, item ID, and creation time.

Use the original `markdown` files for digestion, not the HTML-decorated `preprocessed` copies. Run digestion from the project root because the parser loads `line-item-chart.txt` relative to the working directory.

### Audit direct PDF statement extraction

To extract multiple PDFs or recursively process directories with
[pdf_extract.py](./pdf_extract.py), pass one or more paths:

```bash
python pdf_extract.py reports/first.pdf reports/second.pdf -o extracted/pdf
python pdf_extract.py reports/download/000702 reports/download/605499 -o extracted/pdf
```

Overlapping inputs are deduplicated. Output folders preserve paths relative to
the PDFs' common parent directory, keeping reports with identical names in
different folders separate. Before extraction, each PDF's MD5 hash is checked
against `report_digestion`; reports at `AuditPass` or `ManualPass` are skipped, preserving their
existing files and database contents. Other stages are processed normally.
The extractor also reads report-title periods from the first three pages
(`年年度`, `第一季度`, `第三季度`, and `半年度`). Extraction JSON keeps
`title_reporting_date`, `balance_sheet_reporting_date`, and
`reporting_dates_match` (true/false when both sources exist, otherwise null).
The balance-sheet date takes priority; the title date is used when it is absent.
Disagreement between the two sources is flagged by the extraction audit.
Statement headings are accepted only when one of the next three text lines
contains the statement's reporting period/date, a unit or currency label, or
the preparer (`编制单位`). This rejects headings without nearby statement metadata.
Without `-o`, a single PDF prints a JSON object;
multiple PDFs print a JSON array. A failed PDF is reported on stderr while the
remaining PDFs continue, with exit code 1 if any report fails.

Use [audit_pdf_extraction.py](./audit_pdf_extraction.py) to screen metadata, table
structure, page spans, and decimal-value coverage. By default it reads
`report_digestion` rows where `digestion_stage = 'ExtractionSuccess'` using
`DATABASE_URL` from the environment or `.env`. It audits the stored
`extraction_report` JSON without opening PDFs. For each report it saves the
`issues` and `statements` fields as JSON in `extraction_audit`, and sets
`digestion_stage` to `AuditPass` when issues are empty or `AuditIssues` otherwise.
These updates are committed together per report. Previously audited rows are
excluded from subsequent runs unless their stage returns to `ExtractionSuccess`.
On the report digestion page, select a stage beneath the current stage badge
and click **Save** to update it manually. Setting `ExtractionSuccess` makes the
report eligible for another audit. Stage changes preserve extraction and audit
content; the audit is displayed only at `AuditPass` or `AuditIssues`.
Use `ManualPass` to record a human's manual pass decision; it has a success badge
and is preserved during extraction reruns.
The table has three tabs: **InProgress** (all stages except `AuditPass` and
`ManualPass`), **AuditPass**, and **ManualPass**. It opens on **InProgress**;
switching tabs resets to page one, with totals and pagination for that tab.
The output `audit.json` includes each row's `pdf_hash`; malformed extractions
are recorded without stopping the batch.

```bash
python audit_pdf_extraction.py
python audit_pdf_extraction.py --input-dir reports --output-dir extracted/pdf
```

Pass `--input-dir` to recursively extract and audit local PDFs instead.
The default output is anchored to the project directory, so the script can be
launched from another working directory. In directory mode, each PDF gets a mirrored output subdirectory
containing `report.json` and separate statement Markdown files. Generated files
are replaced on reruns, and `audit.json` is updated after every report. A failed
file is recorded without stopping the batch.

In directory mode, use `--cached` to audit saved extractions without reconverting PDFs that already
have `report.json`. Cached mode does not check source freshness; omit it after
changing PDFs or extraction logic. Use `--fail-on-issues` to return exit code 1
when any report is flagged (for automation). By default, audit findings do not
cause a nonzero exit code. An empty input directory produces an empty audit.

These checks flag suspicious results; they do not certify every financial value
or replace a visual review of source statements.

### Preview a PDF as lightweight HTML

```bash
python pdf_preview.py "reports/download/600519/贵州茅台：2024年第一季度报告.pdf"
python pdf_preview.py report.pdf --output preview.html --start-page 5 --end-page 10
```

[pdf_preview.py](./pdf_preview.py) produces a small text-first HTML reader by
default. It uses plain selectable text, preserving line breaks and approximate
column spacing but omitting images, charts, original fonts, and exact layout.
Text is stored only once, rather than duplicated in a separate search index.
The reader
includes bookmarks (or page navigation when no bookmarks exist), page jumps,
zoom, selectable text, page-level search, and printing. Open the generated HTML
directly in a browser; no extension, server, or original PDF is required.

By default HTML is saved beside the PDF. Existing HTML is preserved unless
`--force` is supplied, and conversion publishes the output only after completion.
Page ranges are one-based and inclusive. Search and bookmarks remain available.
For a larger preview that retains the page appearance, use `--mode image`
(90 DPI, JPEG quality 50 by default); `--dpi` and `--quality` control that mode's
size/sharpness tradeoff. Scanned reports need prior OCR for text mode, otherwise
use image mode to view them.

### Preview a PDF as Markdown

```bash
python pdf_preview_md.py "reports/download/600519/贵州茅台：2024年第一季度报告.pdf" --output output/markdown/600519-2024Q1.md
python pdf_preview_md.py report.pdf --start-page 5 --end-page 10 --force
```

[pdf_preview_md.py](./pdf_preview_md.py) is a standalone converter without database
access. It preserves detected headings, emphasis, reading order, and Markdown
tables, with a linked contents list and page markers. Output defaults to a `.md`
file beside the source PDF. Existing output is protected unless `--force` is used;
only completed conversions replace the destination. Images, charts, fonts, and
exact positioning are not retained. Scanned pages need prior OCR. For borderless
tables, try `--table-strategy text`; table detection should still be checked
against the source report.

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
| [mdgenerate.py](./mdgenerate.py) | PDF-to-Markdown conversion. |
| [mdpreprocess.py](./mdpreprocess.py) | Anchors, navigation metadata, and balance-sheet formatting. |
| [mddigest.py](./mddigest.py) | Report parsing and financial-item extraction. |
| [line_item_chart.py](./line_item_chart.py) | Load the bilingual item hierarchy. |
| [line_item_storage.py](./line_item_storage.py) | Environment loading and PostgreSQL persistence. |
| [main.py](./main.py) | FastAPI endpoints. |
| [report_renderer.py](./report_renderer.py) | Markdown rendering and viewer security headers. |
| [report_view/](./report_view/) | Report HTML, CSS, and JavaScript. |
| [frontend/](./frontend/) | React dashboard. |

Local report directories and `.env` are ignored by Git. Keep private reports and credentials out of version control.
