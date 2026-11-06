#!/bin/bash
# Build the add-on image exactly as the Supervisor would from a Linux clone: from the raw git blobs
# (LF endings). Building from a Windows working tree with core.autocrlf=true gives run.sh CRLF line
# endings, which breaks its `#!/usr/bin/with-contenv bashio` shebang ("unable to exec bashio") even
# though the committed blob is fine; `git archive` also applies autocrlf, so it cannot be used.
# usage: build_image.sh [git-rev] [tag]
set -eu
REV=${1:-HEAD}; TAG=${2:-thermowatt-pr16:test}
CTX=$(mktemp -d)
for f in Dockerfile config.yaml run.sh thermowatt_bridge.py; do git show "$REV:thermowatt-heater/$f" > "$CTX/$f"; done
docker build -q -t "$TAG" "$CTX"
rm -rf "$CTX"
