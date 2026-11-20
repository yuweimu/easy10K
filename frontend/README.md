# Easy10K frontend

## Run locally

Start the FastAPI app on `http://localhost:8000`, then run:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173` and enter a stock code. Vite proxies `/api` requests to the FastAPI server during development.

For a deployed frontend, set `VITE_API_BASE_URL` to the public API URL before building.

## Verify an amount against its source

Click an amount in the balance-sheet table to open a cached image excerpt with
the source row highlighted. The dialog shows the original statement unit and
amount, supports full-page and higher-resolution images, and links to the PDF.
Only choosing the PDF link loads the original PDF in the browser.

New extractions retain normalized row boxes and pre-render statement pages.
Existing extractions with statement page numbers are indexed on the first
source-preview request. Images and legacy row indexes are cached locally under
`reports/.source-cache/{pdf_hash}/`; the API process needs write access there.
No database migration or re-digestion is required for existing registered sources.

Source lookup uses the displayed report's creation time. When a source PDF is
missing, multiple PDFs match the version, or a unique matching row cannot be
located, the dialog reports the problem instead of guessing a highlight.
