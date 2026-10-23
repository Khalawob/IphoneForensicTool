# CLAUDE.md

## Project Overview

A command-line tool that extracts photos and videos off an iPhone to a Windows
laptop over **USB or WiFi**, without iTunes/iCloud.

The point is selective, low-bandwidth extraction: it pulls only the photo
library database (`Photos.sqlite`), queries it locally with SQL to decide which
assets are wanted, then transfers just those files. That makes a wireless run
practical instead of mirroring the whole library.

Assets can be targeted by the same collections the Photos app shows — media
types (photos, videos, live, screenshots, panoramas, bursts, portrait, …) and
utilities (favourites, hidden, recently deleted, duplicates, …).

## Tech Stack

- **Python 3** (3.8+; asyncio throughout, `pathlib`, `argparse`)
- **[pymobiledevice3](https://github.com/doronz88/pymobiledevice3) v11+** —
  pure-Python lockdown/usbmux/AFC client. The async API is required (v11
  changed `create_using_usbmux` and `AfcService` to coroutines).
- **sqlite3** (stdlib) — reads the pulled iOS Photos database
- No third-party deps beyond pymobiledevice3; no requirements file, no tests,
  no build step.

**Windows-only** in one place: `--preview` opens files with `os.startfile()`
(`extract_photos.py:375`).

## Structure

Single-module project — there is no package layout.

| Path | Purpose |
|---|---|
| `extract_photos.py` | The entire tool: CLI, device connection, SQL queries, transfer. The module docstring (`:1-37`) is the user-facing usage reference. |
| `iphone_extract/` | Output directory, created at runtime. Holds the pulled `Photos.sqlite` plus one subdirectory per extracted collection. **Gitignored** — personal data, large. |
| `.claude/docs/` | Extended documentation for Claude (see below) |

Key landmarks in `extract_photos.py`:

- `CATEGORIES` (`:60`) — the declarative table of every extractable collection
- `main()` (`:642`) — argparse setup and command dispatch
- `connect()` (`:206`) — the single USB/WiFi connection point
- `pull_library_db()` (`:510`) — pulls `Photos.sqlite` (+ WAL sidecars)
- `extract()` (`:400`) / `preview()` (`:325`) — the two output paths
- `setup_wifi()` (`:549`) — one-time pairing + WiFi enablement

## Commands

No build or test harness. Run the script directly.

```bash
# One-time WiFi setup (must be over USB)
python extract_photos.py --setup-wifi

# Extract (default collection, over USB)
python extract_photos.py

# Extract over WiFi
python extract_photos.py --wifi

# Target one collection; keys come from CATEGORIES
python extract_photos.py --media-type videos
python extract_photos.py --media-type screenshots --wifi

# Inspect the library without downloading
python extract_photos.py --list-albums
python extract_photos.py --list-media-types   # collections + raw subtype counts
python extract_photos.py --list-paired        # no device needed

# Interactive browse / open / selectively save
python extract_photos.py --preview --media-type favourites
```

Install the dependency with `pip install -U pymobiledevice3`.

### Verifying device reachability

Most failures are connectivity, not code. Check before debugging:

```bash
python -m pymobiledevice3 usbmux list              # "ConnectionType": "Network" = WiFi works
python -m pymobiledevice3 lockdown pair            # pair if needed
python -m pymobiledevice3 lockdown wifi-connections on|off
python -m pymobiledevice3 lockdown unpair
```

WiFi requires: phone and laptop on the **same subnet**, phone on WiFi and
**unlocked** (media files seal when the device locks).

## Working in This Codebase

- **Never assume the SQLite schema.** Apple's `Photos.sqlite` layout shifts
  between iOS versions. Introspect with `PRAGMA table_info`, guard every query,
  and degrade to `None`/`n/a` rather than raising. See the patterns doc.
- **Adding a collection** means adding one row to `CATEGORIES` (`:60`) and
  nothing else.
- **Keep the module docstring current** (`:1-37`) — it is the only user docs;
  new flags belong in its USAGE section and in `argparse` help.
- **Never commit anything under `iphone_extract/`.** It contains personal
  photos and a copy of the photo database. `.gitignore` covers it and `*.sqlite`.
- Treat the pulled database as untrusted read-only input; it is device data.
- Testing requires a physical paired iPhone — there are no fixtures or mocks,
  so changes to the device paths cannot be verified without hardware. Say so
  rather than claiming a change is verified.

## Additional Documentation

Check these when the work touches their topic:

- **`.claude/docs/architectural_patterns.md`** — the recurring patterns and
  design decisions: schema-defensive SQL, the declarative `CATEGORIES` table,
  WHERE-clause composition, command dispatch, connection/AFC lifecycle and
  lazy imports, DB-first minimal transfer, data/presentation separation, the
  interactive preview loop, verify-with-retry after device state changes, and
  error-message style. **Read before adding a command, a category, or any new
  query.**
