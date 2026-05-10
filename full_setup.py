#!/usr/bin/env python3
"""
full_setup.py — One-shot ErsatzTV + Jellyfin complete automated setup.

What this does (end to end, no manual steps):
  1. Downloads ErsatzTV if not already installed.
  2. Reads every MediaItem from the ErsatzTV database and groups it
     by TV show title or movie title using metadata tables.
  3. Creates one channel, collection, schedule, and playout per group.
  4. Restarts ErsatzTV so it rebuilds all playouts immediately.
  5. Adds the ErsatzTV M3U tuner and XMLTV guide to Jellyfin.

Usage:
    python full_setup.py                          # full run
    python full_setup.py --discover               # print media groups, no changes
    python full_setup.py --dry-run                # print what would happen, no changes
    python full_setup.py --no-restart             # skip ErsatzTV restart after setup
    python full_setup.py --jellyfin-user U \\
                         --jellyfin-password P    # configure Jellyfin too
"""

import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

ETV_DB_PATH   = os.environ.get("ETV_DB_PATH",
    r"C:\Users\rbaez\AppData\Local\ersatztv\ersatztv.sqlite3")
ETV_HOST      = os.environ.get("ETV_HOST", "http://localhost:8409")
NAS_BASE      = os.environ.get("NAS_CONTAINER_PATH", "Z:/")
ETV_EXE_PATH  = os.environ.get("ETV_EXE_PATH",
    r"C:\Users\rbaez\ersatztv\ErsatzTV.exe")

ERSATZTV_GITHUB_REPO  = "ErsatzTV/legacy"

JELLYFIN_HOST         = "http://localhost:8096"
ERSATZTV_LAN          = "http://192.168.68.61:8409"
M3U_URL               = f"{ERSATZTV_LAN}/iptv/channels.m3u"
XMLTV_URL             = f"{ERSATZTV_LAN}/iptv/xmltv.xml"

FFMPEG_PROFILE_ID = 1          # "1920x1080 x264 aac" — first profile in DB
SCHEDULE_KIND     = 0          # Classic (ProgramSchedule-based)
COLLECTION_TYPE   = 0          # Collection
FILL_GROUP_MODE   = 0          # None
GUIDE_MODE        = 0          # Normal
MARATHON_GROUP_BY = 0

# PlaybackOrder: 0 = InOrder (chronological), 1 = Shuffle
ORDER_IN_ORDER = 0
ORDER_SHUFFLE  = 1

# Channel definitions ─────────────────────────────────────────────────────────
# patterns: list of lowercase substrings matched against the show/movie title.
# Empty patterns = catch-all (used for the "All My Media" channel).
# order: 0=InOrder (great for series), 1=Shuffle (great for movies/variety)
CHANNEL_DEFS = [
    {"number": 1,  "name": "All My Media",    "patterns": [],
     "order": ORDER_SHUFFLE},
    {"number": 2,  "name": "I Love Lucy",     "patterns": ["i love lucy"],
     "order": ORDER_IN_ORDER},
    {"number": 4,  "name": "Lord of the Ring","patterns": ["lord of the ring"],
     "order": ORDER_IN_ORDER},
    {"number": 5,  "name": "Friday",          "patterns": ["friday"],
     "order": ORDER_SHUFFLE},
    {"number": 6,  "name": "90s Toon",
     "patterns": ["rugrats","hey arnold","doug","animaniacs","tiny toon",
                  "dexter","johnny bravo","cow and chicken","powerpuff",
                  "rocko","recess","pepper ann","batman animated",
                  "spider-man","x-men","gargoyles","ahhh real monsters",
                  "aaahh"],
     "order": ORDER_SHUFFLE},
    {"number": 7,  "name": "Boondocks",       "patterns": ["boondocks"],
     "order": ORDER_IN_ORDER},
    {"number": 8,  "name": "Fitness",         "patterns": ["fitness"],
     "order": ORDER_SHUFFLE},
    {"number": 9,  "name": "Stand Up",        "patterns": ["stand up","standup"],
     "order": ORDER_SHUFFLE},
]

