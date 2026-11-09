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
