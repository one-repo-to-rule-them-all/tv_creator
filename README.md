# tv_creator

> **One-command automation** that turns a personal NAS media library into a local live TV system — powered by [ErsatzTV](https://ersatztv.org), streamed to [Jellyfin](https://jellyfin.org).

`full_setup.py` handles the entire ErsatzTV setup in a single script: downloads ErsatzTV if it isn't installed, reads your media from the SQLite database, creates channels/collections/schedules/playouts, restarts ErsatzTV to build the playouts, and wires up Jellyfin's M3U tuner.

---

## How it works

```
NAS Media (Z:/)
  Movies/
  Shows/
  Fitness/          ──────▶  ErsatzTV (native Windows, localhost:8409)
  Stand Up Comedy/             │
                               │  full_setup.py reads SQLite DB directly
                               │  → groups media by title/path
                               │  → creates channels, collections,
                               │    schedules, playouts
                               │  → restarts ErsatzTV
                               ▼
                    ┌──────────────────────────┐
                    │         Jellyfin          │
                    │  M3U Tuner (auto)         │
                    │  XMLTV Guide (manual*)    │
                    └──────────────────────────┘
```

*XMLTV guide is added manually — see [Connect Jellyfin](#connect-jellyfin) below.

---

## Channels

| # | Name | Content | Playback |
|---|---|---|---|
| 1 | All My Media | Everything | Shuffle |
| 2 | I Love Lucy | I Love Lucy episodes | In-order |
| 4 | Lord of the Ring | LotR films | In-order |
| 5 | Friday | Friday films | Shuffle |
| 6 | 90s Toon | Rugrats, Hey Arnold, Doug, Animaniacs, etc. | Shuffle |
| 7 | Boondocks | Boondocks episodes | In-order |
| 8 | Fitness | Fitness videos | Shuffle |
| 9 | Stand Up | Stand-up specials | Shuffle |

Channel definitions live at the top of `full_setup.py` — edit `CHANNEL_DEFS` to add, remove, or rename channels.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | Windows |
| ErsatzTV | Downloaded automatically if not present |
| Jellyfin 10.8+ | For Live TV client integration |
| NAS / local media | Mounted as `Z:/` |

---

## Quick start

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and edit config
copy .env.example .env
# → Set ETV_DB_PATH, ETV_HOST, NAS_CONTAINER_PATH if different from defaults

# 3. Preview what media groups will be discovered (no writes)
python full_setup.py --discover

# 4. Dry run (no DB writes)
python full_setup.py --dry-run

# 5. Full setup
python full_setup.py
```

---

## Usage

```
python full_setup.py [options]

Options:
  --dry-run             Print what would happen; make no changes
  --discover            Show discovered media groups and exit
  --no-restart          Skip ErsatzTV restart after DB setup
  --jellyfin-only       Only configure Jellyfin (skip ErsatzTV DB setup)
  --jellyfin-user U     Jellyfin admin username
  --jellyfin-password P Jellyfin admin password
```

**Typical workflows:**

```powershell
# ErsatzTV channels only (let it finish indexing before Jellyfin)
python full_setup.py

# Once ErsatzTV is fully indexed, wire up Jellyfin
python full_setup.py --jellyfin-only --jellyfin-user rbaez --jellyfin-password <pw>
```

---

## Connect Jellyfin

### M3U Tuner (automated)

`--jellyfin-only` adds the M3U tuner automatically:

```
http://192.168.68.61:8409/iptv/channels.m3u
```

### XMLTV Guide (manual)

Due to a Jellyfin 10.10.7 API quirk, the XMLTV guide provider must be added manually:

1. Jellyfin → Admin Dashboard → Live TV
2. **TV Guide Data Providers** → `+` → XMLTV
3. File or URL: `http://192.168.68.61:8409/iptv/xmltv.xml`
4. Enable for all tuner devices → Save

---

## Configuration

All settings live in `.env` (copy from `.env.example`).

| Variable | Default | Description |
|---|---|---|
| `ETV_DB_PATH` | `C:\Users\rbaez\AppData\Local\ersatztv\ersatztv.sqlite3` | Path to ErsatzTV SQLite database |
| `ETV_HOST` | `http://localhost:8409` | ErsatzTV base URL |
| `ETV_EXE_PATH` | `C:\Users\rbaez\ersatztv\ErsatzTV.exe` | ErsatzTV executable (downloaded here if missing) |
| `NAS_CONTAINER_PATH` | `Z:/` | Root of your media drive |

---

## Troubleshooting

### ErsatzTV API returns HTML instead of JSON

ErsatzTV is a Blazor Server app. The script always sends `Accept: application/json` — if you're calling the API manually, add that header.

### Channels missing / playouts not built

ErsatzTV rebuilds playouts on startup. After `full_setup.py` runs it restarts ErsatzTV automatically. Wait for the startup to complete (usually 10–30 seconds) before checking Jellyfin.

If playouts still show as empty, go to ErsatzTV → Playouts and verify each one is listed. If ErsatzTV was mid-scan when the script ran, re-run with `--no-restart` after indexing completes, then restart ErsatzTV manually.

### Fitness / Stand Up channels empty

These channels use OtherVideo items matched by library path. Verify your NAS has folders named `Fitness` and `Stand Up Comedy` (case-insensitive match). Run `--discover` to see what OtherVideo items are found.

### XMLTV guide shows 404 from the script

Known issue with Jellyfin 10.10.7 — add the XMLTV guide manually (see [Connect Jellyfin](#connect-jellyfin)).

---

## License

MIT — see [LICENSE](LICENSE) for details.
