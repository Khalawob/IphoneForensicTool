#!/usr/bin/env python3
"""
iPhone FAVOURITES extractor over AFC -- USB or WiFi (async, pymobiledevice3 v11+).

Pulls only the photo-library DB, finds favourited assets, then pulls just
those files. Works over USB (default) or wirelessly (--wifi).

ONE-TIME WIFI SETUP (do this once, over USB):
    python extract_photos.py --setup-wifi

    Or manually:
        python -m pymobiledevice3 lockdown pair               # if not already paired
        python -m pymobiledevice3 lockdown wifi-connections on # enable WiFi reachability

USAGE
-----
    python extract_photos.py            # over USB
    python extract_photos.py --wifi     # over WiFi (after the setup above)

    
Other useful commands:
    python -m pymobiledevice3 lockdown wifi-connections off # disable WiFi reachability
    python -m pymobiledevice3 lockdown unpair #for unpairing
    python -m pymobiledevice3 usbmux list # to see if the device is reachable over WiFi                          
    python extract_photos.py --list-paired
    python extract_photos.py --list-albums      # album names on the device
    python extract_photos.py --list-media-types # media types / utilities + counts
    python extract_photos.py --media-type videos           # extract one collection
    python extract_photos.py --preview --media-type screenshots
    
REQUIREMENTS FOR WIFI:
  * Phone and laptop on the SAME network/subnet (the protocol won't cross subnets).
  * Phone connected to WiFi and UNLOCKED (media files seal when the device locks).
  * Confirm the device is seen over the network first:
        python -m pymobiledevice3 usbmux list
    -- it should appear with "ConnectionType": "Network".
"""

import argparse
import asyncio
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.exceptions import NoDeviceConnectedError
from pymobiledevice3.osu.os_utils import get_os_utils

DEST = Path("iphone_extract")
FAVS_DIR = DEST / "favourites"

# Asset categories, mirroring the "Media Types" and "Utilities" sections of the
# Photos app: key -> (label, columns it needs, SQL predicate over ZASSET).
# A category whose columns are missing on a given iOS version is skipped rather
# than erroring. The ZKINDSUBTYPE / ZPLAYBACKSTYLE numbers come from the
# documented schema and have shifted between iOS releases, so
# --list-media-types also prints the raw distribution to check them against.
CATEGORIES = {
    "photos":            ("Photos",            ("ZKIND",), "ZKIND = 0"),
    "videos":            ("Videos",            ("ZKIND",), "ZKIND = 1"),
    "live":              ("Live Photos",       ("ZPLAYBACKSTYLE",), "ZPLAYBACKSTYLE = 3"),
    "animated":          ("Animated",          ("ZPLAYBACKSTYLE",), "ZPLAYBACKSTYLE = 2"),
    "screenshots":       ("Screenshots",       ("ZKIND", "ZKINDSUBTYPE"),
                          "ZKIND = 0 AND ZKINDSUBTYPE = 10"),
    "screen-recordings": ("Screen Recordings", ("ZKIND", "ZKINDSUBTYPE"),
                          "ZKIND = 1 AND ZKINDSUBTYPE = 10"),
    "panoramas":         ("Panoramas",         ("ZKIND", "ZKINDSUBTYPE"),
                          "ZKIND = 0 AND ZKINDSUBTYPE = 1"),
    "slomo":             ("Slo-mo",            ("ZKIND", "ZKINDSUBTYPE"),
                          "ZKIND = 1 AND ZKINDSUBTYPE = 2"),
    "timelapse":         ("Time-lapse",        ("ZKIND", "ZKINDSUBTYPE"),
                          "ZKIND = 1 AND ZKINDSUBTYPE = 100"),
    "bursts":            ("Bursts",            ("ZAVALANCHEUUID",),
                          "ZAVALANCHEUUID IS NOT NULL"),
    "portrait":          ("Portrait",          ("ZDEPTHTYPE",), "ZDEPTHTYPE != 0"),
    "favourites":        ("Favourites",        ("ZFAVORITE",), "ZFAVORITE = 1"),
    "hidden":            ("Hidden",            ("ZHIDDEN",), "ZHIDDEN = 1"),
    "deleted":           ("Recently Deleted",  ("ZTRASHEDSTATE",), "ZTRASHEDSTATE != 0"),
    "imports":           ("Imports",           ("ZIMPORTSESSION",),
                          "ZIMPORTSESSION IS NOT NULL"),
    "duplicates":        ("Duplicates",        ("ZDUPLICATEASSETVISIBILITYSTATE",),
                          "ZDUPLICATEASSETVISIBILITYSTATE != 0"),
}

