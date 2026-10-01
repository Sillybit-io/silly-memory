#!/bin/bash
# silly-memory — Cursor user-level hook dispatcher (fail-open)
set -euo pipefail
LC_ALL=C
export LC_ALL

# An explicit MEMORY_BIN wins; otherwise use the engine's home: SILLY_MEMORY_HOME,
# else ~/.silly-memory.
MEMORY_BIN="${MEMORY_BIN:-${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory}"

if [[ ! -f "$MEMORY_BIN" ]]; then
  echo '{}'
  exit 0
fi

python3 "$MEMORY_BIN" hook --tool cursor || echo '{}'
exit 0
