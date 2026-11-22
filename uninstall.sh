#!/bin/sh
# Remove a WarpEngine store from this machine's RetroArch.
#
# The engine does the removing, not this script: it is the only thing that knows
# which playlists, thumbnails and content files were the store's — so it runs
# `purge` first, and only then does the store itself go.
#
#   ./uninstall.sh                  # the only store installed, or say which one
#   STORE_ID=example ./uninstall.sh
#   DRY_RUN=1 ./uninstall.sh        # show what would go, remove nothing
#   curl -fsSL https://git.teletypegames.org/stores/we-retroarch/raw/branch/master/uninstall.sh | sh
#
set -eu

STORE_ROOT="${STORE_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/warp-engine-store}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
STORE_ID="${STORE_ID:-}"
STORE_CONFIG="${STORE_CONFIG:-}"
DRY_RUN="${DRY_RUN:-0}"
# Keep the store home — its state.json, catalog cache and log — and take only the
# games and the launcher. Useful before a reinstall.
KEEP_HOME="${KEEP_HOME:-0}"
# Purge even while RetroArch is running. It writes its playlists back on exit,
# so this can undo the uninstall; closing RetroArch is the better answer.
FORCE="${FORCE:-0}"

say() { echo "[uninstall] $*"; }
die() { echo "[uninstall] error: $*" >&2; exit 1; }
run() {
  if [ "$DRY_RUN" = "1" ]; then echo "[uninstall] would: $*"; else "$@"; fi
}

command -v python3 >/dev/null 2>&1 || die "python3 is required"

# Which store? An explicit id wins, then the id in a config, then — if exactly
# one store is installed — that one. Guessing between several would be worse than
# asking.
if [ -z "$STORE_ID" ] && [ -n "$STORE_CONFIG" ]; then
  TMP_CFG="$(mktemp)"
  trap 'rm -f "$TMP_CFG"' EXIT
  case "$STORE_CONFIG" in
    http://*|https://*) curl -fsSL "$STORE_CONFIG" -o "$TMP_CFG" ;;
    *) [ -f "$STORE_CONFIG" ] || die "no such file: $STORE_CONFIG"; cp "$STORE_CONFIG" "$TMP_CFG" ;;
  esac
  STORE_ID="$(python3 -c 'import json,sys; print((json.load(open(sys.argv[1])).get("store") or {}).get("id") or "")' "$TMP_CFG")"
  [ -n "$STORE_ID" ] || die "no store.id in $STORE_CONFIG"
fi

if [ -z "$STORE_ID" ]; then
  [ -d "$STORE_ROOT" ] || die "nothing is installed under $STORE_ROOT"
  FOUND=""
  COUNT=0
  for d in "$STORE_ROOT"/*/; do
    if [ -f "$d/retroarch_store.py" ]; then
      # `<id>-retroarch` is the home; the store id is what the user types.
      name="$(basename "$d")"
      FOUND="$FOUND ${name%-retroarch}"
      COUNT=$((COUNT + 1))
    fi
  done
  case "$COUNT" in
    0) die "nothing is installed under $STORE_ROOT" ;;
    1) STORE_ID="${FOUND# }"; say "the only store installed is '$STORE_ID'" ;;
    *) die "several stores are installed ($FOUND) — pick one: STORE_ID=<id> $0" ;;
  esac
fi

STORE_HOME="$STORE_ROOT/$STORE_ID-retroarch"
# Installs from before the rename kept the home under the bare store id.
[ -d "$STORE_HOME" ] || [ ! -f "$STORE_ROOT/$STORE_ID/retroarch_store.py" ] || STORE_HOME="$STORE_ROOT/$STORE_ID"
LAUNCHER="$BIN_DIR/$STORE_ID-retroarch-store"
[ -d "$STORE_HOME" ] || die "no store '$STORE_ID' in $STORE_ROOT"

say "removing the store '$STORE_ID' from $STORE_HOME"

# The games first, through the engine. If it cannot finish — RetroArch running,
# a ROM folder gone — stop here rather than delete the engine that knows what it
# installed and leave the files orphaned.
if [ -f "$STORE_HOME/retroarch_store.py" ] && [ -f "$STORE_HOME/config.json" ]; then
  DRY=""
  [ "$DRY_RUN" = "1" ] && DRY="--dry-run"
  PURGE_FLAGS=""
  [ "$FORCE" = "1" ] && PURGE_FLAGS="--force"
  # shellcheck disable=SC2086 # the flags are ours and deliberately word-split
  RETROARCH_STORE_HOME="$STORE_HOME" python3 "$STORE_HOME/retroarch_store.py" \
    --config "$STORE_HOME/config.json" $DRY purge $PURGE_FLAGS \
    || die "the engine could not remove the games (see above) — nothing else was touched.
        Close RetroArch and run this again, or FORCE=1 to purge anyway."
else
  say "warning: no engine in $STORE_HOME — playlists, thumbnails and content have to go by hand"
fi

run rm -f "$LAUNCHER"

if [ "$KEEP_HOME" = "1" ]; then
  say "keeping $STORE_HOME (KEEP_HOME=1)"
else
  run rm -rf "$STORE_HOME"
  # Only if this was the last store on the machine.
  if rmdir "$STORE_ROOT" 2>/dev/null; then say "removed the now-empty $STORE_ROOT"; fi
fi

if [ "$DRY_RUN" = "1" ]; then
  say "dry run — nothing was removed."
else
  say "done. Restart RetroArch to see the playlists go."
fi
