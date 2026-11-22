#!/bin/sh
# Packs the release into dist/release.tar.gz: the engine, the we-core tag CORE_VERSION pins
# (CORE_URL overrides where it comes from) and the installers, in one we-retroarch/ folder.
set -eu
cd "$(dirname "$0")/.."

NAME=we-retroarch
ENGINE=retroarch_store.py
CORE_VERSION=$(cat CORE_VERSION)
CORE_URL="${CORE_URL:-https://git.teletypegames.org/engines/we-core/raw/tag/$CORE_VERSION/warpstore.py}"
OUT=dist

rm -rf "./$OUT"
mkdir -p "$OUT/$NAME"
cp "$ENGINE" install.sh uninstall.sh config.example.json README.md LICENSE "$OUT/$NAME/"
curl -fsSL --retry 3 "$CORE_URL" -o "$OUT/$NAME/warpstore.py"
python3 -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$OUT/$NAME/warpstore.py"
chmod 0755 "$OUT/$NAME/$ENGINE" "$OUT/$NAME/warpstore.py" "$OUT/$NAME/install.sh" "$OUT/$NAME/uninstall.sh"
tar -C "$OUT" -czf "$OUT/release.tar.gz" "$NAME"
echo "packed $OUT/release.tar.gz: $NAME + we-core $CORE_VERSION"
