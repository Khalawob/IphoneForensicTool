#!/usr/bin/env python3
"""
iPhone FAVOURITES extractor over AFC -- USB or WiFi (async, pymobiledevice3 v11+).

Pulls only the photo-library DB, finds favourited assets, then pulls just
those files. Works over USB (default) or wirelessly (--wifi).

ONE-TIME WIFI SETUP (do this once, over USB):
    python -m pymobiledevice3 lockdown pair               # if not already paired
    python -m pymobiledevice3 lockdown wifi-connections on # enable WiFi reachability

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
import sqlite3
from pathlib import Path

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.exceptions import NoDeviceConnectedError

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


async def candidate_remote(afc, directory, filename):
    """Resolve the real AFC path (handles the 'DCIM/' prefix ambiguity)."""
    for remote in (f"/{directory}/{filename}", f"/DCIM/{directory}/{filename}"):
        remote = remote.replace("//", "/")
        if await afc.exists(remote):
            return remote
    return None


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


def main():
    parser = argparse.ArgumentParser(description="Extract iPhone favourite photos.")
    parser.add_argument("--wifi", action="store_true",
                        help="connect wirelessly instead of USB "
                             "(requires one-time 'wifi-connections on' over USB)")
    args = parser.parse_args()
    asyncio.run(extract(args.wifi))


if __name__ == "__main__":
    main()