MEDIA_TYPE_KEYS = ("photos", "videos", "live", "animated", "screenshots",
                   "screen-recordings", "panoramas", "slomo", "timelapse",
                   "bursts", "portrait")
UTILITY_KEYS = ("favourites", "hidden", "deleted", "imports", "duplicates")

# What the extractor pulls when no category is named (the original behaviour).
DEFAULT_WHERE = "ZFAVORITE = 1"


def asset_columns(db_path):
    """Return the set of column names present on ZASSET."""
    con = sqlite3.connect(db_path)
    try:
        return {row[1] for row in con.execute("PRAGMA table_info(ZASSET)")}
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


def category_where(key, cols):
    """Build the WHERE clause for a category, or None if unsupported here.

    Everything except 'deleted' excludes trashed assets, matching how the
    Photos app scopes these collections.
    """
    label, needed, predicate = CATEGORIES[key]
    if not all(c in cols for c in needed):
        return None
    if key != "deleted" and "ZTRASHEDSTATE" in cols:
        return f"({predicate}) AND ZTRASHEDSTATE = 0"
    return predicate


def count_categories(db_path, keys):
    """Return [(label, count_or_None), ...] for the given category keys."""
    cols = asset_columns(db_path)
    con = sqlite3.connect(db_path)
    out = []
    try:
        for key in keys:
            label = CATEGORIES[key][0]
            where = category_where(key, cols)
            if where is None:
                out.append((key, label, None))
                continue
            try:
                n = con.execute(f"SELECT COUNT(*) FROM ZASSET WHERE {where}").fetchone()[0]
            except sqlite3.OperationalError:
                n = None
            out.append((key, label, n))
    finally:
        con.close()
    return out


def kind_distribution(db_path):
    """Raw (ZKIND, ZKINDSUBTYPE, ZPLAYBACKSTYLE) counts, for checking the mapping."""
    cols = asset_columns(db_path)
    present = [c for c in ("ZKIND", "ZKINDSUBTYPE", "ZPLAYBACKSTYLE") if c in cols]
    if not present:
        return [], []
    group = ", ".join(present)
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            f"SELECT {group}, COUNT(*) FROM ZASSET GROUP BY {group} "
            "ORDER BY COUNT(*) DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return [], []
    finally:
        con.close()
    return present, rows


def print_category_table(title, counted):
    print(f"\n{title}")
    print(f"{'Key':<20} {'Collection':<20} {'Count':>7}")
    print("-" * 49)
    for key, label, n in counted:
        shown = "n/a" if n is None else str(n)
        print(f"{key:<20} {label:<20} {shown:>7}")


async def list_media_types(use_wifi):
    """List media-type and utility collections with asset counts."""
    from pymobiledevice3.services.afc import AfcService

    lockdown = await connect(use_wifi)
    print(f"connected to {lockdown.short_info.get('DeviceName', 'device')} "
          f"({'WiFi' if use_wifi else 'USB'})")

    async with AfcService(lockdown) as afc:
        db = await pull_library_db(afc)
        if db is None:
            print("Photos.sqlite not found -- cannot read media types.")
            return

        if not asset_columns(db):
            print("ZASSET not present -- schema differs on this iOS version.")
            return

        print_category_table("MEDIA TYPES", count_categories(db, MEDIA_TYPE_KEYS))
        print_category_table("UTILITIES", count_categories(db, UTILITY_KEYS))

        cols, rows = kind_distribution(db)
        if rows:
            print("\nRAW SUBTYPE DISTRIBUTION (to verify the mapping above)")
            header = "  ".join(f"{c:>16}" for c in cols)
            print(f"{header}  {'Count':>7}")
            print("-" * (len(header) + 9))
            for row in rows:
                cells = "  ".join(f"{str(v):>16}" for v in row[:-1])
                print(f"{cells}  {row[-1]:>7}")
        print("\n'n/a' means this iOS version's schema lacks the needed column.")
        print("Target one with:  python extract_photos.py --media-type <key>\n")


