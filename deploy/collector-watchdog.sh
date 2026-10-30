#!/bin/sh
set -eu
umask 077
# Resolve the checkout path without embedding a machine name or user name.
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# Share the lock with the user service; the logger must not hold that lock.
exec /usr/bin/flock -n -E 0 "$project_dir/.runtime/collector.lock" \
    "$project_dir/.venv/bin/python" "$project_dir/collector.py" \
    --config "$project_dir/.runtime/collector-config.json" 2>&1 | \
    /usr/bin/logger -t home-monitor-collector
