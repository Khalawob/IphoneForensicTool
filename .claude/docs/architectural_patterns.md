# Architectural Patterns

Patterns that recur across `extract_photos.py`. Follow them when adding
commands, categories, or schema queries.

## 1. Schema-defensive SQL

The iPhone `Photos.sqlite` schema changes between iOS releases, so **no query
assumes a column or table exists**. Three layers of defence, used together:

- **Introspect first.** `asset_columns()` (`extract_photos.py:96`) reads
  `PRAGMA table_info(ZASSET)` and returns a set of column names. Callers check
  membership before building SQL. Same pattern on `ZGENERICALBUM`
  (`extract_photos.py:452`) and on the join table's columns
  (`extract_photos.py:470`).
- **Discover version-numbered names at runtime.** The album↔asset join table is
  called `Z_28ASSETS`, `Z_30ASSETS`, … depending on the iOS version, so it is
  found by querying `sqlite_master` with a `LIKE 'Z!_%ASSETS'` pattern and a
  regex confirmation (`extract_photos.py:461`). Column names inside it are
  matched by substring (`"ALBUM" in c`, `"ASSET" in c`).
- **Catch `sqlite3.OperationalError` at every query site** and degrade instead
  of raising: return `set()`/`[]`/`None`, or print
  `"schema differs on this iOS version"` (`extract_photos.py:103`, `:140`,
  `:157`, `:228`, `:248`, `:501`).

**Graceful-degradation convention:** an unsupported feature yields `None`
(rendered as `n/a` in tables, `-` in the album list), never an exception. See
`category_where()` returning `None` (`extract_photos.py:107`) and
`count_categories()` appending `None` counts (`extract_photos.py:121`).

**Fallback chains** pick the first available alternative via
`next((c for c in candidates if c in cols), None)`: the file-size column
(`extract_photos.py:241`) and the cached album count
(`extract_photos.py:487`).

## 2. Declarative category table

All extractable collections live in one dict, `CATEGORIES`
(`extract_photos.py:60`), mapping a CLI key to
`(label, required_columns, sql_predicate)`. Everything else is derived from it:

- `--media-type` choices come from `sorted(CATEGORIES)` (`extract_photos.py:671`)
- `category_where()` checks `required_columns` and composes the predicate
- `MEDIA_TYPE_KEYS` / `UTILITY_KEYS` (`extract_photos.py:87`, `:90`) only select
  which keys appear in which printed section

**To add a collection, add one row to `CATEGORIES`** — no other code changes.

`category_where()` also encodes a cross-cutting rule in one place: every
category except `deleted` is scoped with `AND ZTRASHEDSTATE = 0`, matching the
Photos app.

## 3. WHERE-clause as the unit of composition

Queries take a `where` string rather than a category or a filter object
(`get_asset_paths`, `get_asset_details`, `count_categories`). `resolve_category()`
(`extract_photos.py:311`) is the single translation point from a CLI key to the
`(where, label, out_dir)` triple that the read and write paths both consume, so
`preview()` and `extract()` share identical selection semantics.

Note the default: with no `--media-type`, `DEFAULT_WHERE`
(`extract_photos.py:93`) is used and the output is labelled/stored as
`favourites`.

## 4. Command dispatch in `main()`

`main()` (`extract_photos.py:642`) is a flat sequence of
`if args.<flag>: asyncio.run(<coro>(...)); return` guards, with `extract()` as
the fall-through default. Each subcommand is one `async def` taking `use_wifi`
(and optionally `category`) — no command objects, no registry.

**To add a subcommand:** add the `add_argument`, write the coroutine, add one
guard. Place the guard by precedence — device-free commands (`--setup-wifi`,
`--list-paired`) come before the ones that need a connection.

## 5. Connection and session lifecycle

Every device-touching coroutine follows the same four steps:

```
lockdown = await connect(use_wifi)      # extract_photos.py:206
print(f"connected to {lockdown.short_info.get('DeviceName', 'device')} ...")
async with AfcService(lockdown) as afc: # context manager owns the AFC session
    db = await pull_library_db(afc)     # extract_photos.py:510
    if db is None: ...return
```

- `connect()` is the only place `create_using_usbmux` is called; USB vs WiFi is
  just `connection_type="USB"|"Network"`, and `NoDeviceConnectedError` is caught
  there to print WiFi-specific troubleshooting before re-raising
  (`extract_photos.py:212`).
- `pull_library_db()` pulls only `Photos.sqlite` plus its `-wal`/`-shm`
  sidecars, returning `None` rather than raising when absent — callers always
  check.

**Lazy imports:** `pymobiledevice3.services.afc` and
`pymobiledevice3.usbmux` are imported *inside* the coroutines that need them
(`extract_photos.py:174`, `:401`, `:550`), so the module still loads (and
`--help` still works) if those internals shift between library versions.
Top-level imports are limited to `lockdown`, `exceptions` and `osu.os_utils`.

