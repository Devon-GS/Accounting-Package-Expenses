#!/bin/sh
set -e

python -c "from app import bootstrap_storage; bootstrap_storage()"

exec gunicorn --workers "${WEB_CONCURRENCY:-2}" --bind "0.0.0.0:${PORT:-5000}" app:app
