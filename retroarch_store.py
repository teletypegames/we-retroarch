#!/usr/bin/env python3
"""retroarch_store.py — the RetroArch adapter of the WarpEngine store engine.

Reads a WarpEngine catalog (`GET /api/software`), downloads the cartridge assets
a libretro core can boot, and writes RetroArch's own library format: one `.lpl`
playlist per platform, with box art in the thumbnail folders that playlist's name
points at.

RetroArch is not one machine. The same playlist format is read on desktop Linux,
Windows, macOS, Android, the Steam Deck and the console ports — so the engine
never assumes where anything is: it reads `retroarch.cfg` for the four
directories it needs, and it can write a tree for a *different* machine than the
one it runs on (`export`), which is how Android is served at all.

Everything that is not specific to RetroArch — the catalog, release selection,
host matching, state, HTTP — lives in `warpstore.py`, the module this engine
shares with the other store engines. The installer puts the two side by side.

Standard library only.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import zlib

# The shared core sits next to this script (the installer puts it there); make
# sure that is where we look, however the script was invoked.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import warpstore as ws
except ImportError:
    sys.exit("error: warpstore.py is missing next to retroarch_store.py — reinstall the store "
             "engine (https://git.teletypegames.org/engines/warpstore)")
from warpstore import debug, die, log

VERSION = "1.0.0"

# The playlist format version RetroArch writes today. Bump only after checking
# what a current RetroArch produces — the field is how it decides how to read
# the rest of the file.
PLAYLIST_VERSION = "1.5"

# Each store keeps its config, state, cache and log in one directory. The
# installer exports RETROARCH_STORE_HOME; run from a checkout, the script's own
# directory is the home.
DEFAULT_HOME = os.environ.get("RETROARCH_STORE_HOME") or os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_HOME, "config.json")

DEFAULT_CONFIG = {
    "store": {
        # Short slug: names the store home, the log prefix and the backups.
        "id": "warp",
        # Human-readable: goes into the playlist name, so it is what the user
        # sees in RetroArch's main menu.
        "name": "WarpEngine Store",
        "base_url": "https://example.org",
        "api": {
            "catalog": "/api/software",
            "download": "/api/download",
        },
    },
    "paths": {
        # All four null by default, meaning "work it out": the environment, then
        # retroarch.cfg, then the per-OS candidates. Set one to pin it.
        "retroarch_dir": None,
        "playlists_dir": None,
        "thumbnails_dir": None,
        "libretro_dir": None,
        # Where the cartridges land. Null = <retroarch_dir>/content.
        "content_root": None,
        # Our own folder inside it: what a prune is allowed to touch, and what
        # keeps two stores on one machine out of each other's files.
        "subfolder": "warp",
        # Default for `export --target-prefix`: how the exported tree will be
        # spelled on the machine that will actually run it.
        "target_prefix": None,
    },
    "playlist": {
        # `store` and `label` come from the config, `platform` from the catalog.
        "name_template": "{store} - {label}",
        # A real CRC32 helps RetroArch tie saves and thumbnails to content.
        "write_crc32": True,
        # One download, three folders: whichever thumbnail type the user has
        # selected globally, there is an image for it. We do not touch their
        # setting.
        "thumbnail_kinds": ["Named_Boxarts", "Named_Titles", "Named_Snaps"],
    },
    "catalog": {
        "statuses": ["released", "archived"],
        "owner_id": None,
        "only": [],
        "exclude": [],
    },
    # WarpEngine platform -> playlist label, asset to pull, core that boots it.
    "platforms": {
        "c64": {
            "label": "Commodore 64",
            "kind": "cartridge",
            "ext": ".prg",
            "core": {"file": "vice_x64_libretro", "name": "VICE x64"},
            "enabled": True,
        },
        "tic80": {
            "label": "TIC-80",
            "kind": "cartridge",
            "ext": ".tic",
            "core": {"file": "tic80_libretro", "name": "TIC-80"},
            "enabled": True,
        },
    },
    "behavior": {
        "prune": True,
        "timeout": 30,
        "insecure": False,
        # RetroArch writes its playlists back when it exits, so a sync while it
        # runs can be undone. Refuse rather than lose the work.
        "refuse_while_running": True,
        # Box art must be PNG; convert with whatever the machine has if not.
        "convert_images": True,
    },
}

# What a libretro core file is called, per host. Android is the odd one out, and
# it is also the host we can only ever write for from somewhere else.
CORE_SUFFIX = {"darwin": ".dylib", "linux": ".so", "windows": ".dll", "android": "_android.so"}

# Where RetroArch keeps its configuration, in the order worth trying. Only used
# when nothing else says — retroarch.cfg itself is always preferred over these.
RETROARCH_DIRS = {
    "darwin": ["~/Library/Application Support/RetroArch"],
    "linux": [
        "~/.config/retroarch",
        "~/.var/app/org.libretro.RetroArch/config/retroarch",  # flatpak (Steam Deck)
        "~/snap/retroarch/current/.config/retroarch",
    ],
    "windows": ["$APPDATA/RetroArch", "C:/RetroArch"],
    "android": ["/storage/emulated/0/RetroArch", "/sdcard/RetroArch"],
}

# retroarch.cfg is not always in the RetroArch directory itself: a macOS install
# keeps it under config/.
CFG_NAMES = ["retroarch.cfg", "config/retroarch.cfg"]

# RetroArch replaces these characters when it turns a playlist label into a
# thumbnail file name, so we have to do the same or the image is never found.
THUMB_UNSAFE = '&*/:`<>?\\|'

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Tried in order; the first one the machine actually has wins.
CONVERTERS = [
    ("sips", ["sips", "-s", "format", "png", "{src}", "--out", "{dst}"]),
    ("magick", ["magick", "{src}", "{dst}"]),
    ("convert", ["convert", "{src}", "{dst}"]),
    ("ffmpeg", ["ffmpeg", "-y", "-loglevel", "error", "-i", "{src}", "{dst}"]),
]


def subfolder(cfg):
    return cfg["paths"]["subfolder"]


# --------------------------------------------------------------------------
# retroarch.cfg
# --------------------------------------------------------------------------


def parse_retroarch_cfg(path):
    """RetroArch's config: `key = "value"` lines, `#` comments."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"')
    except OSError as exc:
        debug(f"cannot read {path}: {exc}")
    return values