## 6. DB-first, minimal-transfer extraction

The core design decision: **query metadata locally, then pull only the files
you need.** `extract()` (`extract_photos.py:400`) is numbered in the source as
(1) pull the DB, (2) resolve the asset list from SQL, (3) pull just those
files. This keeps a WiFi run viable instead of mirroring the whole library.

`candidate_remote()` (`extract_photos.py:279`) absorbs the one path ambiguity:
AFC exposes assets at either `/{ZDIRECTORY}/{ZFILENAME}` or under a `DCIM/`
prefix, so it probes both with `afc.exists()` and returns `None` if neither
resolves. Pulls pass `ignore_errors=True` and the caller tallies
`pulled`/`missing` rather than aborting the run on one bad asset.

## 6a. The pulled DB is cached, and must be opened read-only

`pull_library_db()` compares each of `DB_FILES` against the device with
`file_is_current()` -- size plus the device's `st_mtime`, which needs no extra
bookkeeping because `afc.pull()` stamps every local file with the device's
timestamp. Files that match are reused; only the rest are pulled.
`--refresh-db` forces the transfer. The check is per file because iOS writes to
`Photos.sqlite-wal` constantly while `Photos.sqlite` changes only on
checkpoint: an all-or-nothing check would re-pull ~129MB for the sake of a few
MB of journal. Mixing a cached file with a freshly pulled one is safe -- what
lands on disk is byte-identical to the set now on the device, which is all
SQLite requires.

Two consequences follow, and both are load-bearing:

- **Every query must go through `open_db()`**, which opens `mode=ro`. A normal
  `sqlite3.connect()` checkpoints the WAL into `Photos.sqlite` and deletes the
  sidecars when the last connection closes. That rewrites the files the cache
  compares against, so the cache never hits -- and the local DB stops matching
  the device byte for byte. Do not use `immutable=1`: it skips the WAL, which
  silently hides every asset added since the last checkpoint.
- **`Photos.sqlite-shm` is never cached.** It is a derived index that SQLite
  recreates on open, so its timestamps never match the device's. It is 32KB,
  so `pull_library_db()` simply re-pulls it every run.

Local files absent on the device are deleted rather than left in place: a
leftover `-wal` from an earlier run would be read as part of the database and
misreport its contents.

## 7. Presentation separated from retrieval

Data functions return plain Python — lists of tuples (`get_asset_paths`), lists
of dicts (`get_asset_details`, `get_albums`), or `(key, label, count)` triples
(`count_categories`) — and never print results. Printing lives in dedicated
helpers that take those structures: `print_category_table()`
(`extract_photos.py:163`) and `format_listing()` (`extract_photos.py:288`).

Output is fixed-width f-string tables (`{x:<40} {y:>7}`) with a `"-" * n`
rule — no table library. Apple Core Data timestamps are converted at the
formatting boundary against the 2001-01-01 epoch (`extract_photos.py:261`).

Note the asymmetry: the data layer is silent on success but *does* print schema
diagnostics on failure, since there is no logger or exception channel.

## 8. Interactive loop in `preview()`

`preview()` (`extract_photos.py:325`) is the only stateful flow:
list → select → pull to a `tempfile.mkdtemp()` → `os.startfile()` → offer to
save to `out_dir` → clean the temp dir → ask whether to loop. Selection syntax
(`1,3,5-7`, `a`, `q`) is parsed by `parse_selection()`
(`extract_photos.py:297`), which silently drops anything out of range and
returns 0-based indices; an empty result re-prompts rather than erroring.

`os.startfile()` makes this path **Windows-only** — the rest of the script is
portable.

## 9. Verify-with-retry after a state change

`setup_wifi()` (`extract_photos.py:549`) shows the pattern for device state
that settles asynchronously: toggle `wifi-connections` **off then on** (the
reset matters), then poll `list_devices()` up to 3 times with a 2s sleep and a
progress dot, looking for `connection_type == "Network"`. On timeout it prints
the manual verification command instead of reporting failure, because the
change often lands late.

UDIDs are compared with `.replace("-", "")` on both sides
(`extract_photos.py:567`, `:598`, `:630`) — usbmux and lockdown disagree on
hyphenation.

The two `wifi-connections` toggles run via
`subprocess.run([sys.executable, "-m", "pymobiledevice3", ...], check=True)`
rather than the Python API — the one place the CLI is shelled out to.

## 10. Error-message style

User-facing failures print a cause followed by indented `  * ` checks the user
can act on, not tracebacks. `connect()` (`extract_photos.py:212`) is the
template; listing commands end with the next command to try
(`extract_photos.py:169`, `:203`). Keep new messages in this voice.
