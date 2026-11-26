#!/usr/bin/env bash
# Copyright (c) 2026 gechengfu. SPDX-License-Identifier: MIT
set -euo pipefail
if [[ $# != 1 ]]; then
    echo "Usage: PG_CONFIG=/path/to/OpenTenBase/bin/pg_config $0 PREPARED_SOURCE" >&2
    exit 2
fi
AGE_BUILD_DIR="$1"
AGE_PG_CONFIG="${PG_CONFIG:-pg_config}"
case "$("$AGE_PG_CONFIG" --version)" in
    *OpenTenBase*) ;;
    *) echo 'This patch targets OpenTenBase 5.21.8; use its pg_config.' >&2; exit 2 ;;
esac
if [[ "$(cat "$AGE_BUILD_DIR/UPSTREAM_COMMIT")" != 1bb3aff56470499a1c036ae36eee4dfeeb9566b4 ]]; then
    echo 'Prepare the pinned source first.' >&2
    exit 2
fi
# Clean rebuild: PGXS does not track our force-included compatibility header.
make -C "$AGE_BUILD_DIR" PG_CONFIG="$AGE_PG_CONFIG" clean
make -C "$AGE_BUILD_DIR" PG_CONFIG="$AGE_PG_CONFIG" -j"${JOBS:-2}"
printf 'Build complete. Install on each CN/DN host with make PG_CONFIG=... install.\n'
