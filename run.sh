#!/bin/sh
# Starts the brain from a plain checkout, with nothing to install first. Any arguments are passed
# on, so `./run.sh agents` is the same as `python3.15 -m brain agents`. The default is `serve`.
set -eu

cd "$(dirname "$0")"

PYTHON="${BRAIN_PYTHON:-python3.15}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Local AI Brain needs Python 3.15, and '$PYTHON' was not found." >&2
    echo "Install it, or point BRAIN_PYTHON at it." >&2
    exit 1
fi

if [ "$#" -eq 0 ]; then
    set -- serve
fi
exec "$PYTHON" -m brain "$@"
