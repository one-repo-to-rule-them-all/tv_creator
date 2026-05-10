#!/usr/bin/env python3
"""
ErsatzTV First-Run Setup
────────────────────────
Automates the full ErsatzTV onboarding flow:
  1. Wait for ErsatzTV to be ready
  2. Discover the live API from Swagger (adapts to any ErsatzTV version)
  3. Add local library paths (Movies, Shows, etc.)
  4. Trigger a media scan and wait for completion
  5. Create a collection containing all scanned media
  6. Create a channel (MPEG-TS mode — Jellyfin-compatible)
  7. Create a classic schedule wired to the collection
  8. Create a playout linking the channel to the schedule
  9. Print M3U + XMLTV URLs ready to paste into Jellyfin

Usage:
  pip install requests python-dotenv
  cp .env.example .env      # edit .env with your actual paths
  python setup.py

If a step fails, run with --dump-api to print all discovered endpoints.
"""

import os
import sys
import time
import json
import logging
import argparse
import re
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("etv-setup")

# ─── Config ───────────────────────────────────────────────────────────────────

load_dotenv()

ETV_HOST         = os.getenv("ETV_HOST", "http://localhost:8409").rstrip("/")
MEDIA_PATH       = os.getenv("MEDIA_PATH", "")
MOVIES_SUBPATH   = os.getenv("MOVIES_SUBPATH", "Movies")
SHOWS_SUBPATH    = os.getenv("SHOWS_SUBPATH", "Shows")
CHANNEL_NUMBER   = int(os.getenv("CHANNEL_NUMBER", "1"))
CHANNEL_NAME     = os.getenv("CHANNEL_NAME", "My Media")
CHANNEL_GROUP    = os.getenv("CHANNEL_GROUP", "IPTV")
SCHEDULE_NAME    = os.getenv("SCHEDULE_NAME", "My Media Schedule")
COLLECTION_NAME  = os.getenv("COLLECTION_NAME", "All My Media")

# ─── HTTP helpers ─────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json", "Accept": "application/json"})


def _json_or_raise(r: requests.Response, context: str = "") -> dict | list:
    """Parse JSON response, raising a clear error if the body is HTML/empty."""
    r.raise_for_status()
    if not r.content:
        return {}
    ct = r.headers.get("content-type", "")
    if "html" in ct:
        raise RuntimeError(
            f"ErsatzTV returned HTML instead of JSON for: {r.url}\n"
            f"  This means the API path is wrong for your version.\n"
            f"  Run with --dump-api to see all discovered endpoints.\n"
            f"  Swagger UI: {ETV_HOST}/swagger"
        )
    try:
        return r.json()
    except Exception:
        raise RuntimeError(
            f"Non-JSON response ({ct}) from {r.url}:\n{r.text[:300]}"
        )


def _get(path: str, **kwargs) -> dict | list:
    r = SESSION.get(f"{ETV_HOST}{path}", **kwargs)
    return _json_or_raise(r, path)


def _post(path: str, body: dict = None, **kwargs) -> dict | list:
    r = SESSION.post(f"{ETV_HOST}{path}", json=body or {}, **kwargs)
    return _json_or_raise(r, path)


def _put(path: str, body: dict = None, **kwargs) -> dict | list:
    r = SESSION.put(f"{ETV_HOST}{path}", json=body or {}, **kwargs)
    return _json_or_raise(r, path)

# ─── Swagger discovery ────────────────────────────────────────────────────────

# Populated by discover_api() at startup
_SWAGGER_PATHS: dict = {}
_API_PREFIX: str = "/api/v1"