DRY_RUN = False  # overridden by --dry-run flag


# ─── ErsatzTV install / lifecycle ─────────────────────────────────────────────

def ensure_ersatztv() -> str:
    """
    Verify ErsatzTV.exe is present. If missing, download the latest Windows
    release from GitHub and extract it to the exe's parent directory.
    Returns the resolved exe path (may differ from ETV_EXE_PATH if zip layout
    places the exe in a subdirectory).
    """
    exe = Path(ETV_EXE_PATH)
    if exe.exists():
        print(f"✓ ErsatzTV found: {exe}")
        return str(exe)

    print(f"  ErsatzTV.exe not found at {exe}")
    print(f"  Fetching latest release info from GitHub ...")

    api_url = (f"https://api.github.com/repos/{ERSATZTV_GITHUB_REPO}"
               f"/releases/latest")
    try:
        r = requests.get(api_url, timeout=30,
                         headers={"Accept": "application/vnd.github+json"})
    except Exception as e:
        print(f"  ✗ GitHub request failed: {e}")
        return ""

    if not r.ok:
        print(f"  ✗ GitHub API error {r.status_code}: "
              f"{r.json().get('message', r.text[:120])}")
        return ""

    release = r.json()
    tag    = release.get("tag_name", "unknown")
    assets = release.get("assets", [])

    # Prefer win-x64 zip; fall back to any zip
    win_asset = next(
        (a for a in assets
         if a["name"].endswith(".zip") and "win" in a["name"].lower()),
        None,
    )
    if not win_asset:
        win_asset = next(
            (a for a in assets if a["name"].endswith(".zip")), None)

    if not win_asset:
        names = [a["name"] for a in assets]
        print(f"  ✗ No Windows zip asset found in release {tag}.")
        print(f"  Available assets: {names}")
        print(f"  Download manually from: "
              f"https://github.com/{ERSATZTV_GITHUB_REPO}/releases")
        return ""

    dl_url   = win_asset["browser_download_url"]
    size_mb  = win_asset["size"] / 1_000_000
    print(f"  Release : {tag}")
    print(f"  Asset   : {win_asset['name']} ({size_mb:.1f} MB)")
    print(f"  URL     : {dl_url}")
    print(f"  Downloading ...", end="", flush=True)

    tmp_path = Path(tempfile.mktemp(suffix=".zip"))
    try:
        with requests.get(dl_url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            downloaded = 0
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65_536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded % (10 * 1024 * 1024) < 65_536:
                        print(".", end="", flush=True)
        print(" done")
    except Exception as e:
        print(f"\n  ✗ Download failed: {e}")
        tmp_path.unlink(missing_ok=True)
        return ""

    dest = exe.parent
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  Extracting to {dest} ...")
    try:
        with zipfile.ZipFile(tmp_path) as zf:
            zf.extractall(dest)
    except Exception as e:
        print(f"  ✗ Extraction failed: {e}")
        tmp_path.unlink(missing_ok=True)
        return ""
    finally:
        tmp_path.unlink(missing_ok=True)

    if exe.exists():
        print(f"  ✓ ErsatzTV installed: {exe}")
        return str(exe)

    # Zip may have placed exe in a subdirectory
    found = list(dest.rglob("ErsatzTV.exe"))
    if found:
        print(f"  ✓ ErsatzTV installed: {found[0]}")
        return str(found[0])

    print(f"  ✗ ErsatzTV.exe not found after extraction — check {dest}")
    return ""


def etv_is_running() -> bool:
    """Return True if an ErsatzTV.exe process is currently running."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ErsatzTV.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        return "ErsatzTV.exe" in result.stdout
    except Exception:
        return False


def restart_ersatztv(exe_path: str = "") -> bool:
    """
    Kill the running ErsatzTV process, relaunch it, and poll until the
    API is responding. Returns True if ErsatzTV comes back online.

    ErsatzTV rebuilds all playouts on startup when the DB has changed,
    so a restart is the most reliable way to trigger playout population.
    """
    exe = exe_path or ETV_EXE_PATH

    print("\n── Restarting ErsatzTV to trigger playout builds ──")

    # ── Kill ──────────────────────────────────────────────────────────────────
    if etv_is_running():
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", "ErsatzTV.exe"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                print("  ✓ ErsatzTV stopped")
            else:
                print(f"  ⚠  taskkill exit {result.returncode}: "
                      f"{result.stderr.strip()}")
        except Exception as e:
            print(f"  ⚠  Could not stop ErsatzTV: {e}")
        time.sleep(3)
    else:
        print("  (ErsatzTV was not running — starting fresh)")

    # ── Launch ────────────────────────────────────────────────────────────────
    exe_path_resolved = Path(exe)
    if not exe_path_resolved.exists():
        print(f"  ✗ ErsatzTV.exe not found at {exe_path_resolved}")
        return False

    try:
        subprocess.Popen(
            [str(exe_path_resolved)],
            creationflags=(subprocess.DETACHED_PROCESS |
                           subprocess.CREATE_NEW_PROCESS_GROUP),
        )
        print(f"  ✓ ErsatzTV launched: {exe_path_resolved}")
    except Exception as e:
        print(f"  ✗ Could not launch ErsatzTV: {e}")
        return False

    # ── Poll until API responds ───────────────────────────────────────────────
    print("  Waiting for ErsatzTV API", end="", flush=True)
    for _ in range(45):
        time.sleep(2)
        try:
            resp = requests.get(f"{ETV_HOST}/api/v1/channels", timeout=3)
            if resp.ok:
                print(" ✓")
                print("  ErsatzTV is online — playouts are being built.")
                return True
        except Exception:
            pass
        print(".", end="", flush=True)

    print(" timed out")
    print("  ⚠  ErsatzTV did not respond in 90 s — check it manually.")
    return False


# ─── DB helpers ───────────────────────────────────────────────────────────────

def open_db(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        print(f"✗ Database not found: {path}")
        sys.exit(1)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def qone(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def qall(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def get_channel_cols(conn) -> set:
    """Return the set of column names currently in the Channel table."""
    rows = conn.execute("PRAGMA table_info(Channel)").fetchall()
    # Each row: (cid, name, type, notnull, dflt_value, pk)
    return {row[1] for row in rows}


# ─── Media discovery ──────────────────────────────────────────────────────────

def discover_media(conn) -> dict[str, list[int]]:
    """
    Returns { group_key: [media_item_id, ...] }
    Group keys are lowercase, matched against CHANNEL_DEFS patterns.
    """
    groups: dict[str, list[int]] = {}

    def add(group: str, mid: int):
        groups.setdefault(group, []).append(mid)

    # TV episodes → group by show title
    rows = qall(conn, """
        SELECT DISTINCT mi.Id, sm.Title
        FROM   MediaItem mi
        JOIN   Episode       e   ON e.Id   = mi.Id
        JOIN   Season        sea ON sea.Id = e.SeasonId
        JOIN   Show          s   ON s.Id   = sea.ShowId
        JOIN   ShowMetadata  sm  ON sm.ShowId = s.Id
        ORDER  BY sm.Title
    """)
    for r in rows:
        add(r["Title"].lower().strip(), r["Id"])

    # Movies → group by movie title
    rows = qall(conn, """
        SELECT DISTINCT mi.Id, mm.Title
        FROM   MediaItem    mi
        JOIN   Movie        m   ON m.Id      = mi.Id
        JOIN   MovieMetadata mm ON mm.MovieId = m.Id
        ORDER  BY mm.Title
    """)
    for r in rows:
        add(r["Title"].lower().strip(), r["Id"])

    # OtherVideos → group by library path category
    rows = qall(conn, """
        SELECT DISTINCT mi.Id, lp.Path, ovm.Title
        FROM   MediaItem        mi
        JOIN   OtherVideo       ov  ON ov.Id            = mi.Id
        LEFT JOIN OtherVideoMetadata ovm ON ovm.OtherVideoId = mi.Id
        JOIN   LibraryPath      lp  ON lp.Id            = mi.LibraryPathId
    """)
    for r in rows:
        raw_path = (r["Path"] or "").replace("\\", "/").lower()
        title    = (r["Title"] or "untitled").lower().strip()
        if "fitness" in raw_path:
            add("fitness", r["Id"])
        elif "stand up" in raw_path or "standup" in raw_path or "comedy" in raw_path:
            add("stand up", r["Id"])
        else:
            add(title, r["Id"])

    return groups


def assign_channels(groups: dict[str, list[int]]) -> list[dict]:
    """
    Match discovered media groups to CHANNEL_DEFS.
    Returns a list of channel tasks ready for setup.
    """
    tasks = []
    all_ids: list[int] = []
    for ids in groups.values():
        all_ids.extend(ids)
    # Deduplicate while preserving order
    seen: set = set()
    unique_all = [i for i in all_ids if not (i in seen or seen.add(i))]

    for ch_def in CHANNEL_DEFS:
        patterns = ch_def["patterns"]
        if not patterns:
            # catch-all channel gets every id
            tasks.append({**ch_def, "media_ids": unique_all})
            continue

        matched: list[int] = []
        for group_key, ids in groups.items():
            if any(p in group_key for p in patterns):
                matched.extend(ids)
        # Deduplicate
        seen2: set = set()
        matched = [i for i in matched if not (i in seen2 or seen2.add(i))]

        if matched:
            tasks.append({**ch_def, "media_ids": matched})
        else:
            print(f"  ⚠  No media found for channel {ch_def['number']} "
                  f"'{ch_def['name']}' — skipping")

    return tasks


# ─── ErsatzTV DB operations ───────────────────────────────────────────────────

def upsert_channel(conn, number: int, name: str) -> int:
    existing = qone(conn,
        "SELECT Id FROM Channel WHERE Number = ?", (str(number),))
    if existing:
        cid = existing["Id"]
        if not DRY_RUN:
            conn.execute("UPDATE Channel SET Name = ? WHERE Id = ?",
                         (name, cid))
        print(f"  ↻  Channel #{number} '{name}' (id={cid}) — reused")
        return cid

    if DRY_RUN:
        print(f"  +  Would create channel #{number} '{name}'")
        return -1

    uid    = str(uuid.uuid4())
    avail  = get_channel_cols(conn)

    # Base columns — present in every known schema version.
    # "Group" is a SQL reserved word and must be double-quoted.
    col_names = [
        "Number", "Name", "FFmpegProfileId", '"Group"',
        "IsEnabled", "ShowInEpg",
        "IdleBehavior", "PlayoutMode", "PlayoutSource", "StreamingMode",
        "TranscodeMode", "SubtitleMode", "SongVideoMode", "MusicVideoCreditsMode",
        "StreamSelectorMode", "SortNumber", "UniqueId",
    ]
    col_vals = [
        str(number), name, FFMPEG_PROFILE_ID, "IPTV",
        1, 1,
        0, 0, 0, 0,
        1, 0, 0, 0,
        0, number, uid,
    ]

    # Optional columns — only add if they exist in this DB schema
    for col, val in [("StreamingEngine", 0),
                     ("NextEngineTextSubtitleMode", 0)]:
        if col in avail:
            col_names.append(col)
            col_vals.append(val)

    sql = (f"INSERT INTO Channel ({', '.join(col_names)}) "
           f"VALUES ({', '.join(['?'] * len(col_vals))})")
    cur = conn.execute(sql, col_vals)
    cid = cur.lastrowid
    print(f"  +  Channel #{number} '{name}' created (id={cid})")
    return cid


def upsert_collection(conn, name: str, media_ids: list[int]) -> int:
    existing = qone(conn,
        "SELECT Id FROM Collection WHERE Name = ?", (name,))
    if existing:
        coll_id = existing["Id"]
        print(f"  ↻  Collection '{name}' (id={coll_id}) — reused, "
              f"syncing {len(media_ids)} items")
    else:
        if DRY_RUN:
            print(f"  +  Would create collection '{name}' "
                  f"with {len(media_ids)} items")
            return -1
        cur = conn.execute(
            "INSERT INTO Collection (Name, UseCustomPlaybackOrder) VALUES (?,?)",
            (name, 0))
        coll_id = cur.lastrowid
        print(f"  +  Collection '{name}' created (id={coll_id}) "
              f"with {len(media_ids)} items")

    if DRY_RUN:
        return coll_id

    # Sync items — clear and re-add
    conn.execute("DELETE FROM CollectionItem WHERE CollectionId = ?",
                 (coll_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO CollectionItem "
        "(CollectionId, MediaItemId, CustomIndex) VALUES (?,?,?)",
        [(coll_id, mid, 0) for mid in media_ids])
    return coll_id


def upsert_schedule(conn, name: str, coll_id: int,
                    playback_order: int) -> int:
    existing = qone(conn,
        "SELECT Id FROM ProgramSchedule WHERE Name = ?", (name,))
    if existing:
        sched_id = existing["Id"]
        print(f"  ↻  Schedule '{name}' (id={sched_id}) — reused")
    else:
        if DRY_RUN:
            print(f"  +  Would create schedule '{name}'")
            return -1
        cur = conn.execute("""
            INSERT INTO ProgramSchedule
            (Name, KeepMultiPartEpisodesTogether, RandomStartPoint,
             ShuffleScheduleItems, TreatCollectionsAsShows,
             FixedStartTimeBehavior)
            VALUES (?,?,?,?,?,?)
        """, (name, 0, 0, 0, 0, 0))
        sched_id = cur.lastrowid
        print(f"  +  Schedule '{name}' created (id={sched_id})")

    if DRY_RUN:
        return sched_id

    # Ensure one flood item exists for this schedule
    existing_item = qone(conn,
        "SELECT Id FROM ProgramScheduleItem "
        "WHERE ProgramScheduleId = ? LIMIT 1",
        (sched_id,))
    if not existing_item:
        cur2 = conn.execute("""
            INSERT INTO ProgramScheduleItem
            (ProgramScheduleId, CollectionId, CollectionType, PlaybackOrder,
             "Index", FillWithGroupMode, GuideMode, MarathonGroupBy,
             MarathonShuffleGroups, MarathonShuffleItems, MarathonBatchSize)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (sched_id, coll_id, COLLECTION_TYPE, playback_order,
              0, FILL_GROUP_MODE, GUIDE_MODE, MARATHON_GROUP_BY,
              0, 0, None))
        item_id = cur2.lastrowid
        conn.execute(
            "INSERT INTO ProgramScheduleFloodItem (Id) VALUES (?)",
            (item_id,))
        print(f"    + Flood item added (item_id={item_id})")
    else:
        # Update existing item with current collection and order
        conn.execute("""
            UPDATE ProgramScheduleItem
            SET CollectionId = ?, PlaybackOrder = ?
            WHERE Id = ?
        """, (coll_id, playback_order, existing_item["Id"]))
    return sched_id