async def connect(use_wifi):
    """Open a lockdown connection over WiFi ('Network') or USB."""
    conn_type = "Network" if use_wifi else "USB"
    try:
        return await create_using_usbmux(connection_type=conn_type)
    except NoDeviceConnectedError:
        where = "over WiFi" if use_wifi else "over USB"
        print(f"No device found {where}.")
        if use_wifi:
            print("  * Did you run 'lockdown wifi-connections on' over USB first?")
            print("  * Is the phone on the same network and unlocked?")
            print("  * Check: python -m pymobiledevice3 usbmux list "
                  "(look for ConnectionType: Network)")
        raise


def get_asset_paths(db_path, where=DEFAULT_WHERE):
    """Return [(ZDIRECTORY, ZFILENAME), ...] for every asset matching 'where'."""
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            f"SELECT ZDIRECTORY, ZFILENAME FROM ZASSET WHERE {where}"
        ).fetchall()
    except sqlite3.OperationalError as e:
        print("schema differs on this iOS version:", e)
        return []
    finally:
        con.close()


def get_asset_details(db_path, where=DEFAULT_WHERE):
    """Return dicts with directory, filename, date and size for matching assets."""
    con = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(ZASSET)").fetchall()}
        size_col = next(
            (c for c in ("ZFILESIZE", "ZADJUSTEDFILESIZE", "ZORIGINALFILESIZE")
             if c in cols), None
        )
        select = "ZDIRECTORY, ZFILENAME, ZDATECREATED"
        if size_col:
            select += f", {size_col}"
        rows = con.execute(
            f"SELECT {select} FROM ZASSET WHERE {where} "
            "ORDER BY ZDATECREATED DESC"
        ).fetchall()
    except sqlite3.OperationalError as e:
        print("schema differs on this iOS version:", e)
        return []
    finally:
        con.close()

    from datetime import datetime, timedelta
    APPLE_EPOCH = datetime(2001, 1, 1)
    results = []
    for row in rows:
        directory, filename, date_created = row[0], row[1], row[2]
        file_size = row[3] if len(row) > 3 else None
        if date_created is not None:
            dt = APPLE_EPOCH + timedelta(seconds=date_created)
            date_str = dt.strftime("%Y-%m-%d %H:%M")
        else:
            date_str = "unknown"
        size_mb = f"{(file_size or 0) / 1_048_576:.1f} MB" if file_size else "-- MB"
        results.append({
            "directory": directory,
            "filename": filename,
            "date": date_str,
            "size": size_mb,
        })
    return results


async def candidate_remote(afc, directory, filename):
    """Resolve the real AFC path (handles the 'DCIM/' prefix ambiguity)."""
    for remote in (f"/{directory}/{filename}", f"/DCIM/{directory}/{filename}"):
        remote = remote.replace("//", "/")
        if await afc.exists(remote):
            return remote
    return None


def format_listing(details):
    """Print a numbered list of favourites with metadata."""
    print(f"\n{'#':>4}  {'Filename':<40} {'Date':<18} {'Size':>8}")
    print("  " + "-" * 72)
    for i, item in enumerate(details, 1):
        print(f"{i:>4}  {item['filename']:<40} {item['date']:<18} {item['size']:>8}")
    print()


def parse_selection(raw, total):
    """Parse user input like '1,3,5-7' into a sorted list of 0-based indices."""
    indices = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo, hi = int(lo.strip()), int(hi.strip())
            indices.update(range(lo - 1, hi))
        elif part.isdigit():
            indices.add(int(part) - 1)
    return sorted(i for i in indices if 0 <= i < total)


