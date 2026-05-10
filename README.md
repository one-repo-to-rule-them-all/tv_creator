# tv_creator

> **One-command automation** that turns a personal media library into a live TV channel — powered by [ErsatzTV](https://ersatztv.org), streamed to [Jellyfin](https://jellyfin.org).

`tv_creator` handles the full ErsatzTV onboarding flow in a single script:
spins up the container, registers your media libraries, triggers a scan,
creates a collection → channel → schedule → playout, then prints the M3U and
XMLTV URLs ready to paste into Jellyfin Live TV.

---

## How it works

```
Your Media
  Movies/                     ┌─────────────────────┐
  Shows/           ──────────▶│     ErsatzTV         │
  ...                         │  ┌───────────────┐   │
                              │  │ Local Library │   │
                              │  │  (Movies)     │   │
docker-compose.yml            │  │  (Shows)      │   │
  mounts media read-only      │  └──────┬────────┘   │
                              │         │ scan        │
                              │  ┌──────▼────────┐   │
setup.py                      │  │  Collection   │   │
  discovers API via Swagger   │  │ "All My Media"│   │
  registers paths             │  └──────┬────────┘   │
  creates channel, schedule   │         │             │
  creates playout             │  ┌──────▼────────┐   │
                              │  │   Schedule    │   │◀── setup.py
                              │  │  + Playout    │   │
                              │  └──────┬────────┘   │
                              │         │             │
                              │  ┌──────▼────────┐   │
                              │  │  Channel #1   │   │
                              │  │  (MPEG-TS)    │   │
                              │  └──────┬────────┘   │
                              └─────────┼─────────────┘
                                        │
                          ┌─────────────▼──────────────┐
                          │           Jellyfin           │
                          │  M3U Tuner + XMLTV Guide     │
                          └──────────────────────────────┘
```

---

## Project layout

```
tv_creator/
├── docker-compose.yml        # ErsatzTV container definition
├── .env.example              # All config vars with inline docs
├── setup.py                  # End-to-end automation script
├── requirements.txt          # Runtime Python dependencies
├── requirements-dev.txt      # Dev/test dependencies
└── tests/
    └── test_setup.py         # Unit + integration test suite
```

---

## Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Docker + Docker Compose | Docker 24 / Compose v2 | `docker compose` (not `docker-compose`) |
| Python | 3.10+ | Windows, macOS, Linux all supported |
| ErsatzTV | latest legacy | Included in `docker-compose.yml` |
| Jellyfin | 10.8+ | For Live TV client integration |

Your media should be organized so that each library type lives under its own folder:

```
/path/to/your/media/
├── Movies/
│   ├── The Matrix (1999)/
│   │   └── The Matrix (1999).mkv
│   └── ...
└── Shows/
    ├── Breaking Bad/
    │   └── Season 01/
    │       └── Breaking.Bad.S01E01.mkv
    └── ...
```

ErsatzTV reads metadata from [NFO files](https://kodi.wiki/view/NFO_files) if present, otherwise uses filename heuristics.

---

## Quick start

```bash
# 1. Clone and enter the project
git clone https://github.com/one-repo-to-rule-them-all/tv_creator.git
cd tv_creator

# 2. Configure
cp .env.example .env
#    → Edit .env: set MEDIA_PATH and TZ at minimum

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Start ErsatzTV (skip if it's already running elsewhere)
docker compose up -d

# 5. Run the setup script
python setup.py
```

That's it. When it finishes it prints the M3U + XMLTV URLs for Jellyfin.

---

## Configuration

All settings live in `.env` (copy from `.env.example`).

### Required

| Variable | Example | Description |
|---|---|---|
| `MEDIA_PATH` | `/mnt/nas/media` | Absolute path to your media root on the **host** machine. Must match the container mount path exactly. |
| `TZ` | `America/Chicago` | Timezone for EPG scheduling. Full list: [tz database](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) |

### Optional — paths

| Variable | Default | Description |
|---|---|---|
| `MOVIES_SUBPATH` | `Movies` | Sub-folder under `MEDIA_PATH` for movies. Leave blank to skip. |
| `SHOWS_SUBPATH` | `Shows` | Sub-folder under `MEDIA_PATH` for TV shows. Leave blank to skip. |

### Optional — channel & schedule

| Variable | Default | Description |
|---|---|---|
| `CHANNEL_NUMBER` | `1` | Channel number in the EPG. Must be unique. |
| `CHANNEL_NAME` | `My Media` | Display name in Jellyfin's channel guide. |
| `CHANNEL_GROUP` | `IPTV` | Group/category shown in the M3U. |
| `SCHEDULE_NAME` | `My Media Schedule` | Internal ErsatzTV schedule name. |
| `COLLECTION_NAME` | `All My Media` | Internal ErsatzTV collection name. |

### Optional — infrastructure

| Variable | Default | Description |
|---|---|---|
| `ETV_HOST` | `http://localhost:8409` | ErsatzTV base URL (no trailing slash). Change if running remotely. |
| `ETV_PORT` | `8409` | Host-side port in `docker-compose.yml`. |
| `ETV_CONFIG_PATH` | `./ersatztv-config` | Where ErsatzTV persists its database and settings. |

---

## Detailed setup guide

### 1. Organize your media

ErsatzTV scans folder structures. Movies should be in their own named folders:

```
Movies/
└── Movie Title (Year)/
    └── Movie Title (Year).mkv   ← or .mp4, .avi, etc.
```

Shows should follow season/episode structure:

```
Shows/
└── Show Name/
    └── Season 01/
        └── Show.Name.S01E01.Episode.Title.mkv
```

### 2. Edit `.env`

```bash
cp .env.example .env
```

At minimum, set:

```dotenv
MEDIA_PATH=/absolute/path/to/your/media
TZ=America/New_York
```

> **Windows users:** use forward slashes and the full path, e.g. `MEDIA_PATH=C:/Users/you/Videos`. The Docker Compose mount uses the same path inside the container, so ErsatzTV will see the exact same path string.

### 3. Start ErsatzTV

```bash
docker compose up -d
```

Verify it's up:

```bash
docker compose ps        # should show status: healthy
curl http://localhost:8409/health
```

The web UI is at [http://localhost:8409](http://localhost:8409).

### 4. Run the setup script

```bash
python setup.py
```

The script is fully **idempotent** — safe to re-run. It checks for existing libraries, collections, channels, and schedules before creating anything new.

**Useful flags:**

| Flag | Purpose |
|---|---|
| `--dry-run` | Print config and exit without touching ErsatzTV |
| `--dump-api` | Print all API endpoints discovered from Swagger and exit |
| `--no-wait-scan` | Trigger scan and continue without blocking (for large libraries) |
| `--skip-populate` | Skip adding items to the collection (useful when re-running mid-scan) |

Example for a large library:

```bash
# Kick off the scan, don't wait for it
python setup.py --no-wait-scan

# Once you've confirmed the scan is done in the ErsatzTV UI, re-run to populate
python setup.py --skip-populate=false
```

### 5. Connect Jellyfin

After `setup.py` runs successfully it prints two URLs:

```
M3U URL  : http://localhost:8409/iptv/channels.m3u?apikey=
XMLTV URL: http://localhost:8409/iptv/xmltv.xml?apikey=
```

In **Jellyfin → Admin Dashboard → Live TV**:

1. **Add Tuner Device** → Tuner Type: `M3U Tuner` → paste the M3U URL → Save
2. **Add TV Guide Data** → Type: `XMLTV` → paste the XMLTV URL → Save
3. Wait for Jellyfin to refresh the EPG (usually a few minutes)

If ErsatzTV auth is enabled, append your API key to both URLs.

---

## Testing

### Unit tests

Unit tests mock all HTTP calls and run without ErsatzTV or Docker.

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

### Integration tests

Integration tests run against a live ErsatzTV instance. They are skipped automatically unless `ETV_HOST` is reachable.

```bash
# Start ErsatzTV first, then:
pytest tests/ -v -m integration
```

Or run everything together:

```bash
pytest tests/ -v --tb=short
```

### Debugging a failed run

```bash
# See every API endpoint the script found on your ErsatzTV version
python setup.py --dump-api

# Dry-run to confirm .env is loaded correctly
python setup.py --dry-run

# Check the ErsatzTV Swagger UI directly
open http://localhost:8409/swagger
```

### Manual smoke test checklist

After a successful `setup.py` run, verify the following in the ErsatzTV UI at [http://localhost:8409](http://localhost:8409):

- [ ] **Media Sources → Local** — your movie/show paths are listed
- [ ] **Libraries** — item counts are greater than 0
- [ ] **Collections** — "All My Media" exists and has items
- [ ] **Channels** — Channel #1 is listed
- [ ] **Schedules** — schedule exists and has at least one item
- [ ] **Playouts** — a playout is active for Channel #1
- [ ] Visit `http://localhost:8409/iptv/channels.m3u` directly — should return an M3U playlist

---

## Troubleshooting

### `Fatal: Expecting value` / HTML response instead of JSON

The API path used doesn't exist on your ErsatzTV version. Run:

```bash
python setup.py --dump-api
```

This prints every endpoint Swagger exposes. Cross-reference with the output to identify the correct path, then open an issue or update `setup.py` accordingly.

### `MEDIA_PATH is not set`

Copy and edit the env file:

```bash
cp .env.example .env
# Set MEDIA_PATH= to your absolute media root
```

### Library scan takes too long

Large libraries can take minutes to hours. Run with `--no-wait-scan` and monitor progress in the ErsatzTV UI under **Libraries**.

### ErsatzTV won't start on Windows/macOS in Docker

ErsatzTV Docker images are Linux-only and hardware acceleration is not supported on Windows/macOS Docker. Run ErsatzTV natively on those platforms and point `ETV_HOST` at the native instance:

```dotenv
ETV_HOST=http://localhost:8409
```

Then skip the `docker compose up` step and run `setup.py` directly.

### Jellyfin shows no channels after adding M3U

- Confirm the playout is running in ErsatzTV → Playouts
- Trigger a manual EPG refresh in Jellyfin → Admin → Live TV → Refresh Guide
- Check that the M3U URL returns content: `curl http://localhost:8409/iptv/channels.m3u`

---

## Contributing

1. Fork the repo
2. Create a branch: `git checkout -b feat/your-feature`
3. Make your changes and add tests
4. Run the test suite: `pytest tests/ -v`
5. Open a PR against `main`

---

## License

MIT — see [LICENSE](LICENSE) for details.
