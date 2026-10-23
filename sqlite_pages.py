#!/usr/bin/env python3
"""Build a page-selective local copy of a remote SQLite database.

The iPhone photo library's Photos.sqlite runs to ~129MB, but the queries this
tool makes touch about a ninth of it: the bulk of the file is ACHANGE,
ZSCENECLASSIFICATION and ATRANSACTION, which are never read. Pulling the whole
file costs ~34s over WiFi at the measured ~3.8MB/s.

So instead of transferring the file, transfer the *pages*. This module walks
the SQLite file format over a ranged reader, collects every page belonging to
the b-trees the tool actually queries, and writes them into a local file of the
correct total length with zeros in the holes. Stock sqlite3 then opens that
file and runs the real queries against it -- indexes, the planner's choices and
overflow pages all behave normally, because the pages are genuinely there.

Correct by enumeration, not by prediction
-----------------------------------------
The risk in a page-selective copy is that a page SQLite wants was not fetched:
that surfaces as "database disk image is malformed", not as a page fault we can
satisfy after the fact. The defence is to never guess. want_tables() names the
handful of tables this tool queries, and every b-tree belonging to them is
walked in full -- including all 91 indexes on ZASSET. The planner is then free
to pick any index it likes, because they are all present.

Measured on a real 2,972-asset library (page_size 4096, 32,958 pages):

    page 1 + sqlite_master          37 pages
    ZASSET table                   414 pages
    ZASSET indexes (91)          1,028 pages
    ZADDITIONALASSETATTRIBUTES   1,950 pages
    its indexes (23)               230 pages
    ZGENERICALBUM + joins          109 pages
    ----------------------------------------
    total                        3,768 pages = 15.4MB = 11.4% of 135.0MB

ZADDITIONALASSETATTRIBUTES is more than half of that, and it is there for one
column: the byte size extract() compares a local copy against before spending
a transfer (see asset_size_select in extract_photos.py). Dropping it would
return the copy to ~6MB, at the price of two AFC round trips per asset the
run could otherwise have skipped untouched.

Those pages are scattered over 1,860 runs, so one ranged read per run would
cost 1,860 round trips. READ_GAP_PAGES bridges small holes to trade wasted
bytes for round trips; the measured curve on that library is:

    gap    reads   fetched
      0     2035    15.4MB
      4     1457    19.5MB
      8     1215    24.5MB      <- the default
     16      914    36.2MB
     64      268    89.3MB

Nothing here assumes the Photos schema. It is a plain SQLite file-format
reader, and it treats the remote file as untrusted: anything structurally
impossible raises MalformedDatabase so the caller can fall back to a full pull.

This module is deliberately free of any device dependency -- it talks to an
async read(offset, length) callable and nothing else -- so it can be validated
end to end against a local database with no phone attached.
"""

import inspect
import re

# B-tree page types, from the SQLite file format spec.
INTERIOR_INDEX, INTERIOR_TABLE = 2, 5
LEAF_INDEX, LEAF_TABLE = 10, 13
INTERIOR = (INTERIOR_INDEX, INTERIOR_TABLE)
LEAF = (LEAF_INDEX, LEAF_TABLE)

# Page 1 carries the 100-byte database header before its b-tree header.
FILE_HEADER_SIZE = 100

# sqlite_master is always rooted at page 1.
MASTER_ROOT = 1

# Bridge holes up to this many pages to save a round trip. See the table above.
READ_GAP_PAGES = 8

# AFC refuses reads larger than 4MB, so no single range may exceed it.
MAX_RANGE_BYTES = 4 * 1024 * 1024

# Sanity bounds. A b-tree deeper than this, or a database claiming more pages
# than this, means we are not reading what we think we are.
MAX_BTREE_DEPTH = 32
MAX_PAGE_COUNT = 1 << 28


class MalformedDatabase(Exception):
    """The remote file does not parse as a SQLite database."""


async def maybe_await(fn, *args):
    """Call fn, awaiting it if the result is awaitable.

    pymobiledevice3 turned the AFC file primitives into coroutines in v11, and
    which of them are coroutines has already shifted once within that line, so
    neither form is assumed here.
    """
    result = fn(*args)
    return await result if inspect.isawaitable(result) else result