def discover_api() -> None:
    """
    Fetch the live Swagger/OpenAPI spec and build a path map.
    Tries common Swagger JSON locations.
    """
    global _SWAGGER_PATHS, _API_PREFIX

    candidates = [
        "/swagger/v1/swagger.json",
        "/api/swagger.json",
        "/swagger.json",
        "/api/v1/swagger.json",
    ]

    spec = None
    for candidate in candidates:
        try:
            r = SESSION.get(f"{ETV_HOST}{candidate}", timeout=5)
            if r.ok and "application/json" in r.headers.get("content-type", ""):
                spec = r.json()
                log.info(f"Swagger spec loaded from {candidate}")
                break
        except Exception:
            continue

    if spec is None:
        log.warning(
            "Could not load Swagger spec — using hardcoded paths. "
            f"If calls fail, check {ETV_HOST}/swagger manually."
        )
        return

    _SWAGGER_PATHS = spec.get("paths", {})

    # Detect the API prefix from the paths (e.g. /api/v1 or /api/v2)
    prefixes = set()
    for p in _SWAGGER_PATHS:
        m = re.match(r"^(/api/v\d+)", p)
        if m:
            prefixes.add(m.group(1))
    if prefixes:
        _API_PREFIX = sorted(prefixes)[0]
        log.info(f"Detected API prefix: {_API_PREFIX}")


def api(path: str) -> str:
    """Resolve a logical path to the full versioned API path."""
    return f"{_API_PREFIX}{path}"


def find_path(keyword: str, method: str = "get") -> Optional[str]:
    """
    Search the Swagger path map for a path containing 'keyword'.
    Returns the first matching path or None.
    """
    keyword = keyword.lower()
    for path, ops in _SWAGGER_PATHS.items():
        if keyword in path.lower() and method.lower() in ops:
            return path
    return None


def dump_api() -> None:
    """Print all discovered endpoints — useful for debugging."""
    if not _SWAGGER_PATHS:
        print("No Swagger paths discovered. Check that ErsatzTV is running.")
        return
    print(f"\n{'─'*60}")
    print(f"  Discovered {len(_SWAGGER_PATHS)} endpoints (prefix: {_API_PREFIX})")
    print(f"{'─'*60}")
    for path in sorted(_SWAGGER_PATHS):
        methods = [m.upper() for m in _SWAGGER_PATHS[path] if m != "parameters"]
        print(f"  {','.join(methods):20s}  {path}")
    print()

# ─── Step 1: Health check ─────────────────────────────────────────────────────