def find_retroarch_cfg(base):
    for name in CFG_NAMES:
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return None


def resolve_ra_path(value, base):
    """Expand a directory as RetroArch spells it.

    `~` is the home directory; a leading `:` means "relative to RetroArch's own
    base directory", which is how a portable install refers to itself.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith(":"):
        rest = value[1:].lstrip("\\/")
        return os.path.abspath(os.path.join(base, rest)) if rest else os.path.abspath(base)
    expanded = os.path.expanduser(os.path.expandvars(value))
    return os.path.abspath(expanded) if os.path.isabs(expanded) else os.path.abspath(os.path.join(base, expanded))


def find_retroarch_dir(cfg, override=None):
    """RetroArch's directory: the flag, the config, the environment, then a guess."""
    for value, source in ((override, "--retroarch-dir"),
                          (cfg["paths"].get("retroarch_dir"), "config"),
                          (os.environ.get("RETROARCH_DIR"), "RETROARCH_DIR")):
        if value:
            path = os.path.abspath(os.path.expanduser(os.path.expandvars(value)))
            if not os.path.isdir(path):
                die(f"{source} points at {path}, which is not a directory")
            return path, source
    candidates = RETROARCH_DIRS.get(ws.host_os(), [])
    for candidate in candidates:
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(candidate)))
        if os.path.isdir(path):
            return path, "found"
    die("cannot find RetroArch's directory. Tried:\n  " + "\n  ".join(candidates)
        + "\nPoint at it with --retroarch-dir, RETROARCH_DIR or paths.retroarch_dir.")


# --------------------------------------------------------------------------
# where we write, and how the target spells it
# --------------------------------------------------------------------------


def target_join(base, *parts):
    """Join a path the way the *target* machine writes them, not this one.

    An export written on a Mac for an Android phone must contain POSIX paths;
    only a Windows-looking base gets backslashes.
    """
    sep = "\\" if ("\\" in base or re.match(r"^[A-Za-z]:", base)) else "/"
    joined = base.rstrip("/\\")
    for part in parts:
        joined += sep + str(part).strip("/\\")
    return joined