def local_reader(path):
    """An async read(offset, length) over a local file, for offline testing.

    The device path and this one drive exactly the same walker, which is what
    makes the walk verifiable without a phone attached.
    """
    handle = open(path, "rb")

    async def read(offset, length):
        handle.seek(offset)
        return handle.read(length)

    read.close = handle.close
    return read


def _u16(buf, i):
    return int.from_bytes(buf[i:i + 2], "big")


def _u32(buf, i):
    return int.from_bytes(buf[i:i + 4], "big")


def _varint(buf, i):
    """Decode a SQLite varint at buf[i]; return (value, next_index)."""
    value = 0
    for k in range(9):
        if i + k >= len(buf):
            raise MalformedDatabase("varint runs past the end of a page")
        byte = buf[i + k]
        if k == 8:
            return (value << 8) | byte, i + 9
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, i + k + 1


async def read_header(read):
    """Return (page_size, page_count, reserved) from the database header."""
    head = await read(0, FILE_HEADER_SIZE)
    if len(head) < FILE_HEADER_SIZE or not head.startswith(b"SQLite format 3\x00"):
        raise MalformedDatabase("missing the SQLite file-format signature")

    page_size = _u16(head, 16)
    page_size = 65536 if page_size == 1 else page_size
    if page_size < 512 or page_size & (page_size - 1):
        raise MalformedDatabase(f"implausible page size {page_size}")

    # The in-header page count is only trustworthy when the file-change counter
    # matches the version-valid-for number; otherwise the file was last written
    # by something that did not maintain it, and the length has to come from
    # the caller instead.
    page_count = _u32(head, 28) if _u32(head, 24) == _u32(head, 92) else 0
    if page_count > MAX_PAGE_COUNT:
        raise MalformedDatabase(f"implausible page count {page_count}")

    return page_size, page_count, head[20]


