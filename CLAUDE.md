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

**Windows-only** in one place: `--preview` opens files with `os.startfile()`.

## Structure

Two flat modules, no package layout.

| Path | Purpose |
|---|---|
| `extract_photos.py` | The tool: CLI, device connection, SQL queries, transfer. The module docstring is the user-facing usage reference. |
| `sqlite_pages.py` | SQLite file-format reader behind `--partial-db`: walks b-trees over a ranged reader and builds a page-selective local copy. No device dependency, so it is verifiable offline. |
| `iphone_extract/` | Output directory, created at runtime. Holds the pulled `Photos.sqlite` plus one subdirectory per extracted collection. **Gitignored** — personal data, large. |
| `.claude/docs/` | Extended documentation for Claude (see below) |

Key landmarks in `extract_photos.py`:

- `CATEGORIES` — the declarative table of every extractable collection
- `main()` — argparse setup and command dispatch
- `connect()` — the single USB/WiFi connection point
- `pull_library_db()` — pulls `Photos.sqlite` (+ WAL sidecars), or dispatches
  to the page-selective path and falls back here if it does not work out
- `pull_partial_library_db()` / `afc_reader()` / `partial_db_is_usable()` —
  the `--partial-db` path and its fallback guard
- `extract()` / `preview()` — the two output paths
- `setup_wifi()` — one-time pairing + WiFi enablement

In `sqlite_pages.py`: `build()` is the entry point, `btree_pages()` the walk,
`want_tables()` the list of tables made local, `local_reader()` the offline
test harness.

## Commands

No build or test harness. Run the script directly.

```bash
# One-time WiFi setup (must be over USB)
python extract_photos.py --setup-wifi

# Extract (default collection, over USB)
python extract_photos.py

# Extract over WiFi
python extract_photos.py --wifi

# Transfer only the Photos.sqlite pages the queries read (~18% of the file)
python extract_photos.py --wifi --partial-db

# Target one collection; keys come from CATEGORIES
python extract_photos.py --media-type videos
python extract_photos.py --media-type screenshots --wifi

# Assets already in the output directory are skipped; --force re-pulls them
python extract_photos.py --media-type videos --force

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
- **Adding a collection** means adding one row to `CATEGORIES` and nothing
  else. But if it queries a *table* the tool has not touched before, add that
  table to `want_tables()` in `sqlite_pages.py` too, or `--partial-db` will not
  have its pages. The fallback catches the mistake; it does not fix it.
- **`sqlite_pages.py` can be tested without a phone.** It reads through an
  async `read(offset, length)` callable, and `local_reader()` supplies one over
  a local file, so a change to the walk can be validated against a real
  `Photos.sqlite` by diffing query results against the full file.
- **Keep the module docstring current** — it is the only user docs;
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
  lazy imports, DB-first minimal transfer, the cached and page-selective DB
  copies (§6a, §6b), data/presentation separation, the interactive preview
  loop, verify-with-retry after device state changes, and error-message style.
  **Read before adding a command, a category, or any new query.**