def target_within(path, root):
    """Containment test in *target* space, where os.sep may not apply."""
    p = (path or "").replace("\\", "/")
    r = (root or "").replace("\\", "/").rstrip("/")
    return bool(r) and (p == r or p.startswith(r + "/"))


class Tree:
    """Where files go locally, and how the files we write name them.

    For a local install the two coincide. For an export they do not: the content
    lands in a staging directory here, while the playlists have to name it as it
    will be on the machine that runs it. Keeping the two apart in one object is
    what makes Android — where no Python can run — reachable at all.
    """

    def __init__(self, playlists_dir, thumbnails_dir, content_root, target_content_root,
                 libretro_dir, target_libretro_dir, core_suffix, sources):
        self.playlists_dir = playlists_dir
        self.thumbnails_dir = thumbnails_dir
        self.content_root = content_root
        self.target_content_root = target_content_root
        self.libretro_dir = libretro_dir
        self.target_libretro_dir = target_libretro_dir
        self.core_suffix = core_suffix
        self.sources = sources

    def content_dir(self, cfg, platform):
        """Local directory holding one platform's cartridges."""
        return os.path.join(self.content_root, subfolder(cfg), platform)

    def target_content_dir(self, cfg, platform):
        return target_join(self.target_content_root, subfolder(cfg), platform)

    def owned_root(self, cfg):
        """The only subtree this store may ever delete from."""
        return os.path.join(self.content_root, subfolder(cfg))


def local_tree(cfg, args):
    """The tree of the RetroArch installed on this machine."""
    base, base_source = find_retroarch_dir(cfg, getattr(args, "retroarch_dir", None))
    cfg_path = find_retroarch_cfg(base)
    racfg = parse_retroarch_cfg(cfg_path) if cfg_path else {}
    sources = {"retroarch_dir": f"{base} ({base_source})",
               "retroarch.cfg": cfg_path or "not found — using defaults"}

    def pick(config_key, ra_key, default_name):
        override = cfg["paths"].get(config_key)
        if override:
            path = os.path.abspath(os.path.expanduser(os.path.expandvars(override)))
            sources[config_key] = f"{path} (config)"
            return path
        if racfg.get(ra_key):
            path = resolve_ra_path(racfg[ra_key], base)
            sources[config_key] = f"{path} (retroarch.cfg {ra_key})"
            return path
        path = os.path.join(base, default_name)
        sources[config_key] = f"{path} (default)"
        return path

    playlists = pick("playlists_dir", "playlist_directory", "playlists")
    thumbnails = pick("thumbnails_dir", "thumbnails_directory", "thumbnails")
    libretro = pick("libretro_dir", "libretro_directory", "cores")
    content = pick("content_root", "", "content")

    suffix = getattr(args, "core_suffix", None) or CORE_SUFFIX.get(ws.host_os(), ".so")
    sources["core_suffix"] = suffix
    return Tree(playlists, thumbnails, content, content, libretro, None, suffix, sources)


def export_tree(cfg, args):
    """A canonical RetroArch-shaped tree for a machine that is not this one."""
    root = os.path.abspath(os.path.expanduser(args.directory))
    target = args.target_prefix or cfg["paths"].get("target_prefix") or root
    suffix = args.core_suffix or CORE_SUFFIX.get(ws.host_os(), ".so")
    sources = {
        "export root": root,
        "target prefix": target,
        "core_suffix": suffix,
        "target libretro dir": args.target_libretro_dir or "unknown — entries stay on DETECT",
    }
    return Tree(
        playlists_dir=os.path.join(root, "playlists"),
        thumbnails_dir=os.path.join(root, "thumbnails"),
        content_root=os.path.join(root, "content"),
        target_content_root=target_join(target, "content"),
        libretro_dir=None,
        target_libretro_dir=args.target_libretro_dir,
        core_suffix=suffix,
        sources=sources,
    )


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------


def playlist_name(cfg, platform, spec):
    """What the user sees in RetroArch's menu, and the name everything hangs off.

    It is also the `db_name` and the thumbnail folder, so it carries the store's
    name: we never write into RetroArch's own `Commodore - 64.lpl`, and we never
    collide with the official thumbnail packs.
    """
    return cfg["playlist"]["name_template"].format(
        store=cfg["store"]["name"], label=spec.get("label") or platform, platform=platform,
    ).strip()


