#!/bin/sh
set -e

if [ "$(id -u)" = "0" ]; then
    # The mounted instance volume may be created by Docker as root. Repair
    # ownership before dropping privileges so uploads and the database remain writable.
    mkdir -p /app/instance/uploads/speedpoint_runs /tmp/accounting
    chown -R appuser:appuser /app/instance /tmp/accounting
    exec gosu appuser "$0" "$@"
fi

python -c "from app import bootstrap_storage; bootstrap_storage()"

exec gunicorn --workers "${WEB_CONCURRENCY:-2}" --bind "0.0.0.0:${PORT:-5000}" app:app
