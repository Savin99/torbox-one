#!/bin/bash
# Release IPA → iCloud Drive/IPA Shelf. See IPA_RELEASE.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/tools/ipa-release.py" --project-root "$ROOT" --scheme TorBoxOne --derived-data build/ipa "$@"
