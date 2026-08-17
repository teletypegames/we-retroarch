# warp-engine-retroarch-store — a RetroArch store engine for WarpEngine sites

Pulls games from a [WarpEngine](https://git.teletypegames.org/engines/warp_engine)
catalog into RetroArch's own library: one `.lpl` playlist per platform, with box
art in the thumbnail folders that playlist's name points at.

This repository is the engine only. It knows the WarpEngine API but nothing about
any particular site: the host, the store's name and where its games land all come
from a `config.json` that lives in a separate **store repository**. The shared
part — catalog, releases, state, HTTP — is
[`warpstore`](https://git.teletypegames.org/engines/warpstore), which this engine
has in common with the [Batocera engine](https://git.teletypegames.org/tools/warp-engine-batocera-store).

```
warpstore                     the shared core
    ▲
warp-engine-retroarch-store   this engine — retroarch_store.py, install.sh
    ▲
    │ config.json
ttg-retroarch-store           store repositories
    ▼
teletypegames.org
```

The reference store is
[`ttg-retroarch-store`](https://git.teletypegames.org/tools/ttg-retroarch-store).

```
<store>-retroarch-store sync
        │
        ├─ GET /api/software                      the whole catalog
        ├─ keep platforms a libretro core boots   c64 → VICE, tic80 → TIC-80
        ├─ pick the newest non-dev release        that carries the cartridge
        ├─ GET /api/download?path=<asset>         .prg / .tic into the content folder
        ├─ GET <software.imageUrl>                box art, converted to PNG
        └─ write <store> - <label>.lpl            playlist + thumbnails
```

Python 3 standard library only.

## Why this is not the Batocera store

RetroArch is not one machine. The same playlist format is read on desktop Linux,
Windows and macOS, on Android, on the Steam Deck and on the console ports — so
the engine assumes nothing about where things are:

- **It reads `retroarch.cfg`.** The playlist folder is not necessarily inside the
  RetroArch directory: on the macOS install this was developed against,
  `playlist_directory` is `~/Documents/RetroArch/playlists` while the cores and
  thumbnails are under `~/Library/Application Support/RetroArch`. An engine that
  assumed `<base>/playlists` would write into the void. `~` is expanded, and so is
  the leading `:` a portable install uses to mean its own directory.
- **It can write for a machine it is not running on** (`export`). Android has no
  Python, so the only way to serve it is to render the tree elsewhere and copy it
  over. See *Another machine* below.
- **It never edits `retroarch.cfg`.** Nothing the user configured is touched.

What it does *not* do, compared with Batocera: a `.lpl` entry has no description
or developer field, so the catalog's `desc` and `author` are lost — RetroArch
takes that from its own databases, which are a binary format outside this
engine's scope. And RetroArch cannot launch scripts, so there is no in-app
trigger: the sync runs from a shell or from a scheduler.

## Installing a store

The installer takes the store as a parameter, so it works with any config:

```sh
STORE_CONFIG=https://git.example.org/tools/my-store/raw/branch/master/config.json \
  bash <(curl -fsSL https://git.teletypegames.org/tools/warp-engine-retroarch-store/raw/branch/master/install.sh)
```

From a checkout, with a local config:

```sh
STORE_CONFIG=./my-config.json ./install.sh
```

It reads `store.id` from the config, installs the engine, the shared core and the
config into `~/.local/share/warp-engine-store/<id>/`, writes a
`<id>-retroarch-store` launcher into `~/.local/bin`, prints the paths it resolved
and runs the first sync.

| Environment variable | Meaning |
|---|---|
| `STORE_CONFIG` | **required** — path or URL of the store's `config.json` |
| `STORE_ROOT` | where stores live (default `${XDG_DATA_HOME:-~/.local/share}/warp-engine-store`) |
| `BIN_DIR` | where the launcher goes (default `~/.local/bin`) |
| `RETROARCH_DIR` | RetroArch's directory, if it is somewhere unusual |
| `STORE_SKIP_SYNC` | `1` to install without syncing |
| `ENGINE_RAW_BASE` | where to fetch `retroarch_store.py` from |
| `WARPSTORE_RAW_BASE` | where to fetch `warpstore.py` from |
| `WARPSTORE_SRC` | a local `warpstore.py` to install instead of downloading it |

## Use

```sh
S=~/.local/bin/example-retroarch-store

$S paths                # resolved directories and whether the cores are there
$S list                 # compatible catalog entries; * installed, ^ update available
$S sync                 # download everything new, refresh the playlists
$S sync blessingofra    # just one title (never prunes)
$S -n sync              # dry run
$S remove c64:c64demo   # uninstall (bare name works too)
$S config               # effective configuration
```

Global flags go **before** the subcommand: `$S --retroarch-dir /mnt/ra sync`.

**Restart RetroArch after a sync.** It builds its menu at startup, so a new
playlist does not appear in a running instance.

**A sync refuses to run while RetroArch is up.** RetroArch holds its playlists in
memory and writes them back when it exits — favourites, last played, sort order —
so writing underneath it can be undone. Close it, or pass `--force` if you know
what you are doing (`behavior.refuse_while_running: false` to stop asking).

## Where things land

```
~/.local/share/warp-engine-store/example/
├── retroarch_store.py, warpstore.py, config.json
└── state.json, catalog.json

<content_root>/example/c64/           blessingofra-2.0.0.prg, rabbit-1.0.0.prg, …
<playlists_dir>/Example Store - Commodore 64.lpl
<thumbnails_dir>/Example Store - Commodore 64/Named_Boxarts/Blessing of Ra.png
                                             /Named_Titles/…  /Named_Snaps/…
```

`content_root` defaults to `<retroarch_dir>/content`; the other two come from
`retroarch.cfg`. Run `paths` to see what was resolved and why.

Everything lands under the store's own `paths.subfolder`, which is what a prune
is allowed to touch — never content you put there yourself, never another
store's. The playlist file carries the store's name, so it is ours; but an entry
*you* added to it survives a rewrite, because only items pointing inside the
store's content folder are replaced. The first time a playlist is touched it is
copied to `<name>.lpl.<store id>-backup`.

## The playlist

```json
{
  "version": "1.5",
  "default_core_path": "/…/cores/vice_x64_libretro.dylib",
  "default_core_name": "VICE x64",
  "label_display_mode": 0,
  "right_thumbnail_mode": 0,
  "left_thumbnail_mode": 0,
  "thumbnail_match_mode": 0,
  "sort_mode": 0,
  "items": [
    {
      "path": "/…/content/example/c64/blessingofra-2.0.0.prg",
      "entry_slot": -1,
      "label": "Blessing of Ra",
      "core_path": "DETECT",
      "core_name": "DETECT",
      "crc32": "4253CFC6|crc",
      "db_name": "Example Store - Commodore 64.lpl"
    }
  ]
}
```

- **One playlist per platform**, because `default_core_path` is a *header* field:
  that is the only way every entry starts with one click and no core prompt. The
  entries themselves stay on `DETECT`, so the file still works on a machine whose
  cores live somewhere else — the worst case is one prompt, not a dead entry.
- **The name is the namespace.** `{store} - {label}` means we never write into
  RetroArch's own `Commodore - 64.lpl` and never collide with the official
  thumbnail packs. It is also the `db_name`, which is how RetroArch finds the
  thumbnails: `<thumbnails_dir>/<db_name without .lpl>/Named_*/`.
- **The display fields are all `0`** — whatever the user has set globally. A store
  has no business overriding how someone likes their library displayed.
- **`crc32` is real**, computed from the downloaded file, which lets RetroArch tie
  saves and thumbnails to the content. Turn it off with `playlist.write_crc32`.

### A missing core is a note, not an error

If the core named for a platform is not in `libretro_directory`, the playlist is
still written: `default_core_path` stays empty and the entries stay on `DETECT`,
so RetroArch asks once which core to use. `list` and `paths` say so, with the fix:

```
c64: core not found: vice_x64_libretro.dylib — entries stay on DETECT
     (Online Updater ▸ Core Downloader ▸ VICE x64)
```

A store that refused to run until the user had installed cores would never run
for anyone the first time.

### Box art must be PNG

RetroArch looks for `<label>.png` and nothing else. The catalog serves whatever
was uploaded — one of ours is a GIF — and the standard library cannot re-encode an
image, so the engine shells out to whatever the machine has: `sips` (built into
macOS), `magick`, `convert` or `ffmpeg`, first one found. With none of them the
entry simply has no box art, which beats a failed sync.

The cover is downloaded once and placed in all three `Named_*` folders (hardlinked
where the filesystem allows), so whichever thumbnail type the user has selected,
there is an image for it. `playlist.thumbnail_kinds` narrows that.

## Another machine: `export`

`sync` installs into this machine's RetroArch. `export` renders the whole store
into a canonical tree meant for somewhere else — the only way to serve Android,
which runs RetroArch but cannot run Python.

```sh
# Android over adb
$S export /tmp/ra-example \
    --target-prefix /storage/emulated/0/RetroArch \
    --target-libretro-dir /data/data/com.retroarch.aarch64/cores \
    --core-suffix _android.so
adb push /tmp/ra-example/. /storage/emulated/0/RetroArch/

# An SD card mounted here, but at /mnt/sdcard on the handheld
$S export /Volumes/SDCARD/RetroArch --target-prefix /mnt/sdcard/RetroArch
```

The tree is always `<dir>/playlists`, `<dir>/thumbnails`, `<dir>/content`.
`--target-prefix` is what the playlists say the content is called on the machine
that will run it; without it, the export directory's own path is used (right when
the export lands where it will stay). Target paths are written the way the target
spells them — POSIX unless the prefix looks like Windows.

`export` is **stateless on purpose**: it is a complete picture of the catalog, not
an incremental update, and it must not disturb the state of a local install living
in the same store home. `--target-libretro-dir` is taken on trust, since the other
machine cannot be inspected; leave it out and the entries stay on `DETECT`.

**Not verified on a device yet.** The Android paths above are the documented
defaults, not measurements — read the device's own `retroarch.cfg` if a push does
not show up, and whether scoped storage lets RetroArch read pushed content is the
open question.

## Writing a store repository

Three files:

```
my-retroarch-store/
├── config.json     the store: URL, name, subfolder, platform → core mapping
├── install.sh      a wrapper that hands that config to the engine installer
└── README.md
```

`install.sh` in full:

```sh
#!/bin/bash
set -euo pipefail
ENGINE_RAW_BASE="${ENGINE_RAW_BASE:-https://git.teletypegames.org/tools/warp-engine-retroarch-store/raw/branch/master}"
export STORE_CONFIG="${STORE_CONFIG:-https://git.example.org/tools/my-retroarch-store/raw/branch/master/config.json}"
curl -fsSL "$ENGINE_RAW_BASE/install.sh" | bash
```

Pick a `store.id` and a `paths.subfolder` nobody else uses — they are what keeps
two stores on one machine from deleting each other's games.

## Configuration

`config.json` in the store home, template in `config.example.json`
(`config --write` regenerates it):

| Key | Default | Meaning |
| --- | --- | --- |
| `store.id` | `warp` | slug: names the store home, log prefix and backups |
| `store.name` | `WarpEngine Store` | goes into the playlist name — what the user sees |
| `store.base_url` | — | WarpEngine host |
| `store.api.catalog` / `.download` | `/api/software`, `/api/download` | endpoints, if the engine is mounted elsewhere |
| `paths.retroarch_dir` | `null` | RetroArch's directory; null = env, then the per-OS candidates |
| `paths.playlists_dir` | `null` | null = from `retroarch.cfg`, then `<retroarch_dir>/playlists` |
| `paths.thumbnails_dir` | `null` | as above, `thumbnails` |
| `paths.libretro_dir` | `null` | as above, `cores` |
| `paths.content_root` | `null` | null = `<retroarch_dir>/content` |
| `paths.subfolder` | `warp` | this store's folder inside it — the prune boundary |
| `paths.target_prefix` | `null` | default for `export --target-prefix` |
| `playlist.name_template` | `{store} - {label}` | `store`, `label`, `platform` are substituted |
| `playlist.write_crc32` | `true` | compute the content CRC32 for each entry |
| `playlist.thumbnail_kinds` | all three `Named_*` | which thumbnail folders to fill |
| `catalog.statuses` | `["released", "archived"]` | catalog `status` values to install |
| `catalog.owner_id` | `null` | restrict to one publisher |
| `catalog.only` / `catalog.exclude` | `[]` | software-name allow / deny lists |
| `platforms` | c64, tic80 | platform → label, asset kind, extension, core, enabled |
| `behavior.prune` | `true` | remove games that left the catalog or the filters |
| `behavior.timeout` | `30` | HTTP timeout, seconds |
| `behavior.insecure` | `false` | skip TLS verification (self-hosted test instances) |
| `behavior.refuse_while_running` | `true` | do not write while RetroArch is running |
| `behavior.convert_images` | `true` | convert non-PNG box art with an external tool |

### Platform mapping

A catalog platform is installable when a libretro core boots its asset directly:

| Catalog platform | Asset kind | Core | Extension |
| --- | --- | --- | --- |
| `c64` | `cartridge` | `vice_x64_libretro` (VICE x64) | `.prg` |
| `tic80` | `cartridge` | `tic80_libretro` (TIC-80) | `.tic` |

Both cores are built for every relevant target — macOS x86_64/arm64, Linux
x86_64/aarch64/armhf, Windows x86_64, Android arm64-v8a/armeabi-v7a — so the
catalogue's cartridges run everywhere RetroArch does. Adding a platform is a
config edit:

```json
"platforms": {
  "nes": { "label": "NES", "kind": "cartridge", "ext": ".nes",
           "core": { "file": "fceumm_libretro", "name": "FCEUmm" }, "enabled": true }
}
```

`kind` and `ext` may be per-host maps rather than strings — `{"windows": …, "*": …}`,
see `warpstore.resolve_for_host` — but for a cartridge they never need to be: it
is data for an emulator, identical on every machine.

The other WarpEngine platforms (`ebitengine`, `love`, `godot`, `bevy`, `phaser`)
ship web builds and per-OS native archives. No libretro core runs those, so this
engine cannot serve them; on Batocera they install as Ports instead.

## State

`state.json` records what this store installed, keyed `<platform>:<name>` so the
same software name on two platforms stays two entries. Re-running a sync is
idempotent: files already present are left alone, and a release bump
(`1.0` → `2.0.0`) deletes the old asset, its thumbnails and its playlist entry
before fetching the new one.