def upsert_playout(conn, channel_id: int, sched_id: int) -> int:
    existing = qone(conn,
        "SELECT Id FROM Playout WHERE ChannelId = ?", (channel_id,))
    if existing:
        playout_id = existing["Id"]
        if not DRY_RUN:
            conn.execute(
                "UPDATE Playout SET ProgramScheduleId = ? WHERE Id = ?",
                (sched_id, playout_id))
        print(f"  ↻  Playout (id={playout_id}) — reused / updated")
        return playout_id

    if DRY_RUN:
        print(f"  +  Would create playout for channel {channel_id}")
        return -1

    seed = random.randint(0, 2**31 - 1)
    cur = conn.execute("""
        INSERT INTO Playout
        (ChannelId, ProgramScheduleId, ScheduleKind, Seed)
        VALUES (?,?,?,?)
    """, (channel_id, sched_id, SCHEDULE_KIND, seed))
    playout_id = cur.lastrowid

    # Ensure PlayoutAnchor row
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    existing_anchor = qone(conn,
        "SELECT PlayoutId FROM PlayoutAnchor WHERE PlayoutId = ?",
        (playout_id,))
    if not existing_anchor:
        conn.execute("""
            INSERT INTO PlayoutAnchor
            (PlayoutId, NextStart, InFlood, InDurationFiller, NextGuideGroup,
             NextInstructionIndex, MultipleRemaining)
            VALUES (?,?,?,?,?,?,?)
        """, (playout_id, now_iso, 1, 0, 0, 0, None))

    print(f"  +  Playout created (id={playout_id}) seed={seed}")
    return playout_id


