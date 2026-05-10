# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```powershell
# Install dependencies
pip install -r requirements.txt

# Preview discovered media groups (no DB writes)
python full_setup.py --discover

# Dry run — print all planned changes, touch nothing
python full_setup.py --dry-run

# Full setup (ErsatzTV channels + restart)
python full_setup.py

# Full setup, skip restart (useful when ErsatzTV is still indexing)
python full_setup.py --no-restart

# Jellyfin-only (after ErsatzTV is healthy and fully indexed)
python full_setup.py --jellyfin-only --jellyfin-user <user> --jellyfin-password <pw>

# Verify syntax after any edit to full_setup.py
python -m py_compile full_setup.py

# Push files to GitHub (requires GITHUB_TOKEN env var)
$env:GITHUB_TOKEN="<token>"
python create_pr.py
```

## Architecture

### full_setup.py — single-file orchestrator

The script is divided into five layers:

**1. Config block (top of file)**
All tuneable constants live here: `ETV_DB_PATH`, `ETV_HOST`, `ERSATZTV_LAN`, `JELLYFIN_HOST`, `CHANNEL_DEFS`. The LAN IP (`ERSATZTV_LAN = "http://192.168.68.61:8409"`) is distinct from `ETV_HOST` (`localhost`) — the LAN IP is what Jellyfin clients use to stream; localhost is what the script uses to talk to ErsatzTV directly.

**2. ErsatzTV lifecycle (`ensure_ersatztv`, `etv_is_running`, `etv_is_indexing`, `wait_for_idle`, `restart_ersatztv`)**
Downloads ErsatzTV from GitHub if the exe is missing. Polls `GET /api/v1/libraries` to check scan state before restarting. Restart is done with `taskkill` + `Popen(DETACHED_PROCESS)` then polls until `/api/v1/channels` responds.

**3. Media discovery (`discover_media`, `assign_channels`)**
Reads the ErsatzTV SQLite DB directly — no API. Queries `ShowMetadata`, `MovieMetadata`, and `OtherVideoMetadata` joined through `MediaItem`. OtherVideo items (fitness videos, stand-up specials) are grouped by `LibraryPath.Path` substring rather than title. `assign_channels` matches group keys against `CHANNEL_DEFS[].patterns` using substring matching; empty `patterns` = catch-all (channel 1).

**4. DB write operations (`upsert_channel`, `upsert_collection`, `upsert_schedule`, `upsert_playout`)**
All writes go directly into the ErsatzTV SQLite database (`ersatztv.sqlite3`). Each function is idempotent — it checks for an existing row by name/number before inserting.

Critical: the native Windows ErsatzTV schema is older than the Docker schema and is missing `StreamingEngine` and `NextEngineTextSubtitleMode` columns. `upsert_channel` uses `get_channel_cols(conn)` (`PRAGMA table_info(Channel)`) to dynamically build the INSERT and only include columns that actually exist.

**5. ErsatzTV pre-flight + Jellyfin config (`verify_ersatztv`, `configure_jellyfin`)**
ErsatzTV's API returns Blazor HTML unless `Accept: application/json` is sent — all API calls use `JSON_HEADERS`. The playout API also returns HTML, so playout readiness is inferred from the M3U feed channel count instead.

Jellyfin auth uses the `X-Emby-Authorization` header pattern. M3U tuner add (`POST /LiveTv/TunerHosts`) works fine. XMLTV guide (`POST /LiveTv/ListingProviders`) returns 404 in Jellyfin 10.10.7 regardless of `validateListings` query params — this step is skipped and documented as a manual action.

### create_pr.py — GitHub push helper

Uses the GitHub Contents API (no local git required). Pushes the files listed in `FILES` to branch `feat/full-automated-setup` in `one-repo-to-rule-them-all/tv_creator`, then opens or updates a PR. Reads `GITHUB_TOKEN` from the environment.

## Key gotchas

- **ErsatzTV API returns HTML by default.** Always include `Accept: application/json` on every request.
- **Schema version mismatch.** Never hardcode the `Channel` INSERT columns — always use `get_channel_cols(conn)` to build it dynamically.
- **File tail corruption.** When editing `full_setup.py` with a mix of tools (Edit, Write, bash `cat >>`), the `if __name__ == "__main__":` block at the end can get duplicated or truncated. Always run `python -m py_compile full_setup.py` after edits.
- **Restart timing.** Call `wait_for_idle()` before `restart_ersatztv()` — restarting while ErsatzTV is indexing cuts the scan short and leaves libraries incomplete.
- **XMLTV guide URL.** The XMLTV guide must be added manually in Jellyfin: Dashboard → Live TV → TV Guide Data Providers → + → XMLTV → `http://192.168.68.61:8409/iptv/xmltv.xml`.
