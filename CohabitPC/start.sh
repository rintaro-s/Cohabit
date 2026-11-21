#!/bin/sh
# Cohabit PC を起動する (Linux / macOS)。Python 3.9 以上があればよい (pip で入れるものは無い)
cd "$(dirname "$0")" && exec python3 cohabit_pc.py "$@"