def coalesce(pages, gap=READ_GAP_PAGES, page_size=4096):
    """Group page numbers into (first_page, count) ranges to read.

    Consecutive pages always merge; pages separated by at most `gap` unwanted
    pages merge too, fetching the hole rather than paying another round trip.
    """
    ranges = []
    max_pages = max(1, MAX_RANGE_BYTES // page_size)
    for page in sorted(pages):
        if ranges:
            first, count = ranges[-1]
            hole = page - (first + count)
            if 0 <= hole <= gap and page - first + 1 <= max_pages:
                ranges[-1] = (first, page - first + 1)
                continue
        ranges.append((page, 1))
    return ranges


class PageStore:
    """Fetches pages over a ranged reader, write-through to the output file.

    Every page that arrives is written to its real offset in the output file
    immediately, so the file is built as a side effect of the walk and nothing
    has to be buffered or reconciled afterwards. Pages pulled in only to bridge
    a hole are written too: a page SQLite did not need is harmless, while a page
    it needed and we skipped is fatal.
    """

    def __init__(self, read, out, page_size, page_count):
        self.read = read
        self.out = out
        self.page_size = page_size
        self.page_count = page_count
        self.cache = {}
        self.reads = 0
        self.fetched_bytes = 0

    async def need(self, pages):
        """Fetch any of `pages` not already held."""
        missing = set()
        for page in pages:
            if not 1 <= page <= self.page_count:
                raise MalformedDatabase(
                    f"page {page} is outside the database "
                    f"({self.page_count} pages)")
            if page not in self.cache:
                missing.add(page)
        for first, count in coalesce(missing, page_size=self.page_size):
            blob = await self.read((first - 1) * self.page_size,
                                   count * self.page_size)
            self.reads += 1
            self.fetched_bytes += len(blob)
            self.out.seek((first - 1) * self.page_size)
            self.out.write(blob)
            for i in range(count):
                chunk = blob[i * self.page_size:(i + 1) * self.page_size]
                if len(chunk) == self.page_size:
                    self.cache[first + i] = chunk

    def __getitem__(self, page):
        try:
            return self.cache[page]
        except KeyError:
            raise MalformedDatabase(f"page {page} was never fetched") from None


def _page_view(store, page):
    """Return (data, header_offset, type, cell_count) for a b-tree page."""
    data = store[page]
    offset = FILE_HEADER_SIZE if page == MASTER_ROOT else 0
    kind = data[offset]
    if kind not in INTERIOR + LEAF:
        raise MalformedDatabase(f"page {page} is not a b-tree page (type {kind})")
    return data, offset, kind, _u16(data, offset + 3)


def _cell_offsets(data, offset, kind, count, page_size):
    base = offset + (12 if kind in INTERIOR else 8)
    if base + 2 * count > page_size:
        raise MalformedDatabase("cell pointer array runs past the page")
    for i in range(count):
        cell = _u16(data, base + 2 * i)
        if not offset <= cell < page_size:
            raise MalformedDatabase(f"cell pointer {cell} is off the page")
        yield cell


def _overflow_head(data, start, payload, usable, is_index):
    """Return the first overflow page of a cell's payload, or None.

    The thresholds are the spec's: a payload longer than max_local spills, and
    K/min_local decide how much stays on the page, with a 4-byte overflow
    pointer immediately after it.
    """
    if is_index:
        max_local = ((usable - 12) * 64 // 255) - 23
    else:
        max_local = usable - 35
    if payload <= max_local:
        return None
    min_local = ((usable - 12) * 32 // 255) - 23
    k = min_local + ((payload - min_local) % (usable - 4))
    local = k if k <= max_local else min_local
    if start + local + 4 > len(data):
        raise MalformedDatabase("overflow pointer runs past the page")
    return _u32(data, start + local) or None


async def btree_pages(store, roots, reserved):
    """Return every page of the b-trees rooted at `roots`, overflow included.

    The walk is breadth-first by level, and across all the b-trees at once,
    both on purpose. An interior page names all of its children, so a whole
    level is known before any of it is fetched; doing every b-tree in lockstep
    then puts hundreds of scattered page numbers in front of the coalescer at
    the same time instead of a dozen. On the measured library that is the
    difference between 1,431 round trips and 577 for the same pages -- walking
    the 164 b-trees one after another gives coalesce() almost nothing to work
    with.
    """
    usable = store.page_size - reserved
    seen = set()
    leaves = []
    overflow_heads = []
    level = list(roots)

    for _ in range(MAX_BTREE_DEPTH):
        level = [p for p in dict.fromkeys(level) if p not in seen]
        if not level:
            break
        await store.need(level)
        children = []
        for page in level:
            seen.add(page)
            data, offset, kind, count = _page_view(store, page)
            if kind in LEAF:
                leaves.append(page)
                continue
            children.append(_u32(data, offset + 8))
            for cell in _cell_offsets(data, offset, kind, count, store.page_size):
                children.append(_u32(data, cell))
                if kind == INTERIOR_INDEX:
                    # An index interior cell carries a key, which can spill.
                    payload, start = _varint(data, cell + 4)
                    head = _overflow_head(data, start, payload, usable, True)
                    if head:
                        overflow_heads.append(head)
        level = children
    else:
        raise MalformedDatabase(
            f"a b-tree is deeper than {MAX_BTREE_DEPTH} levels")

    # Overflow chains hang off cells on both leaf kinds. The leaves are already
    # fetched, so finding the chain heads costs no further transfer.
    for page in leaves:
        data, offset, kind, count = _page_view(store, page)
        is_index = kind == LEAF_INDEX
        for cell in _cell_offsets(data, offset, kind, count, store.page_size):
            payload, start = _varint(data, cell)
            if kind == LEAF_TABLE:
                _rowid, start = _varint(data, start)
            head = _overflow_head(data, start, payload, usable, is_index)
            if head:
                overflow_heads.append(head)

    return seen | await _overflow_pages(store, overflow_heads)


async def _overflow_pages(store, heads):
    """Follow overflow chains; each page's first 4 bytes name the next."""
    pages = set()
    level = list(heads)
    while True:
        level = [p for p in dict.fromkeys(level) if p and p not in pages]
        if not level:
            return pages
        if len(pages) > MAX_PAGE_COUNT:
            raise MalformedDatabase("overflow chain does not terminate")
        await store.need(level)
        pages.update(level)
        level = [_u32(store[p], 0) for p in level]


# Tables this tool queries. Everything else in the library -- the iCloud change
# log, scene classifications, transactions -- is left on the device.
#
# ZASSET and ZGENERICALBUM are named directly. The album<->asset join tables
# carry a version-specific number (Z_28ASSETS, Z_30ASSETS, ...) so they are
# matched by shape, the same way get_albums() discovers the one it needs.
# sqlite_stat* is included because the query planner reads it when choosing an
# index: if it exists and its pages are missing, every query fails.
#
# ZADDITIONALASSETATTRIBUTES is here for one column: on an iOS 17/18-era
# library it is the only place an asset's byte size is recorded, and that size
# is what lets extract() skip an asset already on disk without asking the
# device (see asset_size_select in extract_photos.py). It is not free -- its
# b-trees roughly double the page-selective copy, from ~9% of the file to
# ~18% -- but it buys back the two AFC round-trips per skipped asset that
# candidate_remote() would otherwise spend, which is the larger cost on any
# collection of more than a few dozen assets.
WANTED_EXACT = ("ZASSET", "ZGENERICALBUM", "ZADDITIONALASSETATTRIBUTES")
WANTED_PATTERNS = (re.compile(r"Z_\d+[A-Z]*ASSETS\Z"),
                   re.compile(r"sqlite_stat\d*\Z"))


def want_tables(names):
    """Pick, from the table names in the database, the ones to make local."""
    return {name for name in names
            if name in WANTED_EXACT
            or any(p.match(name) for p in WANTED_PATTERNS)}


async def build(read, out_path, total_bytes, open_ro):
    """Build a page-selective copy of a remote database at out_path.

    `read` is an async read(offset, length) over the remote file; `open_ro`
    opens a local path read-only (the caller owns that invariant). Returns a
    stats dict for the caller to report.

    Two stages, because the root page of every b-tree is recorded in
    sqlite_master and sqlite_master is itself a b-tree. Stage one fetches only
    that b-tree, which makes the partial file readable enough for SQLite to
    decode the schema for us -- far better than hand-rolling a record parser
    over Apple's 496-object DDL. Stage two walks the b-trees it named.

    The output is a file of the database's full length with only the needed
    pages written. On NTFS the unwritten extent still occupies disk: the
    saving here is transfer, not storage.
    """
    page_size, page_count, reserved = await read_header(read)
    if not page_count:
        page_count = total_bytes // page_size
    if not page_count:
        raise MalformedDatabase("database claims to have no pages")
    total_bytes = total_bytes or page_count * page_size

    out = open(out_path, "w+b")
    try:
        out.truncate(total_bytes)
        store = PageStore(read, out, page_size, page_count)

        # Stage one: the schema b-tree, and nothing else.
        pages = await btree_pages(store, [MASTER_ROOT], reserved)
        master_pages = len(pages)
        out.flush()

        objects = _read_schema(out_path, open_ro)

        tables = want_tables(name for kind, name, _tbl, _root in objects
                             if kind == "table")
        roots = [root for _kind, _name, tbl, root in objects
                 if tbl in tables and root]

        # Stage two: every b-tree of those tables -- table and all indexes --
        # so the planner cannot pick an index we failed to fetch.
        pages |= await btree_pages(store, roots, reserved)
    finally:
        out.close()

    return {
        "page_size": page_size,
        "page_count": page_count,
        "tables": len(tables),
        "btrees": len(roots) + 1,
        "master_pages": master_pages,
        "pages": len(pages),
        "reads": store.reads,
        "fetched_bytes": store.fetched_bytes,
        "total_bytes": total_bytes,
        # The pages belonging to the wanted b-trees, as opposed to the ones
        # actually fetched: coalesce() also pulls in the holes it bridges, so
        # the file holds rather more than this. Kept for diagnostics. Note
        # this is a deliberate superset of what any one run reads -- it covers
        # every index the planner could choose, not the one it did.
        "page_set": pages,
    }


def _read_schema(path, open_ro):
    """Return [(type, name, tbl_name, rootpage), ...] from a partial database.

    Only the sqlite_master b-tree has to be present for this to work, which is
    the whole point of doing it in two stages.
    """
    con = open_ro(path)
    try:
        return con.execute(
            "SELECT type, name, tbl_name, rootpage FROM sqlite_master"
        ).fetchall()
    finally:
        con.close()