def playlist_file_name(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name) + ".lpl"


def thumb_file_name(label):
    return "".join("_" if c in THUMB_UNSAFE else c for c in label) + ".png"


# --------------------------------------------------------------------------
# catalog -> RetroArch
# --------------------------------------------------------------------------


def accept_for_retroarch(cfg):
    """The adapter's veto: a title is installable if a core is named for it.

    The scope is the catalog platform, one per playlist — not the playlist name,
    so renaming the store does not re-key everything that is installed.
    """

    def accept(spec, sw):
        platform = sw.get("platform")
        if not (spec.get("core") or {}).get("file"):
            raise ws.Skip(f"no core configured for {platform}")
        return platform, {"playlist": playlist_name(cfg, platform, spec)}

    return accept


def select_games(cfg, catalog, host_info=None):
    return ws.select_games(cfg, catalog, accept_for_retroarch(cfg), host_info)


def resolve_core(cfg, tree, platform):
    """(core_path, core_name, note) for a platform's playlist header.

    A missing core is a note, never an error: the playlist still works, the user
    is asked which core to use once, and `Online Updater ▸ Core Downloader`
    fixes it for good. A store that refuses to run without cores installed would
    never run for anyone the first time.
    """
    spec = cfg["platforms"][platform]
    core = spec.get("core") or {}
    name = core.get("name") or ""
    file_name = (core.get("file") or "") + tree.core_suffix

    if tree.target_libretro_dir:
        # An export: we cannot check the other machine, so take the user's word.
        return target_join(tree.target_libretro_dir, file_name), name, ""
    if tree.libretro_dir:
        path = os.path.join(tree.libretro_dir, file_name)
        if os.path.isfile(path):
            return path, name, ""
        return "", name, (f"core not found: {file_name} — entries stay on DETECT "
                          f"(Online Updater ▸ Core Downloader ▸ {name})")
    return "", name, f"core location unknown ({file_name}) — entries stay on DETECT"


# --------------------------------------------------------------------------
# box art
# --------------------------------------------------------------------------


