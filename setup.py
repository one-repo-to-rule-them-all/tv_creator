#!/usr/bin/env python3
"""
setup.py — ErsatzTV end-to-end setup via direct SQLite database manipulation.

ErsatzTV is a Blazor Server app with no REST API for setup operations.
This script configures ErsatzTV by writing directly to its SQLite database.

Usage:
    python setup.py                        # full setup
    python setup.py --dry-run              # show what would be done, no writes
    python setup.py --populate-collection  # add all scanned MediaItems to collection
    python setup.py --status               # show current DB state
"""
import argparse
import os
import random
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── Config ───────────────────────────────────────────────────────────────────

# Path to ErsatzTV SQLite database.
# Docker default: ./ersatztv-config/ersatztv.sqlite3  (relative to this script)
# Native default: %LOCALAPPDATA%\ersatztv\ersatztv.sqlite3
_script_dir = Path(__file__).parent
_default_db = str(_script_dir / "ersatztv-config" / "ersatztv.sqlite3")

ETV_DB_PATH  = os.getenv("ETV_DB_PATH", _default_db)
ETV_HOST     = os.getenv("ETV_HOST", "http://localhost:8409")

# Base path inside the container (or on the host for native ErsatzTV).
# Docker: /nas  (mounted from NAS via docker-compose)
# Native: Z:/   (mapped NAS drive)
NAS_CONTAINER_PATH = os.getenv("NAS_CONTAINER_PATH", "/nas").rstrip("/")

CHANNEL_NUMBER   = os.getenv("CHANNEL_NUMBER",   "2")
CHANNEL_NAME     = os.getenv("CHANNEL_NAME",     "My Media")
CHANNEL_GROUP    = os.getenv("CHANNEL_GROUP",    "IPTV")
SCHEDULE_NAME    = os.getenv("SCHEDULE_NAME",    "My Media Schedule")
COLLECTION_NAME  = os.getenv("COLLECTION_NAME",  "All My Media")

# ─── ErsatzTV enum constants ──────────────────────────────────────────────────

MEDIA_KIND_MOVIE       = 1
MEDIA_KIND_SHOW        = 2
MEDIA_KIND_MUSIC_VIDEO = 3
MEDIA_KIND_OTHER_VIDEO = 4

COLLECTION_TYPE_COLLECTION = 0   # regular hand-curated collection

PLAYBACK_ORDER_SHUFFLE = 1       # shuffle items in the collection

GUIDE_MODE_LIVE = 0

SCHEDULE_KIND_PROGRAM = 0        # ProgramSchedule-backed playout

STREAMING_MODE_MPEGTS = 5        # matches the ErsatzTV default channel

