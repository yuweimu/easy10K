#!/usr/bin/env bash

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python="$script_dir/.venv/bin/python"

if [[ ! -x "$python" ]]; then
    python=python3
fi

usage() {
    echo "Usage: $0" >&2
    echo "       DATABASE_URL must point to the Easy10K PostgreSQL database." >&2
}

if [[ $# -ne 0 ]]; then
    usage
    exit 2
fi

database_url=${DATABASE_URL:-}
if [[ -z "$database_url" && -f "$script_dir/.env" ]]; then
    database_url=$(sed -n 's/^[[:space:]]*DATABASE_URL[[:space:]]*=[[:space:]]*//p' \
        "$script_dir/.env" | head -n 1)
    database_url=${database_url#\"}
    database_url=${database_url%\"}
    database_url=${database_url#\'}
    database_url=${database_url%\'}
fi

: "${database_url:?DATABASE_URL must be set or defined in .env}"

if ! command -v psql >/dev/null 2>&1; then
    echo "Unable to find psql in PATH" >&2
    exit 1
fi

stock_codes=$(psql "$database_url" -AtX -v ON_ERROR_STOP=1 -c \
    "SELECT stock_code
       FROM stock_metadata
      WHERE enrollment = 'Pending' AND stock_code >= '002371'
      ORDER BY stock_code;")

if [[ -z "$stock_codes" ]]; then
    echo "No stocks with enrollment InProgress."
    exit 0
fi

stock_count=0
failed_count=0

while IFS= read -r stock_code; do
    [[ -n "$stock_code" ]] || continue
    stock_count=$((stock_count + 1))
    echo "=== Downloading $stock_code ==="
    if ! "$python" "$script_dir/cninfo_download.py" --delay 5,10 --refresh "$stock_code"; then
        echo "Failed to download $stock_code; continuing." >&2
        failed_count=$((failed_count + 1))
    fi
done <<< "$stock_codes"

echo "Processed $stock_count stocks with enrollment InProgress; $failed_count failed."
