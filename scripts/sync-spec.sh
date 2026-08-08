#!/bin/sh
# Refresh the vendored api-gateway protocol spec.
#
# The canonical spec lives in the api-gateway repo; the conformance tests here
# run against it. A vendored copy under spec/ lets CI run conformance without
# checking out that (private) repo. Locally the tests prefer the sibling
# ../api-gateway/spec, so this copy only needs refreshing when the spec changes.
#
#   scripts/sync-spec.sh          # copy from ../api-gateway/spec
#   scripts/sync-spec.sh --check  # fail if the vendored copy is stale
set -eu

CDPATH=''
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/spec"
SRC="$(cd "$ROOT/.." && pwd)/api-gateway/spec"

[ -d "$SRC" ] || {
    echo "Canonical spec not found at $SRC — check out api-gateway beside this repo." >&2
    exit 1
}

if [ "${1:-}" = "--check" ]; then
    if ! diff -r "$SRC" "$DEST" >/dev/null 2>&1; then
        echo "Vendored spec/ is out of sync with api-gateway. Run scripts/sync-spec.sh." >&2
        diff -r "$SRC" "$DEST" || true
        exit 1
    fi
    echo "Vendored spec/ is in sync."
else
    rm -rf "$DEST"
    mkdir -p "$DEST"
    cp -R "$SRC/." "$DEST/"
    echo "Synced $DEST from $SRC"
fi