def wait_for_ready(max_wait: int = 120) -> None:
    """Poll /health until ErsatzTV responds."""
    log.info(f"Waiting for ErsatzTV at {ETV_HOST} ...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = SESSION.get(f"{ETV_HOST}/health", timeout=5)
            if r.ok:
                log.info("ErsatzTV is ready ✓")
                return
        except requests.ConnectionError:
            pass
        time.sleep(3)
    raise TimeoutError(
        f"ErsatzTV did not respond in {max_wait}s. "
        "Check: docker compose ps"
    )

# ─── Step 2: Local media source + libraries ───────────────────────────────────

def get_local_media_source() -> dict:
    """
    Return the default local media source. Tries multiple known path patterns
    across ErsatzTV versions and picks the first that returns valid JSON.
    """
    candidates = [
        api("/mediaSources/local"),
        api("/MediaSources/Local"),
        api("/localMediaSources"),
        api("/LocalMediaSources"),
        api("/mediaSources"),
        api("/MediaSources"),
    ]

    for path in candidates:
        try:
            r = SESSION.get(f"{ETV_HOST}{path}", timeout=10)
            ct = r.headers.get("content-type", "")
            if r.ok and "application/json" in ct:
                data = r.json()
                if data:
                    sources = data if isinstance(data, list) else [data]
                    # Filter to local sources if we got an unfiltered list
                    local = [s for s in sources if s.get("sourceType", "").lower() in ("local", "") or "local" in s.get("name", "").lower()]
                    result = (local or sources)[0]
                    log.info(f"Local media source found via {path}: id={result.get('id')}")
                    return result
        except Exception:
            continue

    # Last resort: use Swagger discovery to find the right path
    discovered = find_path("mediaSource") or find_path("mediasource") or find_path("library")
    if discovered:
        raise RuntimeError(
            f"Could not get local media source via known paths.\n"
            f"  Swagger suggests: {discovered}\n"
            f"  Run --dump-api to see all endpoints and update this script."
        )

    raise RuntimeError(
        "Could not find local media source endpoint.\n"
        f"  Run with --dump-api or check {ETV_HOST}/swagger"
    )


def get_libraries(source_id: int) -> list:
    """Fetch all libraries for the local media source."""
    candidates = [
        api(f"/mediaSources/local/libraries"),
        api(f"/MediaSources/Local/Libraries"),
        api(f"/libraries"),
        api(f"/Libraries"),
    ]
    for path in candidates:
        try:
            r = SESSION.get(f"{ETV_HOST}{path}", timeout=10)
            ct = r.headers.get("content-type", "")
            if r.ok and "application/json" in ct:
                data = r.json()
                libs = data if isinstance(data, list) else data.get("items", [data])
                if libs:
                    log.info(f"Libraries found via {path} ({len(libs)} total)")
                    return libs, path
        except Exception:
            continue
    raise RuntimeError(
        "Could not list libraries.\n"
        f"  Run with --dump-api or check {ETV_HOST}/swagger"
    )


def add_library_path(lib_path_base: str, library_id: int, folder_path: str) -> None:
    """Append a folder path to a library (idempotent)."""
    # Try to get existing paths first
    path_candidates = [
        f"{lib_path_base}/{library_id}",
        api(f"/mediaSources/local/libraries/{library_id}"),
        api(f"/MediaSources/Local/Libraries/{library_id}"),
    ]
    for p in path_candidates:
        try:
            r = SESSION.get(f"{ETV_HOST}{p}", timeout=10)
            if r.ok and "application/json" in r.headers.get("content-type", ""):
                lib = r.json()
                existing = [x["path"] for x in lib.get("paths", [])]
                if folder_path in existing:
                    log.info(f"  Path already registered: {folder_path}")
                    return
                break
        except Exception:
            continue

    # Add the path
    add_candidates = [
        (f"{lib_path_base}/{library_id}/paths", {"path": folder_path}),
        (api(f"/mediaSources/local/libraries/{library_id}/paths"), {"path": folder_path}),
        (api(f"/mediaSources/local/libraries/{library_id}"), {"paths": [{"path": folder_path}]}),
    ]
    for add_path, body in add_candidates:
        try:
            r = SESSION.post(f"{ETV_HOST}{add_path}", json=body, timeout=10)
            if r.ok:
                log.info(f"  Added path: {folder_path}")
                return
        except Exception:
            continue

    log.warning(f"  Could not add path {folder_path} — you may need to add it manually via the UI.")


def setup_libraries() -> tuple[list[int], str]:
    """Register media sub-paths. Returns (library_ids, libraries_base_path)."""
    if not MEDIA_PATH:
        raise ValueError("MEDIA_PATH is not set in .env")

    source = get_local_media_source()
    source_id = source["id"]
    libs, libs_base = get_libraries(source_id)

    library_ids = []

    for lib_type, subpath, label in [
        ("Movie",  MOVIES_SUBPATH, "Movies"),
        ("Show",   SHOWS_SUBPATH,  "Shows"),
    ]:
        if not subpath:
            continue
        folder = str(Path(MEDIA_PATH) / subpath)
        match = next(
            (l for l in libs if l.get("libraryType", "").lower() == lib_type.lower()),
            None
        )
        if match:
            log.info(f"Configuring {label} library (id={match['id']}) → {folder}")
            add_library_path(libs_base, match["id"], folder)
            library_ids.append(match["id"])
        else:
            log.warning(
                f"No {label} ({lib_type}) library found. "
                "ErsatzTV ships with default libraries — this is unexpected. "
                "Add the path manually via the UI."
            )

    return library_ids, libs_base

# ─── Step 3: Scan ─────────────────────────────────────────────────────────────

def scan_libraries(library_ids: list[int], libs_base: str, wait: bool = True) -> None:
    for lib_id in library_ids:
        log.info(f"Triggering scan for library {lib_id} ...")
        scan_paths = [
            f"{libs_base}/{lib_id}/scan",
            api(f"/libraries/{lib_id}/scan"),
            api(f"/Libraries/{lib_id}/scan"),
        ]
        for sp in scan_paths:
            try:
                r = SESSION.post(f"{ETV_HOST}{sp}", json={}, timeout=10)
                if r.status_code in (200, 202, 204):
                    log.info(f"  Scan triggered via {sp}")
                    break
            except Exception:
                continue

    if not wait:
        log.info("Not waiting for scan completion (--no-wait-scan).")
        return

    log.info("Waiting for scans to complete (polling every 10s, max 10 min) ...")
    timeout = time.time() + 600
    while time.time() < timeout:
        try:
            # Try to detect active scans
            for scan_status_path in [api("/libraries/scanning"), api("/Libraries/scanning")]:
                r = SESSION.get(f"{ETV_HOST}{scan_status_path}", timeout=5)
                if r.ok and "application/json" in r.headers.get("content-type", ""):
                    scanning = r.json()
                    ids_scanning = [s["id"] for s in (scanning if isinstance(scanning, list) else [])]
                    if not any(lid in ids_scanning for lid in library_ids):
                        log.info("Scan complete ✓")
                        return
                    break
        except Exception:
            pass
        time.sleep(10)

    log.warning("Scan status check timed out — continuing. Re-run after scanning finishes if items are missing.")

# ─── Step 4: Collection ───────────────────────────────────────────────────────

def find_or_create_collection(name: str) -> dict:
    col_path = api("/collections")
    try:
        collections = _get(col_path)
    except Exception:
        col_path = api("/Collections")
        collections = _get(col_path)

    for col in (collections if isinstance(collections, list) else []):
        if col.get("name") == name:
            log.info(f"Collection exists: id={col['id']}  '{name}'")
            return col, col_path

    log.info(f"Creating collection: '{name}'")
    col = _post(col_path, {"name": name})
    log.info(f"  Created: id={col['id']}")
    return col, col_path


def populate_collection_from_libraries(collection_id: int, library_ids: list[int], libs_base: str) -> int:
    added = 0
    for lib_id in library_ids:
        log.info(f"Fetching items from library {lib_id} ...")
        page, page_size = 0, 100
        items_path = None
        for p in [f"{libs_base}/{lib_id}/items", api(f"/libraries/{lib_id}/items"), api(f"/Libraries/{lib_id}/items")]:
            try:
                r = SESSION.get(f"{ETV_HOST}{p}", params={"pageNumber": 0, "pageSize": 1}, timeout=10)
                if r.ok and "application/json" in r.headers.get("content-type", ""):
                    items_path = p
                    break
            except Exception:
                continue

        if not items_path:
            log.warning(f"  Could not find items endpoint for library {lib_id} — skipping.")
            continue

        while True:
            items = _get(items_path, params={"pageNumber": page, "pageSize": page_size})
            item_list = items.get("items", items) if isinstance(items, dict) else items
            if not item_list:
                break
            ids = [i["id"] for i in item_list if "id" in i]
            if ids:
                try:
                    _post(api(f"/collections/{collection_id}/items"), {"mediaItemIds": ids})
                    added += len(ids)
                    log.info(f"  Added {len(ids)} items (page {page})")
                except Exception:
                    _post(api(f"/Collections/{collection_id}/items"), {"mediaItemIds": ids})
                    added += len(ids)
            if len(item_list) < page_size:
                break
            page += 1

    log.info(f"Collection populated: {added} items")
    return added

# ─── Step 5: Channel ──────────────────────────────────────────────────────────

def get_default_ffmpeg_profile_id() -> int:
    for p in [api("/ffmpegProfiles"), api("/FfmpegProfiles"), api("/ffmpeg/profiles")]:
        try:
            r = SESSION.get(f"{ETV_HOST}{p}", timeout=5)
            if r.ok and "application/json" in r.headers.get("content-type", ""):
                profiles = r.json()
                pl = profiles if isinstance(profiles, list) else profiles.get("items", [profiles])
                for prof in pl:
                    if prof.get("name", "").lower() == "default":
                        return prof["id"]
                if pl:
                    return pl[0]["id"]
        except Exception:
            continue
    return 1


def find_or_create_channel(number: int, name: str, group: str) -> dict:
    ch_path = api("/channels")
    try:
        channels = _get(ch_path)
    except Exception:
        ch_path = api("/Channels")
        channels = _get(ch_path)

    ch_list = channels if isinstance(channels, list) else channels.get("items", [])
    for ch in ch_list:
        if ch.get("number") == number:
            log.info(f"Channel exists: id={ch['id']}  #{number} '{name}'")
            return ch, ch_path

    profile_id = get_default_ffmpeg_profile_id()
    log.info(f"Creating channel #{number}: '{name}'  (ffmpegProfileId={profile_id})")
    ch = _post(ch_path, {
        "number": number,
        "name": name,
        "ffmpegProfileId": profile_id,
        "streamingMode": "TransportStream",
        "groupTitle": group,
        "progressiveSegmentCount": 0,
    })
    log.info(f"  Created: id={ch['id']}")
    return ch, ch_path

# ─── Step 6: Schedule ─────────────────────────────────────────────────────────

def find_or_create_schedule(name: str) -> dict:
    sched_path = api("/schedules")
    try:
        schedules = _get(sched_path)
    except Exception:
        sched_path = api("/Schedules")
        schedules = _get(sched_path)

    s_list = schedules if isinstance(schedules, list) else schedules.get("items", [])
    for s in s_list:
        if s.get("name") == name:
            log.info(f"Schedule exists: id={s['id']}  '{name}'")
            return s, sched_path

    log.info(f"Creating schedule: '{name}'")
    sched = _post(sched_path, {
        "name": name,
        "keepMultiPartEpisodesTogether": True,
        "randomStartPoint": False,
        "shuffleScheduleItems": False,
        "treatCollectionsAsShows": False,
    })
    log.info(f"  Created: id={sched['id']}")
    return sched, sched_path


def add_schedule_item(schedule_id: int, sched_path: str, collection_id: int) -> None:
    items_path = f"{sched_path}/{schedule_id}/items"
    try:
        items = _get(items_path)
        for item in (items if isinstance(items, list) else []):
            coll = item.get("collection") or {}
            if coll.get("id") == collection_id:
                log.info(f"  Schedule item already exists for collection {collection_id}")
                return
    except Exception:
        pass  # can't list — just try to add

    log.info(f"Adding collection {collection_id} to schedule {schedule_id}")
    _post(items_path, {
        "collectionId": collection_id,
        "collectionType": "Collection",
        "playbackOrder": "Shuffle",
        "startTime": None,
        "playoutDuration": None,
        "tailFiller": None,
        "midRollFiller": None,
        "multiplePlaybackEpisodes": None,
        "incompletePlaybackHandling": "None",
    })
    log.info("  Schedule item added")

# ─── Step 7: Playout ──────────────────────────────────────────────────────────

def find_or_create_playout(channel_id: int, schedule_id: int) -> dict:
    playout_path = api("/playouts")
    try:
        playouts = _get(playout_path)
    except Exception:
        playout_path = api("/Playouts")
        playouts = _get(playout_path)

    p_list = playouts if isinstance(playouts, list) else playouts.get("items", [])
    for p in p_list:
        if p.get("channelId") == channel_id:
            log.info(f"Playout exists: id={p['id']}  channel={channel_id}")
            return p

    log.info(f"Creating playout: channel={channel_id}  schedule={schedule_id}")
    playout = _post(playout_path, {
        "channelId": channel_id,
        "scheduleId": schedule_id,
        "playoutType": "Classic",
    })
    log.info(f"  Created: id={playout['id']}")
    return playout

# ─── Output ───────────────────────────────────────────────────────────────────

def print_jellyfin_config() -> None:
    m3u_url   = f"{ETV_HOST}/iptv/channels.m3u?apikey="
    xmltv_url = f"{ETV_HOST}/iptv/xmltv.xml?apikey="
    sep = "─" * 60
    print(f"\n{sep}")
    print("  ErsatzTV → Jellyfin Configuration")
    print(sep)
    print()
    print("  Jellyfin Admin Dashboard → Live TV:")
    print()
    print("  1. Add Tuner Device")
    print("     Tuner Type : M3U Tuner")
    print(f"     File / URL : {m3u_url}")
    print()
    print("  2. Add TV Guide Data Provider")
    print("     Type       : XMLTV")
    print(f"     URL        : {xmltv_url}")
    print()
    print("  (Append your ErsatzTV API key to both URLs if auth is enabled.)")
    print(f"{sep}\n")

# ─── Main ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="ErsatzTV first-run setup")
    p.add_argument("--no-wait-scan", action="store_true",
                   help="Don't block waiting for library scan")
    p.add_argument("--skip-populate", action="store_true",
                   help="Skip adding library items to the collection")
    p.add_argument("--dump-api", action="store_true",
                   help="Print all discovered API endpoints and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="Print config and exit without making changes")
    return p.parse_args()


def main():
    args = parse_args()

    if args.dry_run:
        log.info("DRY RUN")
        for k, v in [
            ("ETV_HOST", ETV_HOST), ("MEDIA_PATH", MEDIA_PATH),
            ("MOVIES_SUBPATH", MOVIES_SUBPATH), ("SHOWS_SUBPATH", SHOWS_SUBPATH),
            ("CHANNEL_NAME", f"{CHANNEL_NAME} (#{CHANNEL_NUMBER})"),
            ("SCHEDULE_NAME", SCHEDULE_NAME), ("COLLECTION_NAME", COLLECTION_NAME),
        ]:
            log.info(f"  {k:20s}: {v}")
        print_jellyfin_config()
        return

    wait_for_ready()
    discover_api()

    if args.dump_api:
        dump_api()
        return

    log.info("── Libraries ──")
    library_ids, libs_base = setup_libraries()

    log.info("── Scan ──")
    scan_libraries(library_ids, libs_base, wait=not args.no_wait_scan)

    log.info("── Collection ──")
    collection, col_path = find_or_create_collection(COLLECTION_NAME)
    if not args.skip_populate:
        populate_collection_from_libraries(collection["id"], library_ids, libs_base)

    log.info("── Channel ──")
    channel, _ = find_or_create_channel(CHANNEL_NUMBER, CHANNEL_NAME, CHANNEL_GROUP)

    log.info("── Schedule ──")
    schedule, sched_path = find_or_create_schedule(SCHEDULE_NAME)
    add_schedule_item(schedule["id"], sched_path, collection["id"])

    log.info("── Playout ──")
    find_or_create_playout(channel["id"], schedule["id"])

    log.info("Setup complete ✓")
    print_jellyfin_config()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except requests.HTTPError as e:
        log.error(f"HTTP {e.response.status_code} — {e.response.url}")
        try:
            log.error(f"Body: {e.response.json()}")
        except Exception:
            log.error(f"Body: {e.response.text[:300]}")
        log.error(f"Swagger UI: {ETV_HOST}/swagger  |  Run with --dump-api for endpoint list")
        sys.exit(1)
    except Exception as e:
        log.error(str(e))
        sys.exit(1)
