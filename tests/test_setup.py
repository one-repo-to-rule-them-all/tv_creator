"""
tv_creator — Test Suite
═══════════════════════

Unit tests: fully mocked, no ErsatzTV or Docker required.
Integration tests: require a live ErsatzTV instance (auto-skipped otherwise).

Run unit tests:
    pytest tests/ -v

Run integration tests (ErsatzTV must be up):
    pytest tests/ -v -m integration

Run everything:
    pytest tests/ -v --tb=short
"""

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import responses as rsps_lib  # renamed to avoid collision with pytest fixture
import requests

# ─── Make setup.py importable as a module ────────────────────────────────────

# setup.py lives at the project root (one level up from tests/)
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import setup  # noqa: E402  (imported after sys.path manipulation)

# ─── Fixtures ─────────────────────────────────────────────────────────────────

ETV = "http://localhost:8409"
API = f"{ETV}/api/v1"


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level globals between tests."""
    original_prefix = setup._API_PREFIX
    original_paths = dict(setup._SWAGGER_PATHS)
    yield
    setup._API_PREFIX = original_prefix
    setup._SWAGGER_PATHS = original_paths


@pytest.fixture
def mock_swagger():
    """Inject a minimal Swagger spec so api() resolves correctly."""
    setup._API_PREFIX = "/api/v1"
    setup._SWAGGER_PATHS = {
        "/api/v1/mediaSources/local": {"get": {}},
        "/api/v1/mediaSources/local/libraries": {"get": {}, "post": {}},
        "/api/v1/libraries/{id}/scan": {"post": {}},
        "/api/v1/collections": {"get": {}, "post": {}},
        "/api/v1/collections/{id}/items": {"post": {}},
        "/api/v1/channels": {"get": {}, "post": {}},
        "/api/v1/ffmpegProfiles": {"get": {}},
        "/api/v1/schedules": {"get": {}, "post": {}},
        "/api/v1/schedules/{id}/items": {"get": {}, "post": {}},
        "/api/v1/playouts": {"get": {}, "post": {}},
    }


# ─── Unit: HTTP helpers ───────────────────────────────────────────────────────

class TestJsonOrRaise:
    def _make_response(self, status=200, body=None, content_type="application/json"):
        r = requests.models.Response()
        r.status_code = status
        r.headers["content-type"] = content_type
        r._content = json.dumps(body).encode() if body is not None else b""
        r.url = f"{API}/test"
        return r

    def test_returns_dict_on_valid_json(self):
        r = self._make_response(body={"id": 1})
        assert setup._json_or_raise(r) == {"id": 1}

    def test_returns_list_on_valid_json_array(self):
        r = self._make_response(body=[{"id": 1}, {"id": 2}])
        assert setup._json_or_raise(r) == [{"id": 1}, {"id": 2}]

    def test_returns_empty_dict_on_empty_body(self):
        r = self._make_response(body=None)
        assert setup._json_or_raise(r) == {}

    def test_raises_on_html_response(self):
        r = self._make_response(
            body=None, content_type="text/html"
        )
        r._content = b"<html><body>Not Found</body></html>"
        with pytest.raises(RuntimeError, match="HTML instead of JSON"):
            setup._json_or_raise(r)

    def test_raises_on_http_error(self):
        r = self._make_response(status=404, body={"message": "not found"})
        with pytest.raises(requests.HTTPError):
            setup._json_or_raise(r)


# ─── Unit: Swagger discovery ──────────────────────────────────────────────────

class TestDiscoverApi:
    @rsps_lib.activate
    def test_loads_spec_and_sets_prefix(self):
        spec = {
            "paths": {
                "/api/v1/channels": {"get": {}, "post": {}},
                "/api/v1/schedules": {"get": {}},
            }
        }
        rsps_lib.add(
            rsps_lib.GET,
            f"{ETV}/swagger/v1/swagger.json",
            json=spec,
            status=200,
        )
        setup._API_PREFIX = "/api/vX"  # will be overwritten
        setup.ETV_HOST = ETV
        setup.discover_api()
        assert setup._API_PREFIX == "/api/v1"
        assert "/api/v1/channels" in setup._SWAGGER_PATHS

    @rsps_lib.activate
    def test_falls_back_gracefully_when_swagger_missing(self, caplog):
        for path in [
            "/swagger/v1/swagger.json",
            "/api/swagger.json",
            "/swagger.json",
            "/api/v1/swagger.json",
        ]:
            rsps_lib.add(rsps_lib.GET, f"{ETV}{path}", status=404, body="Not found")
        import logging
        with caplog.at_level(logging.WARNING):
            setup.ETV_HOST = ETV
            setup.discover_api()
        assert "Could not load Swagger spec" in caplog.text

    def test_find_path_returns_matching_endpoint(self, mock_swagger):
        result = setup.find_path("channels", "get")
        assert result == "/api/v1/channels"

    def test_find_path_returns_none_when_not_found(self, mock_swagger):
        result = setup.find_path("nonexistent_endpoint_xyz")
        assert result is None


# ─── Unit: wait_for_ready ─────────────────────────────────────────────────────

class TestWaitForReady:
    @rsps_lib.activate
    def test_succeeds_when_health_ok(self):
        rsps_lib.add(rsps_lib.GET, f"{ETV}/health", status=200, body="Healthy")
        setup.ETV_HOST = ETV
        setup.wait_for_ready(max_wait=10)  # should not raise

    @rsps_lib.activate
    def test_raises_timeout_when_health_never_responds(self):
        rsps_lib.add(rsps_lib.GET, f"{ETV}/health", body=requests.ConnectionError())
        setup.ETV_HOST = ETV
        with pytest.raises(TimeoutError):
            setup.wait_for_ready(max_wait=1)


# ─── Unit: get_local_media_source ────────────────────────────────────────────

class TestGetLocalMediaSource:
    @rsps_lib.activate
    def test_returns_first_valid_source(self, mock_swagger):
        setup.ETV_HOST = ETV
        rsps_lib.add(
            rsps_lib.GET,
            f"{ETV}/api/v1/mediaSources/local",
            json=[{"id": 1, "name": "Local", "sourceType": "local"}],
            status=200,
        )
        source = setup.get_local_media_source()
        assert source["id"] == 1

    @rsps_lib.activate
    def test_tries_fallback_paths_on_html_response(self, mock_swagger):
        setup.ETV_HOST = ETV
        # Primary path returns HTML (wrong path)
        rsps_lib.add(
            rsps_lib.GET,
            f"{ETV}/api/v1/mediaSources/local",
            body="<html>Not found</html>",
            content_type="text/html",
            status=200,
        )
        # Fallback path returns correct JSON
        rsps_lib.add(
            rsps_lib.GET,
            f"{ETV}/api/v1/MediaSources/Local",
            json=[{"id": 1, "name": "Local", "sourceType": "local"}],
            status=200,
        )
        source = setup.get_local_media_source()
        assert source["id"] == 1

    @rsps_lib.activate
    def test_raises_when_all_paths_fail(self, mock_swagger):
        setup.ETV_HOST = ETV
        for path in [
            "/api/v1/mediaSources/local",
            "/api/v1/MediaSources/Local",
            "/api/v1/localMediaSources",
            "/api/v1/LocalMediaSources",
            "/api/v1/mediaSources",
            "/api/v1/MediaSources",
        ]:
            rsps_lib.add(rsps_lib.GET, f"{ETV}{path}", status=404, body="Not found")
        with pytest.raises(RuntimeError, match="Could not find local media source"):
            setup.get_local_media_source()


# ─── Unit: find_or_create_collection ────────────────────────────────────────

class TestFindOrCreateCollection:
    @rsps_lib.activate
    def test_returns_existing_collection(self, mock_swagger):
        setup.ETV_HOST = ETV
        existing = [{"id": 42, "name": "All My Media"}]
        rsps_lib.add(rsps_lib.GET, f"{ETV}/api/v1/collections", json=existing, status=200)
        col, _ = setup.find_or_create_collection("All My Media")
        assert col["id"] == 42

    @rsps_lib.activate
    def test_creates_collection_when_not_found(self, mock_swagger):
        setup.ETV_HOST = ETV
        rsps_lib.add(rsps_lib.GET, f"{ETV}/api/v1/collections", json=[], status=200)
        rsps_lib.add(rsps_lib.POST, f"{ETV}/api/v1/collections",
                     json={"id": 99, "name": "All My Media"}, status=200)
        col, _ = setup.find_or_create_collection("All My Media")
        assert col["id"] == 99

    @rsps_lib.activate
    def test_does_not_duplicate_existing_collection(self, mock_swagger):
        setup.ETV_HOST = ETV
        existing = [{"id": 42, "name": "All My Media"}]
        rsps_lib.add(rsps_lib.GET, f"{ETV}/api/v1/collections", json=existing, status=200)
        # POST should NOT be called — if it were, responses would raise
        col, _ = setup.find_or_create_collection("All My Media")
        assert col["id"] == 42
        # Verify no POST was made
        assert all(c.request.method == "GET" for c in rsps_lib.calls)


# ─── Unit: find_or_create_channel ────────────────────────────────────────────

class TestFindOrCreateChannel:
    @rsps_lib.activate
    def test_returns_existing_channel(self, mock_swagger):
        setup.ETV_HOST = ETV
        existing = [{"id": 10, "number": 1, "name": "My Media"}]
        rsps_lib.add(rsps_lib.GET, f"{ETV}/api/v1/channels", json=existing, status=200)
        ch, _ = setup.find_or_create_channel(1, "My Media", "IPTV")
        assert ch["id"] == 10

    @rsps_lib.activate
    def test_creates_channel_with_mpeg_ts_mode(self, mock_swagger):
        setup.ETV_HOST = ETV
        rsps_lib.add(rsps_lib.GET, f"{ETV}/api/v1/channels", json=[], status=200)
        rsps_lib.add(rsps_lib.GET, f"{ETV}/api/v1/ffmpegProfiles",
                     json=[{"id": 1, "name": "default"}], status=200)
        rsps_lib.add(rsps_lib.POST, f"{ETV}/api/v1/channels",
                     json={"id": 5, "number": 1, "name": "My Media"}, status=200)
        ch, _ = setup.find_or_create_channel(1, "My Media", "IPTV")
        assert ch["id"] == 5
        # Verify streaming mode sent to API
        post_body = json.loads(rsps_lib.calls[-1].request.body)
        assert post_body["streamingMode"] == "TransportStream"


# ─── Unit: find_or_create_schedule ───────────────────────────────────────────

class TestFindOrCreateSchedule:
    @rsps_lib.activate
    def test_returns_existing_schedule(self, mock_swagger):
        setup.ETV_HOST = ETV
        existing = [{"id": 7, "name": "My Media Schedule"}]
        rsps_lib.add(rsps_lib.GET, f"{ETV}/api/v1/schedules", json=existing, status=200)
        sched, _ = setup.find_or_create_schedule("My Media Schedule")
        assert sched["id"] == 7

    @rsps_lib.activate
    def test_creates_schedule_when_missing(self, mock_swagger):
        setup.ETV_HOST = ETV
        rsps_lib.add(rsps_lib.GET, f"{ETV}/api/v1/schedules", json=[], status=200)
        rsps_lib.add(rsps_lib.POST, f"{ETV}/api/v1/schedules",
                     json={"id": 8, "name": "My Media Schedule"}, status=200)
        sched, _ = setup.find_or_create_schedule("My Media Schedule")
        assert sched["id"] == 8


# ─── Unit: find_or_create_playout ────────────────────────────────────────────

class TestFindOrCreatePlayout:
    @rsps_lib.activate
    def test_returns_existing_playout(self, mock_swagger):
        setup.ETV_HOST = ETV
        existing = [{"id": 3, "channelId": 5, "scheduleId": 8}]
        rsps_lib.add(rsps_lib.GET, f"{ETV}/api/v1/playouts", json=existing, status=200)
        playout = setup.find_or_create_playout(channel_id=5, schedule_id=8)
        assert playout["id"] == 3

    @rsps_lib.activate
    def test_creates_playout_with_classic_type(self, mock_swagger):
        setup.ETV_HOST = ETV
        rsps_lib.add(rsps_lib.GET, f"{ETV}/api/v1/playouts", json=[], status=200)
        rsps_lib.add(rsps_lib.POST, f"{ETV}/api/v1/playouts",
                     json={"id": 11, "channelId": 5, "scheduleId": 8}, status=200)
        playout = setup.find_or_create_playout(channel_id=5, schedule_id=8)
        assert playout["id"] == 11
        post_body = json.loads(rsps_lib.calls[-1].request.body)
        assert post_body["playoutType"] == "Classic"


# ─── Unit: dump_api ───────────────────────────────────────────────────────────

class TestDumpApi:
    def test_prints_endpoints(self, mock_swagger, capsys):
        setup.dump_api()
        out = capsys.readouterr().out
        assert "/api/v1/channels" in out
        assert "GET" in out

    def test_handles_empty_swagger(self, capsys):
        setup._SWAGGER_PATHS = {}
        setup.dump_api()
        out = capsys.readouterr().out
        assert "No Swagger paths" in out


# ─── Integration tests ────────────────────────────────────────────────────────
# These tests require a running ErsatzTV instance. They are automatically
# skipped if ErsatzTV is not reachable at ETV_HOST.

def _etv_is_reachable() -> bool:
    import os
    host = os.getenv("ETV_HOST", "http://localhost:8409")
    try:
        r = requests.get(f"{host}/health", timeout=3)
        return r.ok
    except Exception:
        return False


requires_etv = pytest.mark.skipif(
    not _etv_is_reachable(),
    reason="ErsatzTV not reachable — skipping integration tests",
)
integration = pytest.mark.integration


@requires_etv
@integration
class TestIntegration:
    """
    End-to-end integration tests.
    These mutate ErsatzTV state — run against a dev/test instance only.
    """

    def test_health_endpoint_responds(self):
        import os
        host = os.getenv("ETV_HOST", "http://localhost:8409")
        r = requests.get(f"{host}/health", timeout=5)
        assert r.ok, f"Health check failed: {r.status_code}"

    def test_swagger_spec_is_reachable(self):
        import os
        setup.ETV_HOST = os.getenv("ETV_HOST", "http://localhost:8409")
        setup.discover_api()
        assert len(setup._SWAGGER_PATHS) > 0, "Swagger returned no paths"

    def test_local_media_source_exists(self):
        import os
        setup.ETV_HOST = os.getenv("ETV_HOST", "http://localhost:8409")
        setup.discover_api()
        source = setup.get_local_media_source()
        assert "id" in source

    def test_collection_create_is_idempotent(self):
        import os
        setup.ETV_HOST = os.getenv("ETV_HOST", "http://localhost:8409")
        setup.discover_api()
        name = "_test_collection_idempotency"
        col1, _ = setup.find_or_create_collection(name)
        col2, _ = setup.find_or_create_collection(name)
        assert col1["id"] == col2["id"], "Idempotency failed: two collections created"

    def test_channel_create_is_idempotent(self):
        import os
        setup.ETV_HOST = os.getenv("ETV_HOST", "http://localhost:8409")
        setup.discover_api()
        ch1, _ = setup.find_or_create_channel(999, "_test_channel", "test")
        ch2, _ = setup.find_or_create_channel(999, "_test_channel", "test")
        assert ch1["id"] == ch2["id"], "Idempotency failed: two channels created"

    def test_m3u_endpoint_returns_content(self):
        import os
        host = os.getenv("ETV_HOST", "http://localhost:8409")
        r = requests.get(f"{host}/iptv/channels.m3u", timeout=10)
        assert r.ok
        assert "#EXTM3U" in r.text, "M3U response does not look like a valid playlist"

    def test_xmltv_endpoint_returns_content(self):
        import os
        host = os.getenv("ETV_HOST", "http://localhost:8409")
        r = requests.get(f"{host}/iptv/xmltv.xml", timeout=10)
        assert r.ok
        assert "<?xml" in r.text or "<tv" in r.text, "XMLTV response does not look like valid XML"