def to_png(blob, ext, allow_convert=True):
    """RetroArch thumbnails must be PNG. Convert if the machine can, else None.

    The catalog serves whatever was uploaded — today one cover is a GIF — and
    the standard library cannot re-encode an image, so this leans on whatever
    converter the host happens to have. Failing that, the entry simply has no
    box art, which is far better than a broken sync.
    """
    if blob[:8] == PNG_MAGIC:
        return blob
    if not allow_convert:
        return None
    for name, template in CONVERTERS:
        if not shutil.which(name):
            continue
        tmp = tempfile.mkdtemp(prefix=f".{ws.TAG}-img-")
        try:
            src = os.path.join(tmp, "in" + (ext or ".img"))
            dst = os.path.join(tmp, "out.png")
            with open(src, "wb") as fh:
                fh.write(blob)
            cmd = [part.format(src=src, dst=dst) for part in template]
            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                with open(dst, "rb") as fh:
                    converted = fh.read()
                debug(f"converted box art to PNG with {name}")
                return converted
            debug(f"{name} could not convert the box art: {result.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            debug(f"{name} failed: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return None


def thumb_paths(cfg, tree, record):
    """The local thumbnail files for one entry, one per configured kind."""
    playlist = record["playlist"]
    name = thumb_file_name(record["title"])
    return [os.path.join(tree.thumbnails_dir, playlist, kind, name)
            for kind in cfg["playlist"]["thumbnail_kinds"]]


def install_thumbnails(cfg, tree, record, dry_run=False):
    """Fetch the cover once and put it in every thumbnail folder that wants it."""
    wanted = thumb_paths(cfg, tree, record)
    if all(os.path.isfile(p) and os.path.getsize(p) > 0 for p in wanted):
        debug(f"{record['name']}: box art already in place")
        return wanted, False
    if dry_run:
        log(f"would fetch box art for {record['name']} ({len(wanted)} folders)")
        return wanted, True

    blob, ext = ws.download_image(cfg, record)
    if blob is None:
        return [], False
    png = to_png(blob, ext, allow_convert=cfg["behavior"].get("convert_images", True))
    if png is None:
        log(f"warning: {record['name']}: box art is {ext or 'not PNG'} and no converter "
            f"(sips/magick/convert/ffmpeg) is available — skipping the thumbnail")
        return [], False

    first = None
    for path in wanted:
        if first is None:
            ws.write_atomic(path, png)
            first = path
            continue
        # Same bytes three times: link where the filesystem allows it.
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        try:
            os.link(first, path)
        except OSError:
            shutil.copy2(first, path)
    debug(f"{record['name']}: box art -> {len(wanted)} thumbnail folders")
    return wanted, True


# --------------------------------------------------------------------------
# installation
# --------------------------------------------------------------------------


def crc32_of(path):
    """RetroArch stores the content's CRC32 as `XXXXXXXX|crc`."""
    checksum = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(256 * 1024)
            if not chunk:
                break
            checksum = zlib.crc32(chunk, checksum)
    return format(checksum & 0xFFFFFFFF, "08X")


def install_game(cfg, tree, game, state_entry, dry_run=False):
    """Download the cartridge and its cover. Returns (record, changed)."""
    platform = game["platform"]
    local = os.path.join(tree.content_dir(cfg, platform), game["asset"])
    changed = False

    if os.path.isfile(local) and os.path.getsize(local) > 0:
        debug(f"{game['name']}: {game['asset']} already present")
    elif dry_run:
        log(f"would download {game['name']} {game['version']} -> {local}")
        changed = True
    else:
        size = ws.http_download(cfg, ws.download_url(cfg, game["asset"]), local)
        log(f"installed {game['name']} {game['version']} ({size} bytes) -> {local}")
        changed = True

    record = dict(game)
    record["content"] = local
    record["target"] = target_join(tree.target_content_dir(cfg, platform), game["asset"])
    record["crc32"] = ""
    if cfg["playlist"].get("write_crc32") and os.path.isfile(local):
        record["crc32"] = crc32_of(local)

    thumbs = (state_entry or {}).get("thumbs") or []
    if game.get("image_url"):
        thumbs, did = install_thumbnails(cfg, tree, record, dry_run=dry_run)
        changed = changed or did
    elif thumbs and not dry_run:
        # The cover disappeared from the catalog: drop what we cached.
        remove_thumbnails(tree, thumbs, dry_run=dry_run)
        thumbs, changed = [], True
    record["thumbs"] = thumbs
    return record, changed


def remove_thumbnails(tree, thumbs, dry_run=False):
    for path in thumbs or []:
        if not ws.within(path, tree.thumbnails_dir):
            log(f"warning: refusing to delete {path} (outside {tree.thumbnails_dir})")
            continue
        if os.path.isfile(path):
            if dry_run:
                log(f"would remove {path}")
            else:
                os.unlink(path)
                debug(f"removed {path}")


def remove_game(cfg, tree, record, dry_run=False):
    """Delete one entry's files — never anything outside what this store owns."""
    content = record.get("content")
    owned = tree.owned_root(cfg)
    if content:
        if not ws.within(content, owned):
            log(f"warning: refusing to delete {content} (outside {owned})")
        elif os.path.isfile(content):
            if dry_run:
                log(f"would remove {content}")
            else:
                os.unlink(content)
                debug(f"removed {content}")
    remove_thumbnails(tree, record.get("thumbs"), dry_run=dry_run)


# --------------------------------------------------------------------------
# the playlist
# --------------------------------------------------------------------------


def playlist_path(cfg, tree, platform):
    spec = cfg["platforms"][platform]
    return os.path.join(tree.playlists_dir, playlist_file_name(playlist_name(cfg, platform, spec)))


def read_playlist(path):
    data = ws.load_json(path, default=None)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        if os.path.isfile(path):
            log(f"warning: {path} is not a playlist we can read — starting a fresh one")
        return None
    return data


def write_playlist(cfg, tree, platform, records, dry_run=False):
    """Write one platform's playlist, keeping anything the user added to it.

    The file carries the store's name, so it is ours — but a user may still have
    added an entry of their own to it, and that survives: only items pointing
    inside this store's content folder are rewritten.
    """
    spec = cfg["platforms"].get(platform)
    if not spec:
        debug(f"no platform config for {platform} — not writing a playlist")
        return
    path = playlist_path(cfg, tree, platform)
    name = os.path.basename(path)
    ours = tree.target_content_dir(cfg, platform)

    existing = read_playlist(path)
    foreign = [i for i in (existing or {}).get("items", [])
               if not target_within(i.get("path"), ours)]
    if existing and not dry_run:
        backup = f"{path}.{cfg['store']['id']}-backup"
        if not os.path.exists(backup):
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copy2(path, backup)

    if not records and not foreign:
        if os.path.isfile(path):
            if dry_run:
                log(f"would remove the now-empty playlist {path}")
            else:
                os.unlink(path)
                log(f"removed the now-empty playlist {path}")
        return

    core_path, core_name, note = resolve_core(cfg, tree, platform)
    if note:
        log(f"{playlist_name(cfg, platform, spec)}: {note}")

    items = list(foreign)
    for record in sorted(records, key=lambda r: r["title"].lower()):
        items.append({
            "path": record["target"],
            "entry_slot": -1,
            "label": record["title"],
            # The header's default core runs these; DETECT keeps the file
            # portable to a machine whose cores live somewhere else.
            "core_path": "DETECT",
            "core_name": "DETECT",
            "crc32": f"{record['crc32']}|crc" if record.get("crc32") else "",
            "db_name": name,
        })

    playlist = {
        "version": PLAYLIST_VERSION,
        "default_core_path": core_path,
        "default_core_name": core_name,
        # Zero everywhere means "whatever the user has set globally" — a store
        # has no business overriding how someone likes their library displayed.
        "label_display_mode": 0,
        "right_thumbnail_mode": 0,
        "left_thumbnail_mode": 0,
        "thumbnail_match_mode": 0,
        "sort_mode": 0,
        "items": items,
    }
    if dry_run:
        log(f"would write {path} ({len(records)} of ours, {len(foreign)} kept)")
        return
    ws.write_json(path, playlist)
    log(f"playlist updated: {path} ({len(records)} entries" +
        (f", {len(foreign)} kept" if foreign else "") + ")")


# --------------------------------------------------------------------------
# RetroArch itself
# --------------------------------------------------------------------------


def retroarch_running():
    """True if a RetroArch process is up — it would write our playlist back."""
    if ws.host_os() == "windows":
        try:
            out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=15).stdout
            return "retroarch.exe" in out.lower()
        except (OSError, subprocess.SubprocessError):
            debug("cannot tell whether RetroArch is running")
            return False
    if not shutil.which("pgrep"):
        debug("no pgrep — cannot tell whether RetroArch is running")
        return False
    for name in ("retroarch", "RetroArch", "org.libretro.RetroArch"):
        try:
            if subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=15).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            debug("cannot tell whether RetroArch is running")
            return False
    return False