# ─── Jellyfin helpers ─────────────────────────────────────────────────────────

def jf_auth_header(token: str = "") -> dict:
    parts = [
        'MediaBrowser Client="SetupScript"',
        'Device="AutoSetup"',
        'DeviceId="ersatztv-full-setup"',
        'Version="1.0"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return {"X-Emby-Authorization": ", ".join(parts)}


def jf_authenticate(username: str, password: str) -> str:
    r = requests.post(
        f"{JELLYFIN_HOST}/Users/AuthenticateByName",
        json={"Username": username, "Pw": password},
        headers={**jf_auth_header(), "Content-Type": "application/json"},
        timeout=10,
    )
    if not r.ok:
        print(f"  ✗ Jellyfin auth failed: {r.status_code}")
        return ""
    token = r.json()["AccessToken"]
    print(f"  ✓ Authenticated to Jellyfin")
    return token


def jf_call(method: str, path: str, token: str, **kwargs) -> dict | None:
    headers = {**jf_auth_header(token), "Content-Type": "application/json"}
    r = requests.request(method, f"{JELLYFIN_HOST}{path}",
                         headers=headers, timeout=15, **kwargs)
    if r.ok:
        return r.json() if r.content else {}
    return None


def configure_jellyfin(username: str, password: str) -> None:
    print("\n── Jellyfin setup ──")
    token = jf_authenticate(username, password)
    if not token:
        return

    # M3U tuner
    info = jf_call("GET", "/LiveTv/Info", token) or {}
    tuner_exists = any(
        ERSATZTV_LAN in t.get("Url", "")
        for t in info.get("TunerHosts", [])
    )
    if tuner_exists:
        print("  ✓ M3U tuner already present")
    elif not DRY_RUN:
        r = jf_call("POST", "/LiveTv/TunerHosts", token, json={
            "Type": "m3u",
            "Url": M3U_URL,
            "TunerCount": 1,
            "AllowHWTranscoding": False,
        })
        if r is not None:
            print(f"  ✓ M3U tuner added: {M3U_URL}")
        else:
            print(f"  ✗ M3U tuner add failed")

    # XMLTV guide
    guide_exists = any(
        ERSATZTV_LAN in p.get("Url", "")
        for p in info.get("ListingProviders", [])
    )
    if guide_exists:
        print("  ✓ XMLTV guide already present")
    elif not DRY_RUN:
        r = requests.post(
            f"{JELLYFIN_HOST}/LiveTv/ListingProviders",
            json={"Type": "xmltv", "Url": XMLTV_URL},
            headers={**jf_auth_header(token), "Content-Type": "application/json"},
            timeout=20,
        )
        if r.status_code in (200, 201, 204):
            print(f"  XMLTV guide added: {XMLTV_URL}")
        elif r.status_code == 500:
            # Known Jellyfin 10.10.x issue: 500 but guide IS saved
            print(f"  Jellyfin returned 500 on XMLTV save (known 10.10 quirk).")
            print(f"     Check Dashboard -> Live TV -> Guide Data Providers to verify.")
        else:
            print(f"  XMLTV guide failed: {r.status_code}")


# --- Main ---

def main():
    global DRY_RUN

    parser = argparse.ArgumentParser(description="Full ErsatzTV + Jellyfin setup")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Print what would happen; make no changes")
    parser.add_argument("--discover",     action="store_true",
                        help="Show discovered media groups and exit")
    parser.add_argument("--no-restart",   action="store_true",
                        help="Skip ErsatzTV restart after DB setup")
    parser.add_argument("--jellyfin-user",     default="",
                        help="Jellyfin admin username")
    parser.add_argument("--jellyfin-password", default="",
                        help="Jellyfin admin password")
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    print(f"\nErsatzTV full setup")
    print(f"  DB   : {ETV_DB_PATH}")
    print(f"  Host : {ETV_HOST}\n")

    # Ensure ErsatzTV is installed
    print("-- Step 0: Verify ErsatzTV installation --")
    resolved_exe = ensure_ersatztv()

    conn = open_db(ETV_DB_PATH)

    # Discover
    print("\n-- Step 1: Discover media --")
    groups = discover_media(conn)
    total = sum(len(v) for v in groups.values())
    print(f"  {len(groups)} unique shows/movies  ({total} total MediaItems)")

    if args.discover:
        print()
        for g, ids in sorted(groups.items(), key=lambda x: -len(x[1])):
            print(f"  {len(ids):4d}  {g}")
        sys.exit(0)

    tasks = assign_channels(groups)

    # Full DB setup
    print("\n-- Step 2: Configure channels --")
    ok_count   = 0
    fail_count = 0

    for task in tasks:
        number    = task["number"]
        name      = task["name"]
        media_ids = task["media_ids"]
        order     = task["order"]

        print(f"\n-- Channel #{number}: {name}  ({len(media_ids)} items) --")

        if not DRY_RUN:
            conn.execute("BEGIN")
        try:
            ch_id      = upsert_channel(conn, number, name)
            coll_id    = upsert_collection(conn, f"{name} Collection", media_ids)
            sched_id   = upsert_schedule(conn,
                                         f"{name} Schedule", coll_id, order)
            playout_id = upsert_playout(conn, ch_id, sched_id)

            if not DRY_RUN:
                conn.execute("COMMIT")
            ok_count += 1
        except Exception as exc:
            if not DRY_RUN:
                conn.execute("ROLLBACK")
            print(f"  Error setting up channel '{name}': {exc}")
            fail_count += 1

    conn.close()

    # Restart ErsatzTV to build playouts
    if not DRY_RUN and not args.no_restart and ok_count > 0:
        print(f"\n-- Step 3: Restart ErsatzTV (triggers playout builds) --")
        restart_ersatztv(resolved_exe)
    elif args.no_restart:
        print("\n  (--no-restart: skipping ErsatzTV restart)")
        print("  Go to ErsatzTV -> Playouts and click 'Build' for each playout.")

    # Jellyfin
    if args.jellyfin_user and not DRY_RUN:
        configure_jellyfin(args.jellyfin_user, args.jellyfin_password)

    # Summary
    print(f"\n" + "-"*55)
    if DRY_RUN:
        print("  Dry run complete -- no changes made.")
    else:
        print(f"  Setup complete.")
        print(f"  Channels OK     : {ok_count}")
        if fail_count:
            print(f"  Channels failed : {fail_count}")
        print()
        print(f"  M3U  : {M3U_URL}")
        print(f"  XMLTV: {XMLTV_URL}")
        print()
        print("  Jellyfin -> Dashboard -> Live TV -> Tuner Devices")
        print(f"    Add M3U Tuner: {M3U_URL}")
    print("-"*55 + "\n")


if __name__ == "__main__":
    main()
