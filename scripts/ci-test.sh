#!/bin/sh
# The checks of a release: the engine compiles and says the version metadata.json does,
# the installers pass shellcheck, and CORE_VERSION is a we-core tag.
set -eu
cd "$(dirname "$0")/.."

ENGINE=retroarch_store.py
python3 -m py_compile "$ENGINE"
ENGINE_VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$ENGINE")
META_VERSION=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' metadata.json | head -n 1)
if [ "$ENGINE_VERSION" != "$META_VERSION" ]; then
  echo "$ENGINE says $ENGINE_VERSION, metadata.json $META_VERSION" >&2
  exit 1
fi
shellcheck install.sh uninstall.sh scripts/*.sh
if ! grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' CORE_VERSION; then
  echo "CORE_VERSION is not a we-core tag" >&2
  exit 1
fi
echo "ok: $ENGINE $ENGINE_VERSION, we-core $(cat CORE_VERSION)"