def guard_running(cfg, args):
    if not cfg["behavior"].get("refuse_while_running") or getattr(args, "force", False):
        return
    if retroarch_running():
        die("RetroArch is running: it holds its playlists in memory and writes them back on "
            "exit, so a sync now can be undone. Close it and try again (or --force).")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def install_all(cfg, tree, games, installed, dry_run=False, prune=False):
    """Install every game, prune what left the catalog. Returns (changed, scopes)."""
    touched, changed = set(), False
    for game in games:
        key = ws.game_key(game)
        previous = installed.get(key)
        if previous and previous.get("asset") != game["asset"]:
            log(f"{game['name']}: {previous.get('version')} -> {game['version']}, removing old files")
            remove_game(cfg, tree, previous, dry_run=dry_run)
            previous = None
            changed = True
        try:
            record, did = install_game(cfg, tree, game, previous, dry_run=dry_run)
        except (urllib.error.URLError, OSError) as exc:
            log(f"warning: {game['name']} failed: {exc}")
            continue
        if did or installed.get(key) != record:
            changed = True
        installed[key] = record
        touched.add(record["scope"])

    if prune:
        keep = {ws.game_key(g) for g in games}
        for key in list(installed):
            if key in keep:
                continue
            log(f"pruning {key} (no longer in the catalog or filtered out)")
            remove_game(cfg, tree, installed[key], dry_run=dry_run)
            touched.add(ws.record_scope(installed[key]))
            del installed[key]
            changed = True
    return changed, touched


