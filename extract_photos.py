#!/usr/bin/env python3
"""
iPhone FAVOURITES extractor over AFC -- USB or WiFi (async, pymobiledevice3 v11+).

Pulls only the photo-library DB, finds favourited assets, then pulls just
those files. Works over USB (default) or wirelessly (--wifi).

ONE-TIME WIFI SETUP (do this once, over USB):
    python -m pymobiledevice3 lockdown pair               # if not already paired
    python -m pymobiledevice3 lockdown wifi-connections on # enable WiFi reachability
    python -m pymobiledevice3 lockdown unpair #for unpairing
    
    
USAGE
-----
    python extract_photos.py            # over USB
    python extract_photos.py --wifi     # over WiFi (after the setup above)

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


def get_favourite_paths(db_path):
    """Return [(ZDIRECTORY, ZFILENAME), ...] for every favourited asset."""
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT ZDIRECTORY, ZFILENAME FROM ZASSET WHERE ZHIDDEN = 1"
        ).fetchall()
    except sqlite3.OperationalError as e:
        print("schema differs on this iOS version:", e)
        return []
    finally:
        con.close()


def get_favourite_details(db_path):
    """Return list of dicts with directory, filename, date, and size for favourites."""
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
            f"SELECT {select} FROM ZASSET WHERE ZHIDDEN = 1 "
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


async def preview(use_wifi):
    from pymobiledevice3.services.afc import AfcService

    lockdown = await connect(use_wifi)
    print(f"connected to {lockdown.short_info.get('DeviceName', 'device')} "
          f"({'WiFi' if use_wifi else 'USB'})")

    DEST.mkdir(exist_ok=True)

    async with AfcService(lockdown) as afc:
        for name in ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"):
            remote = f"/PhotoData/{name}"
            if await afc.exists(remote):
                await afc.pull(remote, str(DEST), ignore_errors=True)

        db = DEST / "Photos.sqlite"
        if not db.exists():
            print("Photos.sqlite not found -- cannot determine favourites.")
            return

        details = get_favourite_details(db)
        if not details:
            print("No favourites found.")
            return

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
                    FAVS_DIR.mkdir(exist_ok=True)
                    import shutil
                    for f in opened:
                        shutil.copy2(str(f), str(FAVS_DIR / f.name))
                    print(f"Saved {len(opened)} file(s) to {FAVS_DIR}")

            for f in Path(tmp_dir).iterdir():
                f.unlink(missing_ok=True)
            Path(tmp_dir).rmdir()

            another = input("Preview more? (y/n): ").strip().lower()
            if another != "y":
                return
            format_listing(details)


async def extract(use_wifi):
    # Import here so the module loads even if AFC internals shift.
    from pymobiledevice3.services.afc import AfcService

    lockdown = await connect(use_wifi)
    print(f"connected to {lockdown.short_info.get('DeviceName', 'device')} "
          f"({'WiFi' if use_wifi else 'USB'})")

    DEST.mkdir(exist_ok=True)

    async with AfcService(lockdown) as afc:
        # 1. Pull only the DB (+ WAL sidecars).
        for name in ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"):
            remote = f"/PhotoData/{name}"
            if await afc.exists(remote):
                await afc.pull(remote, str(DEST), ignore_errors=True)

        db = DEST / "Photos.sqlite"
        if not db.exists():
            print("Photos.sqlite not found -- cannot determine favourites.")
            return

        # 2. Find favourites.
        favs = get_favourite_paths(db)
        print(f"{len(favs)} favourites recorded in the library.")
        if not favs:
            return

        # 3. Pull only those files.
        FAVS_DIR.mkdir(exist_ok=True)
        pulled, missing = 0, 0
        for directory, filename in favs:
            remote = await candidate_remote(afc, directory, filename)
            if remote is None:
                missing += 1
                print(f"  not found on device: {directory}/{filename}")
                continue
            await afc.pull(remote, str(FAVS_DIR), ignore_errors=True)
            pulled += 1

        print(f"\npulled {pulled} favourites into {FAVS_DIR}"
              + (f" ({missing} not found)" if missing else ""))


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
    parser.add_argument("--list-paired", action="store_true",
                        help="list all devices ever paired with this laptop (no device needed)")
    args = parser.parse_args()

    if args.list_paired:
        asyncio.run(list_paired_devices())
        return

    if args.preview:
        asyncio.run(preview(args.wifi))
        return

    asyncio.run(extract(args.wifi))


if __name__ == "__main__":
    main()
