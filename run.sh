#!/usr/bin/env bash
# Start topofab on http://127.0.0.1:8724
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { cat <<'MSG'
No .venv yet. Create it with:

  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python fastapi "uvicorn[standard]" httpx \
      numpy contourpy shapely pyproj pillow tifffile imagecodecs osmium
MSG
exit 1; }
exec .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port "${PORT:-8724}" "$@"
