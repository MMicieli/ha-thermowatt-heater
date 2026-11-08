#!/bin/bash
# Reproducible clean-environment run of the whole bridge test suite (issue #17 / PR #21).
#   usage: thermowatt-heater/tests/run_all.sh            # unit + real-bashio startup tests
#          RUN_DOCKER_E2E=1 thermowatt-heater/tests/run_all.sh   # also the Docker e2e (needs Docker; ~7-10 min per scenario, set E2E_SCENARIOS="C D" to choose)
# Creates a throwaway venv and a throwaway bashio download under a fresh temp dir; nothing
# outside it is modified. Needs: bash, python3 >= 3.12 with venv, curl, tar, sha256sum.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDON="$(dirname "$HERE")"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q "paho-mqtt==2.2.0" "requests==2.34.2" "urllib3==2.8.0" \
  "certifi==2026.10.23" "idna==3.20" "charset-normalizer==3.5.2" -r "$HERE/requirements-test.txt"
# bashio v0.16.2 (the library the add-on base image ships); pinned by checksum
curl -fsSL https://github.com/hassio-addons/bashio/archive/refs/tags/v0.16.2.tar.gz -o "$WORK/bashio.tgz"
echo "d0f0c780c4badd103c00c572b1bf9645520d15a8a8070d6e3d64e35cb9f583aa  $WORK/bashio.tgz" | sha256sum -c -
tar -xzf "$WORK/bashio.tgz" -C "$WORK"
export TW_BASHIO_LIB="$WORK/bashio-0.16.2/lib"
cd "$ADDON"
"$WORK/venv/bin/python" -m pytest tests -q -p no:cacheprovider -rs
if [ "${RUN_DOCKER_E2E:-0}" = 1 ]; then
  cd "$HERE/e2e"
  ./build_image.sh HEAD thermowatt-pr16:test
  for sc in ${E2E_SCENARIOS:-A B C D F G}; do ./run_e2e.sh "$(echo "$sc" | tr 'A-Z' 'a-z')" "$sc"; done
fi