# ─── Library definitions ──────────────────────────────────────────────────────
# (display_name, media_kind, subpath_under_NAS_CONTAINER_PATH)
# Paths become: {NAS_CONTAINER_PATH}/{subpath}
LIBRARIES = [
    ("Movies",          MEDIA_KIND_MOVIE,       "Movies"),
    ("Tv Shows",        MEDIA_KIND_SHOW,        "Tv Shows"),
    ("Fitness",         MEDIA_KIND_OTHER_VIDEO,  "Fitness"),
    ("Stand up Comedy", MEDIA_KIND_OTHER_VIDEO,  "Stand up Comedy"),
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def now_utc_str() -> str:
    """EF Core SQLite datetime format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

def ok(msg: str):   print(f"  ✓  {msg}")
def skip(msg: str): print(f"  -  {msg}")
def warn(msg: str): print(f"  !  {msg}", file=sys.stderr)
def info(msg: str): print(f"     {msg}")


class DB:
    """Thin SQLite wrapper that respects --dry-run."""

    def __init__(self, path: str, dry_run: bool = False):
        self.path    = path
        self.dry_run = dry_run
        self._con: sqlite3.Connection | None = None

    def connect(self):
        db_path = Path(self.path)
        if not db_path.exists():
            sys.exit(
                f"\nDatabase not found: {self.path}\n"
                f"Is ErsatzTV running (or has it run at least once)?\n"
                f"Check ETV_DB_PATH in your .env file.\n"
            )
        self._con = sqlite3.connect(self.path, timeout=10)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA foreign_keys = ON")
        self._con.execute("PRAGMA journal_mode = WAL")

    def close(self):
        if self._con:
            self._con.close()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def fetchone(self, sql: str, params=()):
        return self._con.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params=()):
        return self._con.execute(sql, params).fetchall()

    def execute(self, sql: str, params=()):
        """Execute a write statement, respecting dry_run."""
        if self.dry_run:
            preview = sql.strip()[:120].replace("\n", " ")
            print(f"  [dry-run] {preview}")
            return None
        cur = self._con.execute(sql, params)
        self._con.commit()
        return cur

    def lastrowid(self, sql: str, params=()) -> int:
        if self.dry_run:
            preview = sql.strip()[:120].replace("\n", " ")
            print(f"  [dry-run] {preview}")
            return -1
        cur = self._con.execute(sql, params)
        self._con.commit()
        return cur.lastrowid


# ─── Setup steps ──────────────────────────────────────────────────────────────

def step_libraries(db: DB):
    print("\n-- Libraries & Paths --")
    local_source = db.fetchone("SELECT Id FROM LocalMediaSource LIMIT 1")
    if not local_source:
        sys.exit("No LocalMediaSource found. Has ErsatzTV initialised its DB?")
    media_source_id = local_source["Id"]
    ok(f"LocalMediaSource Id={media_source_id}")

    for lib_name, media_kind, subpath in LIBRARIES:
        container_path = f"{NAS_CONTAINER_PATH}/{subpath}"
        print(f"\n  Library: '{lib_name}'  ({container_path})")

        # Find or create the Library row
        lib = db.fetchone(
            "SELECT Id FROM Library WHERE Name=? AND MediaKind=? AND MediaSourceId=?",
            (lib_name, media_kind, media_source_id),
        )
        if lib:
            lib_id = lib["Id"]
            skip(f"Library already exists (Id={lib_id})")
        else:
            lib_id = db.lastrowid(
                "INSERT INTO Library (LastScan, MediaKind, MediaSourceId, Name) "
                "VALUES (?,?,?,?)",
                ("0001-01-01 00:00:00", media_kind, media_source_id, lib_name),
            )
            ok(f"Created Library (Id={lib_id})")
            # LocalLibrary is the EF table-per-type child row (same PK as Library)
            db.execute("INSERT INTO LocalLibrary (Id) VALUES (?)", (lib_id,))
            ok(f"Created LocalLibrary (Id={lib_id})")

        # Find or create the LibraryPath
        existing_path = db.fetchone(
            "SELECT Id FROM LibraryPath WHERE Path=? AND LibraryId=?",
            (container_path, lib_id),
        )
        if existing_path:
            skip(f"LibraryPath already exists (Id={existing_path['Id']})")
        else:
            path_id = db.lastrowid(
                "INSERT INTO LibraryPath (Path, LibraryId, LastScan) VALUES (?,?,?)",
                (container_path, lib_id, "0001-01-01 00:00:00"),
            )
            ok(f"Added LibraryPath (Id={path_id})  ->  {container_path}")


def step_collection(db: DB) -> int:
    print("\n-- Collection --")
    existing = db.fetchone(
        "SELECT Id FROM Collection WHERE Name=?", (COLLECTION_NAME,)
    )
    if existing:
        coll_id = existing["Id"]
        skip(f"Collection '{COLLECTION_NAME}' already exists (Id={coll_id})")
        return coll_id

    coll_id = db.lastrowid(
        "INSERT INTO Collection (Name, UseCustomPlaybackOrder) VALUES (?,?)",
        (COLLECTION_NAME, 0),
    )
    ok(f"Created Collection '{COLLECTION_NAME}' (Id={coll_id})")
    return coll_id


def step_channel(db: DB) -> int:
    print("\n-- Channel --")
    existing = db.fetchone(
        "SELECT Id FROM Channel WHERE Number=?", (CHANNEL_NUMBER,)
    )
    if existing:
        ch_id = existing["Id"]
        skip(f"Channel {CHANNEL_NUMBER} already exists (Id={ch_id})")
        return ch_id

    ffmpeg_profile = db.fetchone("SELECT Id FROM FFmpegProfile WHERE Id=1")
    if not ffmpeg_profile:
        sys.exit("FFmpegProfile Id=1 not found. ErsatzTV may not be fully initialised.")

    ch_id = db.lastrowid(
        """
        INSERT INTO Channel (
            Categories, FFmpegProfileId, FallbackFillerId, "Group",
            IdleBehavior, IsEnabled, MirrorSourceChannelId,
            MusicVideoCreditsMode, MusicVideoCreditsTemplate,
            Name, Number, PlayoutMode, PlayoutOffset, PlayoutSource,
            PreferredAudioLanguageCode, PreferredAudioTitle,
            PreferredSubtitleLanguageCode, ShowInEpg, SongVideoMode,
            SortNumber, StreamSelector, StreamSelectorMode,
            StreamingMode, SubtitleMode, TranscodeMode, UniqueId,
            WatermarkId, SlugSeconds, StreamingEngine, NextEngineTextSubtitleMode
        ) VALUES (
            NULL, 1, NULL, ?,
            0, 1, NULL,
            0, NULL,
            ?, ?, 0, NULL, 0,
            NULL, NULL,
            NULL, 1, 0,
            ?, NULL, 0,
            5, 0, 0, ?,
            NULL, NULL, 0, 0
        )
        """,
        (
            CHANNEL_GROUP,
            CHANNEL_NAME,
            CHANNEL_NUMBER,
            float(CHANNEL_NUMBER),
            str(uuid.uuid4()).upper(),
        ),
    )
    ok(f"Created Channel '{CHANNEL_NAME}' number={CHANNEL_NUMBER} (Id={ch_id})")
    return ch_id


def step_schedule(db: DB, coll_id: int) -> int:
    print("\n-- Program Schedule --")
    existing = db.fetchone(
        "SELECT Id FROM ProgramSchedule WHERE Name=?", (SCHEDULE_NAME,)
    )
    if existing:
        sched_id = existing["Id"]
        skip(f"Schedule '{SCHEDULE_NAME}' already exists (Id={sched_id})")
        item = db.fetchone(
            "SELECT Id FROM ProgramScheduleItem "
            "WHERE ProgramScheduleId=? AND CollectionId=?",
            (sched_id, coll_id),
        )
        if not item:
            _insert_flood_item(db, sched_id, coll_id)
        else:
            skip(f"Flood schedule item already exists (Id={item['Id']})")
        return sched_id

    sched_id = db.lastrowid(
        """
        INSERT INTO ProgramSchedule (
            FixedStartTimeBehavior, KeepMultiPartEpisodesTogether,
            Name, RandomStartPoint, ShuffleScheduleItems,
            TreatCollectionsAsShows
        ) VALUES (0, 0, ?, 1, 0, 0)
        """,
        (SCHEDULE_NAME,),
    )
    ok(f"Created ProgramSchedule '{SCHEDULE_NAME}' (Id={sched_id})")
    _insert_flood_item(db, sched_id, coll_id)
    return sched_id


def _insert_flood_item(db: DB, sched_id: int, coll_id: int):
    """Insert a FloodItem schedule entry referencing the given collection."""
    item_id = db.lastrowid(
        """
        INSERT INTO ProgramScheduleItem (
            CollectionId, CollectionType, CustomTitle, FakeCollectionKey,
            FallbackFillerId, FillWithGroupMode, FixedStartTimeBehavior,
            GuideMode, "Index", MarathonBatchSize, MarathonGroupBy,
            MarathonShuffleGroups, MarathonShuffleItems, MediaItemId,
            MidRollFillerId, MultiCollectionId, PlaybackOrder,
            PlaylistId, PostRollFillerId, PreRollFillerId,
            PreferredAudioLanguageCode, PreferredAudioTitle,
            PreferredSubtitleLanguageCode, ProgramScheduleId,
            RerunCollectionId, SmartCollectionId, StartTime,
            SubtitleMode, TailFillerId, SearchQuery, SearchTitle
        ) VALUES (
            ?, 0, NULL, NULL,
            NULL, 0, NULL,
            0, 0, NULL, 0,
            0, 0, NULL,
            NULL, NULL, 1,
            NULL, NULL, NULL,
            NULL, NULL,
            NULL, ?,
            NULL, NULL, NULL,
            NULL, NULL, NULL, NULL
        )
        """,
        (coll_id, sched_id),
    )
    ok(f"Created ProgramScheduleItem (flood, CollectionId={coll_id}, Id={item_id})")
    # ProgramScheduleFloodItem is the EF discriminator child row (same PK)
    db.execute("INSERT INTO ProgramScheduleFloodItem (Id) VALUES (?)", (item_id,))
    ok(f"Created ProgramScheduleFloodItem (Id={item_id})")


def step_playout(db: DB, ch_id: int, sched_id: int):
    print("\n-- Playout --")
    existing = db.fetchone(
        "SELECT Id FROM Playout WHERE ChannelId=? AND ProgramScheduleId=?",
        (ch_id, sched_id),
    )
    if existing:
        skip(f"Playout already exists (Id={existing['Id']})")
        return

    seed = random.randint(1, 2**31 - 1)
    playout_id = db.lastrowid(
        """
        INSERT INTO Playout (
            ChannelId, DailyRebuildTime, DecoId, OnDemandCheckpoint,
            ProgramScheduleId, ScheduleFile, ScheduleKind, Seed
        ) VALUES (?, NULL, NULL, NULL, ?, NULL, 0, ?)
        """,
        (ch_id, sched_id, seed),
    )
    ok(f"Created Playout (Id={playout_id}, Seed={seed})")

    next_start = now_utc_str()
    db.execute(
        """
        INSERT INTO PlayoutAnchor (
            PlayoutId, DurationFinish, InDurationFiller, InFlood,
            MultipleRemaining, NextGuideGroup, NextStart,
            NextInstructionIndex, Context
        ) VALUES (?, NULL, 0, 0, NULL, 0, ?, 0, NULL)
        """,
        (playout_id, next_start),
    )
    ok(f"Created PlayoutAnchor (NextStart={next_start})")


def step_populate_collection(db: DB, coll_id: int):
    print("\n-- Populate Collection --")
    all_items = db.fetchall("SELECT Id FROM MediaItem")
    if not all_items:
        warn("No MediaItems found — has a library scan completed?")
        info("Go to ErsatzTV UI -> Libraries -> Scan, then re-run:")
        info("  python setup.py --populate-collection")
        return

    existing = {
        r["MediaItemId"]
        for r in db.fetchall(
            "SELECT MediaItemId FROM CollectionItem WHERE CollectionId=?",
            (coll_id,),
        )
    }
    to_add = [r["Id"] for r in all_items if r["Id"] not in existing]

    if not to_add:
        skip(f"All {len(all_items)} MediaItems already in collection.")
        return

    for media_id in to_add:
        db.execute(
            "INSERT INTO CollectionItem (CollectionId, MediaItemId, CustomIndex) "
            "VALUES (?,?,NULL)",
            (coll_id, media_id),
        )
    ok(
        f"Added {len(to_add)} MediaItems to Collection (Id={coll_id}). "
        f"{len(existing)} were already present."
    )


def step_status(db: DB):
    print("\n-- Current Database State --\n")

    libs = db.fetchall(
        "SELECT l.Id, l.Name, l.MediaKind, COUNT(lp.Id) AS path_count "
        "FROM Library l LEFT JOIN LibraryPath lp ON lp.LibraryId=l.Id "
        "GROUP BY l.Id ORDER BY l.Id"
    )
    print(f"  Libraries ({len(libs)}):")
    for lib in libs:
        mk = {1: "Movie", 2: "Show", 3: "MusicVideo", 4: "OtherVideo"}.get(
            lib["MediaKind"], str(lib["MediaKind"])
        )
        print(f"    [{lib['Id']:2d}] {lib['Name']:<25s}  type={mk}  paths={lib['path_count']}")

    paths = db.fetchall(
        "SELECT lp.Id, lp.Path, lp.LibraryId FROM LibraryPath lp ORDER BY lp.LibraryId"
    )
    print(f"\n  LibraryPaths ({len(paths)}):")
    for p in paths:
        print(f"    [{p['Id']:2d}] lib={p['LibraryId']}  {p['Path']}")

    channels = db.fetchall(
        'SELECT Id, Number, Name, "Group" FROM Channel ORDER BY Id'
    )
    print(f"\n  Channels ({len(channels)}):")
    for ch in channels:
        print(f"    [{ch['Id']:2d}] #{ch['Number']}  {ch['Name']}  (group: {ch['Group']})")

    colls = db.fetchall(
        "SELECT c.Id, c.Name, COUNT(ci.MediaItemId) AS item_count "
        "FROM Collection c LEFT JOIN CollectionItem ci ON ci.CollectionId=c.Id "
        "GROUP BY c.Id"
    )
    print(f"\n  Collections ({len(colls)}):")
    for c in colls:
        print(f"    [{c['Id']:2d}] {c['Name']}  ({c['item_count']} items)")

    scheds = db.fetchall("SELECT Id, Name FROM ProgramSchedule ORDER BY Id")
    print(f"\n  Schedules ({len(scheds)}):")
    for s in scheds:
        print(f"    [{s['Id']:2d}] {s['Name']}")

    playouts = db.fetchall(
        "SELECT p.Id, p.ChannelId, p.ProgramScheduleId, ch.Name AS ch_name "
        "FROM Playout p LEFT JOIN Channel ch ON ch.Id=p.ChannelId ORDER BY p.Id"
    )
    print(f"\n  Playouts ({len(playouts)}):")
    for p in playouts:
        print(f"    [{p['Id']:2d}] channel={p['ch_name']}  schedule={p['ProgramScheduleId']}")

    media_count = db.fetchone("SELECT COUNT(*) AS n FROM MediaItem")
    print(f"\n  MediaItems scanned: {media_count['n']}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ErsatzTV SQLite setup automation")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be done without writing to the DB",
    )
    parser.add_argument(
        "--populate-collection", action="store_true",
        help="Add all scanned MediaItems to the collection (run after a library scan)",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show current DB state and exit",
    )
    parser.add_argument(
        "--db", default=ETV_DB_PATH, metavar="PATH",
        help=f"Path to ersatztv.sqlite3 (default: {ETV_DB_PATH})",
    )
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    print(f"  ErsatzTV SQLite Setup")
    print(f"  DB:  {args.db}")
    print(f"  NAS: {NAS_CONTAINER_PATH}")
    if args.dry_run:
        print("  MODE: DRY RUN -- no writes will be committed")
    print(f"{'=' * 60}")

    with DB(args.db, dry_run=args.dry_run) as db:

        if args.status:
            step_status(db)
            return

        # Phase 1: configure library paths
        step_libraries(db)

        # Phase 2: collection
        coll_id = step_collection(db)

        # Phase 3: channel
        ch_id = step_channel(db)

        # Phase 4: schedule + flood item
        sched_id = step_schedule(db, coll_id)

        # Phase 5: playout + anchor
        step_playout(db, ch_id, sched_id)

        # Phase 6 (optional): populate collection after scan
        if args.populate_collection:
            step_populate_collection(db, coll_id)

    # Final output
    print(f"\n{'=' * 60}")
    if args.dry_run:
        print("  Dry run complete -- re-run without --dry-run to apply.")
    else:
        print("  Setup complete!\n")
        print("  Next steps:")
        print("  1. Restart ErsatzTV to pick up new library paths:")
        print("       docker compose restart ersatztv")
        print("  2. In ErsatzTV UI -> Libraries, click Scan for each library.")
        print("  3. Once scanning finishes, populate the collection:")
        print("       python setup.py --populate-collection")
        print("  4. Add to Jellyfin Live TV:")
        print(f"       M3U  : {ETV_HOST}/iptv/xmltv.m3u")
        print(f"       XMLTV: {ETV_HOST}/iptv/xmltv.xml")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
