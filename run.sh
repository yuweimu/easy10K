#!/usr/bin/env bash

# set -euo pipefail

if [[ $# -ne 1 || ! $1 =~ ^[0-9]{6}$ ]]; then
    echo "Usage: $0 STOCK_CODE" >&2
    exit 2
fi

stock_code=$1
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python="$script_dir/.venv/bin/python"

if [[ ! -x $python ]]; then
    python=python3
fi

echo "Downloading reports for $stock_code"
#"$python" "$script_dir/cninfo_download.py" --enroll-only "$stock_code"
#"$python" "$script_dir/cninfo_download.py" --refresh "$stock_code"
# "$python" "$script_dir/cninfo_download.py" "$stock_code"

echo "Extracting reports for $stock_code"
#"$python" "$script_dir/extract.py" --stock_code "$stock_code"

echo "Auditing extractions for $stock_code"
"$python" "$script_dir/audit_extraction.py" --stock_code "$stock_code" --fail-on-issues

echo "Digesting balance sheets for $stock_code"
"$python" "$script_dir/digest_bs.py" --stock_code "$stock_code"

echo "Digesting income statements for $stock_code"
"$python" "$script_dir/digest_is.py" --stock_code "$stock_code"

echo "Digesting cash-flow statements for $stock_code"
"$python" "$script_dir/digest_cf.py" --stock_code "$stock_code"

echo "Completed pipeline for $stock_code"
