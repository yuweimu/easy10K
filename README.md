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

### 1. Convert PDFs to Markdown

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

Without `--output`, each Markdown file is saved beside its source PDF. Filenames are preserved with a `.md` extension; the converter does not infer stock codes or reporting dates from arbitrary PDF names.

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
