#!/usr/bin/env bash
# Replace the tarball SHA256 placeholder in the formula with the real
# checksum of the GitHub release tarball (fusion-mlx tap convention, #72).
set -euo pipefail

VERSION="${1:?Usage: $0 <version>}"
FORMULA="homebrew-tap/Formula/fusion-plugins-ecosystem.rb"

if [ ! -f "$FORMULA" ]; then
    echo "ERROR: $FORMULA not found" >&2
    exit 1
fi

log() { echo "[$(date +%H:%M:%S)] $*"; }

log "Fetching release tarball SHA256 for v${VERSION}..."
TARBALL_URL="https://github.com/dahai80/fusion-plugins-ecosystem/archive/refs/tags/v${VERSION}.tar.gz"
TARBALL_SHA=$(curl -sL "$TARBALL_URL" | shasum -a 256 | cut -d' ' -f1)
if [ -z "$TARBALL_SHA" ]; then
    echo "ERROR: Could not fetch tarball from $TARBALL_URL" >&2
    echo "Make sure the tag v${VERSION} exists on GitHub." >&2
    exit 1
fi
sed -i '' "s/PLACEHOLDER_SHA256/${TARBALL_SHA}/" "$FORMULA"
log "  PLACEHOLDER_SHA256 -> ${TARBALL_SHA:0:12}..."

remaining=$(grep -c 'PLACEHOLDER' "$FORMULA" || true)
if [ "$remaining" -gt 0 ]; then
    echo "ERROR: ${remaining} PLACEHOLDER(s) remain in formula" >&2
    exit 1
fi
log "Formula checksums updated for v${VERSION}"
