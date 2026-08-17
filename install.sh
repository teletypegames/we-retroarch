#!/bin/bash
# Install a WarpEngine store into a RetroArch installation.
#
# This installer belongs to the engine, so it takes the store as a parameter:
# point STORE_CONFIG at a config.json — a local path or a URL — and it sets up
# that store. A store repository's own install.sh is a short wrapper that
# supplies its config; see README.
#
#   STORE_CONFIG=./config.json ./install.sh
#   STORE_CONFIG=https://git.example.org/tools/my-store/raw/branch/master/config.json \
#     bash <(curl -fsSL https://git.teletypegames.org/tools/warp-engine-retroarch-store/raw/branch/master/install.sh)
#
set -euo pipefail

STORE_ROOT="${STORE_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/warp-engine-store}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
ENGINE_RAW_BASE="${ENGINE_RAW_BASE:-https://git.teletypegames.org/tools/warp-engine-retroarch-store/raw/branch/master}"
# The shared core lives in its own repository, because both store engines use it.
WARPSTORE_RAW_BASE="${WARPSTORE_RAW_BASE:-https://git.teletypegames.org/engines/warpstore/raw/branch/master}"
STORE_CONFIG="${STORE_CONFIG:-}"
# Only set when run as a file; piped from curl "$0" is `bash`, and then the
# engine has to come from the forge rather than from whatever the cwd holds.
SRC_DIR=""
if [ -f "$0" ]; then
  SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
fi

say() { echo "[install] $*"; }
die() { echo "[install] error: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v curl >/dev/null 2>&1 || die "curl is required"
[ -n "$STORE_CONFIG" ] || die "STORE_CONFIG is required (path or URL of a store config.json)"

fetch() {  # fetch <source> <dest>; source may be a local path or a URL
  case "$1" in
    http://*|https://*) curl -fsSL "$1" -o "$2" ;;
    *) [ -f "$1" ] || die "no such file: $1"; cp "$1" "$2" ;;
  esac
}

# Stage the config first: the store id in it decides where everything lands.
TMP_CFG="$(mktemp)"
trap 'rm -f "$TMP_CFG"' EXIT
say "reading store config from $STORE_CONFIG"
fetch "$STORE_CONFIG" "$TMP_CFG"

STORE_META="$(python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1])).get("store") or {}
sid = cfg.get("id") or ""
if not sid or "/" in sid:
    sys.exit("store.id is required and must not contain a slash")
print(sid, cfg.get("name") or sid)
' "$TMP_CFG")" || die "cannot read $STORE_CONFIG"

STORE_ID="${STORE_META%% *}"
STORE_NAME="${STORE_META#* }"

STORE_HOME="$STORE_ROOT/$STORE_ID"
LAUNCHER="$BIN_DIR/$STORE_ID-retroarch-store"

mkdir -p "$STORE_HOME" "$BIN_DIR"

install_file() {  # install_file <name> <raw base> [local override]
  if [ -n "${3:-}" ] && [ -f "${3:-}" ]; then
    say "installing $1 from ${3}"
    install -m 0755 "$3" "$STORE_HOME/$1"
  elif [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/$1" ]; then
    say "installing $1 from $SRC_DIR"
    install -m 0755 "$SRC_DIR/$1" "$STORE_HOME/$1"
  else
    say "downloading $1 from $2"
    curl -fsSL "$2/$1" -o "$STORE_HOME/$1"
    chmod 0755 "$STORE_HOME/$1"
  fi
}

install_file retroarch_store.py "$ENGINE_RAW_BASE"
install_file warpstore.py "$WARPSTORE_RAW_BASE" "${WARPSTORE_SRC:-}"

# The config is the store's identity, so it is always refreshed; state.json,
# catalog.json and the log stay put, which makes re-running the upgrade path.
if [ -f "$STORE_HOME/config.json" ]; then
  say "updating $STORE_HOME/config.json (previous copy kept as config.json.bak)"
  cp "$STORE_HOME/config.json" "$STORE_HOME/config.json.bak"
fi
install -m 0644 "$TMP_CFG" "$STORE_HOME/config.json"

# A short launcher so the CLI is one path, not a python invocation.
cat > "$LAUNCHER" <<EOF
#!/bin/bash
# $STORE_NAME — CLI wrapper around the shared RetroArch store engine.
export RETROARCH_STORE_HOME="$STORE_HOME"
exec "$STORE_HOME/retroarch_store.py" --config "$STORE_HOME/config.json" "\$@"
EOF
chmod 0755 "$LAUNCHER"

say "resolved paths:"
"$LAUNCHER" paths 2>&1 | sed 's/^/  /' || true

if [ "${STORE_SKIP_SYNC:-0}" != "1" ]; then
  say "running the first sync"
  "$LAUNCHER" --verbose sync || say "the first sync did not finish — see the message above"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "note: $BIN_DIR is not on your PATH" ;;
esac

cat <<EOF

[install] done.

  CLI:      $LAUNCHER list|sync|remove|export|paths|config
  Config:   $STORE_HOME/config.json

Restart RetroArch to see the playlists — it builds its menu at startup.

Keep it up to date by running the sync on a schedule, e.g. in crontab:
  0 * * * * $LAUNCHER sync >>$STORE_HOME/store.log 2>&1
EOF