def cmd_sync(cfg, args):
    guard_running(cfg, args)
    tree = local_tree(cfg, args)
    catalog = ws.fetch_catalog(cfg, use_cache=True)
    games, skipped = select_games(cfg, catalog)
    for reason in skipped:
        log(f"skipped {reason}")
    if args.name:
        games = ws.limit_to_names(games, args.name)

    state = ws.load_state()
    installed = state["installed"]
    # A name-limited sync is not a full picture of the catalog, so never prune.
    changed, touched = install_all(cfg, tree, games, installed, dry_run=args.dry_run,
                                   prune=cfg["behavior"].get("prune") and not args.name)
    if args.dry_run:
        grouped = ws.records_by_scope(installed)
        for platform in sorted(touched | set(grouped)):
            write_playlist(cfg, tree, platform, grouped.get(platform, []), dry_run=True)
        log("dry run — nothing written")
        return 0

    ws.save_state(state)
    if not changed and not args.force:
        log("already up to date")
        return 0

    grouped = ws.records_by_scope(installed)
    for platform in sorted(touched | set(grouped)):
        write_playlist(cfg, tree, platform, grouped.get(platform, []))
    log("restart RetroArch to see the playlist — it builds the menu at startup")
    return 0


def cmd_export(cfg, args):
    """Render the whole store into a tree meant for another machine.

    Stateless on purpose: an export is a complete picture of the catalog, not an
    incremental update to this machine's installation, and it must not disturb
    the state of a local install living in the same store home.
    """
    tree = export_tree(cfg, args)
    catalog = ws.fetch_catalog(cfg, use_cache=True)
    games, skipped = select_games(cfg, catalog)
    for reason in skipped:
        log(f"skipped {reason}")
    if args.name:
        games = ws.limit_to_names(games, args.name)

    log(f"exporting {len(games)} titles to {tree.sources['export root']}")
    log(f"playlists will point at {tree.target_content_root}")
    records = {}
    install_all(cfg, tree, games, records, dry_run=args.dry_run)
    grouped = ws.records_by_scope(records)
    for platform, group in sorted(grouped.items()):
        write_playlist(cfg, tree, platform, group, dry_run=args.dry_run)
    if not args.dry_run:
        log("copy the tree onto the device, e.g.:  adb push "
            f"{os.path.abspath(args.directory)}/. {tree.sources['target prefix']}/")
    return 0


def cmd_list(cfg, args):
    tree = local_tree(cfg, args)
    catalog = ws.fetch_catalog(cfg, use_cache=True)
    host = ws.host()
    log(f"host: {host['os']}/{host['arch']}, core suffix {tree.core_suffix}")
    games, skipped = select_games(cfg, catalog, host)
    installed = ws.load_state()["installed"]

    if not games:
        log("no compatible games in the catalog")
    for game in sorted(games, key=lambda g: (g["scope"], g["title"].lower())):
        record = installed.get(ws.game_key(game))
        if not record:
            mark = "  "
        elif record.get("asset") == game["asset"]:
            mark = "* "
        else:
            mark = "^ "
        print(f"{mark}{game['platform']:<8} {game['name']:<18} {game['version']:<10} {game['title']}")
    if games:
        print("\n  * installed   ^ update available")
    for reason in skipped:
        log(f"skipped {reason}")
    for platform in sorted({g["scope"] for g in games}):
        _, core_name, note = resolve_core(cfg, tree, platform)
        log(f"{platform}: {note or f'core ready ({core_name})'}")
    return 0


def cmd_remove(cfg, args):
    guard_running(cfg, args)
    tree = local_tree(cfg, args)
    state = ws.load_state()
    installed = state["installed"]
    keys = ws.match_keys(installed, args.name)
    if not keys:
        log(f"not installed: {', '.join(args.name)}")
        return 0
    touched = set()
    for key in keys:
        record = installed[key]
        remove_game(cfg, tree, record, dry_run=args.dry_run)
        touched.add(ws.record_scope(record))
        if not args.dry_run:
            del installed[key]
    if args.dry_run:
        return 0
    ws.save_state(state)
    grouped = ws.records_by_scope(installed)
    for platform in touched:
        write_playlist(cfg, tree, platform, grouped.get(platform, []))
    return 0