def resolve_category(db, category):
    """Return (where, label, out_dir) for a category key, or None if unusable."""
    if category is None:
        return DEFAULT_WHERE, "favourites", FAVS_DIR
    cols = asset_columns(db)
    where = category_where(category, cols)
    if where is None:
        label = CATEGORIES[category][0]
        print(f"'{label}' is not available on this iOS version "
              f"(needs {', '.join(CATEGORIES[category][1])}).")
        return None
    return where, CATEGORIES[category][0], DEST / category


async def preview(use_wifi, category=None):
    from pymobiledevice3.services.afc import AfcService

    lockdown = await connect(use_wifi)
    print(f"connected to {lockdown.short_info.get('DeviceName', 'device')} "
          f"({'WiFi' if use_wifi else 'USB'})")

    async with AfcService(lockdown) as afc:
        db = await pull_library_db(afc)
        if db is None:
            print("Photos.sqlite not found -- cannot determine favourites.")
            return

        resolved = resolve_category(db, category)
        if resolved is None:
            return
        where, label, out_dir = resolved

        details = get_asset_details(db, where)
        if not details:
            print(f"No {label.lower()} found.")
            return
        print(f"{len(details)} {label.lower()} in the library.")

        format_listing(details)

        while True:
            sel = input("Enter photo numbers to preview (e.g. 1,3,5-7), "
                        "'a' for all, or 'q' to quit: ").strip().lower()
            if sel == "q":
                return
            if sel == "a":
                chosen = list(range(len(details)))
            else:
                chosen = parse_selection(sel, len(details))
            if not chosen:
                print("No valid selection. Try again.")
                continue

            tmp_dir = tempfile.mkdtemp(prefix="iphone_preview_")
            opened = []
            for idx in chosen:
                item = details[idx]
                remote = await candidate_remote(afc, item["directory"], item["filename"])
                if remote is None:
                    print(f"  not found on device: {item['filename']}")
                    continue
                await afc.pull(remote, tmp_dir, ignore_errors=True)
                local = Path(tmp_dir) / item["filename"]
                if local.exists():
                    os.startfile(str(local))
                    opened.append(local)
                    print(f"  opened: {item['filename']}")
                else:
                    print(f"  failed to pull: {item['filename']}")

            if opened:
                save = input("\nDownload these to disk? (y/n): ").strip().lower()
                if save == "y":
                    out_dir.mkdir(parents=True, exist_ok=True)
                    import shutil
                    for f in opened:
                        shutil.copy2(str(f), str(out_dir / f.name))
                    print(f"Saved {len(opened)} file(s) to {out_dir}")

            for f in Path(tmp_dir).iterdir():
                f.unlink(missing_ok=True)
            Path(tmp_dir).rmdir()

            another = input("Preview more? (y/n): ").strip().lower()
            if another != "y":
                return
            format_listing(details)


async def extract(use_wifi, category=None):
    # Import here so the module loads even if AFC internals shift.
    from pymobiledevice3.services.afc import AfcService

    lockdown = await connect(use_wifi)
    print(f"connected to {lockdown.short_info.get('DeviceName', 'device')} "
          f"({'WiFi' if use_wifi else 'USB'})")

    async with AfcService(lockdown) as afc:
        # 1. Pull only the DB (+ WAL sidecars).
        db = await pull_library_db(afc)
        if db is None:
            print("Photos.sqlite not found -- cannot determine favourites.")
            return

        # 2. Find the assets to pull.
        resolved = resolve_category(db, category)
        if resolved is None:
            return
        where, label, out_dir = resolved

        favs = get_asset_paths(db, where)
        print(f"{len(favs)} {label.lower()} recorded in the library.")
        if not favs:
            return

        # 3. Pull only those files.
        out_dir.mkdir(parents=True, exist_ok=True)
        pulled, missing = 0, 0
        for directory, filename in favs:
            remote = await candidate_remote(afc, directory, filename)
            if remote is None:
                missing += 1
                print(f"  not found on device: {directory}/{filename}")
                continue
            await afc.pull(remote, str(out_dir), ignore_errors=True)
            pulled += 1

        print(f"\npulled {pulled} {label.lower()} into {out_dir}"
              + (f" ({missing} not found)" if missing else ""))