def cmd_paths(cfg, args):
    """Where this machine's RetroArch keeps things, and where each answer came from.

    The paths are not in the config — they are worked out from the machine — and
    they decide everything the store does, so they are worth being able to look
    at on their own. This is the first thing to run when a sync went somewhere
    unexpected.
    """
    tree = local_tree(cfg, args)
    host = ws.host()
    print(f"detected: os={host['os']} arch={host['arch']} core_suffix={tree.core_suffix}")
    for key in ("retroarch_dir", "retroarch.cfg", "playlists_dir", "thumbnails_dir",
                "libretro_dir", "content_root"):
        print(f"  {key:<16} {tree.sources.get(key, '-')}")
    print(f"  {'store folder':<16} {tree.owned_root(cfg)}")
    for platform in sorted(cfg["platforms"]):
        if not cfg["platforms"][platform].get("enabled", True):
            continue
        spec = cfg["platforms"][platform]
        _, core_name, note = resolve_core(cfg, tree, platform)
        print(f"  {playlist_file_name(playlist_name(cfg, platform, spec))}")
        print(f"    {'core':<14} {note or f'ready ({core_name})'}")
    return 0


def cmd_config(cfg, args):
    if args.write:
        if os.path.exists(args.config) and not args.force:
            die(f"{args.config} already exists (use --force to overwrite)")
        ws.write_json(args.config, DEFAULT_CONFIG)
        log(f"wrote default config to {args.config}")
        return 0
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]) or "retroarch_store.py",
        description="Install WarpEngine catalog releases into RetroArch playlists.",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help=f"store config file (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--base-url", help="override the catalog base URL")
    parser.add_argument("--retroarch-dir", help="override RetroArch's directory")
    parser.add_argument("--core-suffix", help="libretro core file suffix (default: this host's)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="report actions, change nothing")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version",
                        version=f"{VERSION} (warpstore {ws.VERSION})")
    sub = parser.add_subparsers(dest="command")

    p_sync = sub.add_parser("sync", help="download new/updated games and refresh playlists")
    p_sync.add_argument("name", nargs="*", help="limit to these software names")
    p_sync.add_argument("--force", action="store_true",
                        help="write even when RetroArch is running or nothing changed")
    p_sync.set_defaults(func=cmd_sync)

    p_exp = sub.add_parser("export", help="render the store for another machine (Android, an SD card)")
    p_exp.add_argument("directory", help="staging directory to write the RetroArch tree into")
    p_exp.add_argument("name", nargs="*", help="limit to these software names")
    p_exp.add_argument("--target-prefix", help="how that tree will be spelled on the target machine")
    p_exp.add_argument("--target-libretro-dir", help="the target's cores folder, if you know it")
    # Also accepted here, where an export for another OS makes it the natural
    # thing to reach for. SUPPRESS keeps the global flag's value when omitted.
    p_exp.add_argument("--core-suffix", default=argparse.SUPPRESS,
                       help="libretro core file suffix on the target (e.g. _android.so)")
    p_exp.set_defaults(func=cmd_export)

    p_list = sub.add_parser("list", help="show compatible catalog entries and core status")
    p_list.set_defaults(func=cmd_list)

    p_rm = sub.add_parser("remove", help="uninstall games (name or platform:name)")
    p_rm.add_argument("name", nargs="+")
    p_rm.add_argument("--force", action="store_true", help="write even when RetroArch is running")
    p_rm.set_defaults(func=cmd_remove)

    p_paths = sub.add_parser("paths", help="show the resolved RetroArch directories and core status")
    p_paths.set_defaults(func=cmd_paths)

    p_cfg = sub.add_parser("config", help="print the effective config")
    p_cfg.add_argument("--write", action="store_true", help="write a template config file")
    p_cfg.add_argument("--force", action="store_true")
    p_cfg.set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    ws.set_verbose(args.verbose)
    ws.init(args.config)
    cfg = ws.load_config(
        args.config, DEFAULT_CONFIG,
        overrides={"store.base_url": args.base_url},
        required=("paths.subfolder",),
    )
    os.makedirs(ws.HOME, exist_ok=True)
    return args.func(cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