# Album kinds seen in ZGENERICALBUM; anything else is printed as its raw number.
ALBUM_KINDS = {2: "album", 1500: "folder", 1505: "folder"}


def get_albums(db_path):
    """Return [{title, kind, count}, ...] for every named album in the library.

    Asset counts come from the album<->asset join table, whose name carries a
    version-specific number (Z_28ASSETS, Z_30ASSETS, ...), so it is discovered
    at runtime. If it cannot be found, a cached count column is used instead.
    """
    con = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(ZGENERICALBUM)")}
        if not cols:
            print("ZGENERICALBUM not present -- schema differs on this iOS version.")
            return []

        join = next(
            (name for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'Z!_%ASSETS' ESCAPE '!'"
            ) if re.fullmatch(r"Z_\d+ASSETS", name)),
            None,
        )
        album_col = asset_col = None
        if join:
            join_cols = [row[1] for row in con.execute(f"PRAGMA table_info({join})")]
            album_col = next((c for c in join_cols if "ALBUM" in c), None)
            asset_col = next((c for c in join_cols if "ASSET" in c), None)

        kind = "ZKIND" if "ZKIND" in cols else None

        if join and album_col and asset_col:
            rows = con.execute(
                f"SELECT a.ZTITLE, {f'a.{kind}' if kind else 'NULL'}, "
                f"COUNT(j.{asset_col}) "
                f"FROM ZGENERICALBUM a "
                f"LEFT JOIN {join} j ON j.{album_col} = a.Z_PK "
                "WHERE a.ZTITLE IS NOT NULL AND a.ZTITLE != '' "
                "GROUP BY a.Z_PK ORDER BY a.ZTITLE COLLATE NOCASE"
            ).fetchall()
        else:
            cached = next(
                (c for c in ("ZCACHEDCOUNT", "ZCACHEDPHOTOSCOUNT") if c in cols), None
            )
            rows = con.execute(
                f"SELECT ZTITLE, {kind or 'NULL'}, {cached or 'NULL'} "
                "FROM ZGENERICALBUM "
                "WHERE ZTITLE IS NOT NULL AND ZTITLE != '' "
                "ORDER BY ZTITLE COLLATE NOCASE"
            ).fetchall()
    except sqlite3.OperationalError as e:
        print("schema differs on this iOS version:", e)
        return []
    finally:
        con.close()

    return [
        {
            "title": title,
            "kind": ALBUM_KINDS.get(kind_val, str(kind_val) if kind_val is not None else "-"),
            "count": count,
        }
        for title, kind_val, count in rows
    ]


async def pull_library_db(afc):
    """Pull Photos.sqlite (+ WAL sidecars) into DEST; return the DB path or None."""
    DEST.mkdir(exist_ok=True)
    for name in ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"):
        remote = f"/PhotoData/{name}"
        if await afc.exists(remote):
            await afc.pull(remote, str(DEST), ignore_errors=True)
    db = DEST / "Photos.sqlite"
    return db if db.exists() else None


async def list_albums(use_wifi):
    """List the album names on the device with their photo counts."""
    from pymobiledevice3.services.afc import AfcService

    lockdown = await connect(use_wifi)
    print(f"connected to {lockdown.short_info.get('DeviceName', 'device')} "
          f"({'WiFi' if use_wifi else 'USB'})")

    async with AfcService(lockdown) as afc:
        db = await pull_library_db(afc)
        if db is None:
            print("Photos.sqlite not found -- cannot read albums.")
            return

        albums = get_albums(db)
        if not albums:
            print("No albums found.")
            return

        print(f"\n{'Album':<40} {'Kind':<10} {'Photos':>7}")
        print("-" * 59)
        for a in albums:
            count = "-" if a["count"] is None else str(a["count"])
            print(f"{a['title'][:40]:<40} {a['kind']:<10} {count:>7}")
        print("-" * 59)
        print(f"{len(albums)} album(s)\n")


async def setup_wifi():
    """Pair over USB and enable WiFi connections in one step."""
    from pymobiledevice3.usbmux import list_devices

    print("Connecting over USB...")
    try:
        lockdown = await create_using_usbmux(
            connection_type="USB", autopair=True, pair_timeout=30
        )
    except NoDeviceConnectedError:
        print("No device found over USB. Plug in your iPhone and try again.")
        return

    device_name = lockdown.short_info.get("DeviceName", "device")
    udid = lockdown.short_info.get("UniqueDeviceID", "")
    udid_norm = udid.replace("-", "")
    print(f"Paired with {device_name}.")
    await lockdown.close()

    import subprocess, sys
    print("Resetting WiFi connections...")
    subprocess.run(
        [sys.executable, "-m", "pymobiledevice3", "lockdown", "wifi-connections", "off"],
        check=True,
    )
    await asyncio.sleep(1)

    print("Enabling WiFi connections...")
    subprocess.run(
        [sys.executable, "-m", "pymobiledevice3", "lockdown", "wifi-connections", "on"],
        check=True,
    )
    print("WiFi connections enabled.")

    print("Verifying network reachability", end="", flush=True)
    for _ in range(3):
        await asyncio.sleep(2)
        print(".", end="", flush=True)
        try:
            for dev in await list_devices():
                if dev.serial.replace("-", "") == udid_norm and dev.connection_type == "Network":
                    print(f"\n\nDone! {device_name} is reachable over WiFi.")
                    print("You can now run:  python extract_photos.py --wifi")
                    return
        except Exception:
            pass

    print("\n\nWiFi connections enabled, but the device isn't showing up over "
          "the network yet.")
    print("This can take up to a minute. Verify with:")
    print("  python -m pymobiledevice3 usbmux list")
    print("Once it appears with ConnectionType: Network, you're good to go.")


async def list_paired_devices():
    """Print all paired devices and check which are currently reachable."""
    from pymobiledevice3.usbmux import list_devices

    record_path = get_os_utils().pair_record_path
    if not record_path.exists():
        print(f"No pairing record directory found at {record_path}")
        return

    udid_pattern = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{16}$")
    paired = [f.stem for f in record_path.glob("*.plist") if udid_pattern.match(f.stem)]
    if not paired:
        print("No paired devices found.")
        return

    connected = {}
    try:
        for dev in await list_devices():
            key = dev.serial.replace("-", "")
            if key in connected:
                connected[key].append(dev.connection_type)
            else:
                connected[key] = [dev.connection_type]
    except Exception:
        pass

    print(f"Found {len(paired)} paired device(s):\n")
    for udid in paired:
        normalized = udid.replace("-", "")
        types = connected.get(normalized)
        if types:
            status = ", ".join(f"reachable ({t})" for t in types)
        else:
            status = "not connected"
        print(f"  UDID:   {udid}")
        print(f"  Status: {status}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Extract iPhone favourite photos.")
    parser.add_argument("--wifi", action="store_true",
                        help="connect wirelessly instead of USB "
                             "(requires one-time 'wifi-connections on' over USB)")
    parser.add_argument("--preview", action="store_true",
                        help="browse and preview favourites without downloading")
    parser.add_argument("--setup-wifi", action="store_true",
                        help="one-time setup: pair over USB and enable WiFi connections")
    parser.add_argument("--list-paired", action="store_true",
                        help="list all devices ever paired with this laptop (no device needed)")
    parser.add_argument("--list-albums", action="store_true",
                        help="list the album names on the device with photo counts")
    parser.add_argument("--list-media-types", action="store_true",
                        help="list media-type and utility collections with counts")
    parser.add_argument("--media-type", metavar="KEY", choices=sorted(CATEGORIES),
                        help="target one collection instead of the default set; "
                             "keys: " + ", ".join(sorted(CATEGORIES)))
    args = parser.parse_args()

    if args.setup_wifi:
        asyncio.run(setup_wifi())
        return

    if args.list_paired:
        asyncio.run(list_paired_devices())
        return

    if args.list_albums:
        asyncio.run(list_albums(args.wifi))
        return

    if args.list_media_types:
        asyncio.run(list_media_types(args.wifi))
        return

    if args.preview:
        asyncio.run(preview(args.wifi, args.media_type))
        return

    asyncio.run(extract(args.wifi, args.media_type))


if __name__ == "__main__":
    main()
