import json
import os
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
import requests

import utils
from jellyfin import JellyfinAPI, _parse_playback_datetime
from toggl import TogglAPI


class TestUtilityFunctions:
    """Test utility functions."""

    def test_timestamp_format(self):
        """Test timestamp returns correct format."""
        ts = utils.timestamp()
        assert len(ts) == 19
        assert ts[4] == "-"
        assert ts[10] == " "

    def test_load_json_file_exists(self, tmp_path):
        """Test loading existing JSON file."""
        test_file = tmp_path / "test.json"
        test_data = {"key": "value"}
        test_file.write_text(json.dumps(test_data))

        result = utils.load_json_file(str(test_file))
        assert result == test_data

    def test_load_json_file_not_exists(self):
        """Test loading non-existent JSON file returns None."""
        result = utils.load_json_file("nonexistent.json")
        assert result is None

    def test_load_json_file_empty(self, tmp_path):
        """Test loading empty JSON file returns None."""
        test_file = tmp_path / "empty.json"
        test_file.write_text("")

        result = utils.load_json_file(str(test_file))
        assert result is None

    def test_load_json_file_invalid_json(self, tmp_path):
        """Test loading invalid JSON file returns None."""
        test_file = tmp_path / "bad.json"
        test_file.write_text("{not valid json")

        result = utils.load_json_file(str(test_file))
        assert result is None

    def test_save_json_file(self, tmp_path):
        """Test saving JSON file with correct permissions."""
        test_file = tmp_path / "test.json"
        test_data = {"key": "value"}

        utils.save_json_file(str(test_file), test_data)

        assert test_file.exists()
        assert json.loads(test_file.read_text()) == test_data
        stat = os.stat(test_file)
        assert oct(stat.st_mode)[-3:] == "600"

    def test_save_json_file_creates_parent_dirs(self, tmp_path):
        """Test saving JSON file creates parent directories if needed."""
        test_file = tmp_path / "nested" / "dir" / "test.json"
        utils.save_json_file(str(test_file), {"x": 1})
        assert test_file.exists()


class TestCheckRequiredEnvVariables:
    """Test environment variable validation."""

    def test_exits_when_variable_missing(self):
        required = [
            "JELLYFIN_URL",
            "JELLYFIN_API_KEY",
            "TOGGL_API_TOKEN",
            "TOGGL_WORKSPACE_ID",
            "TOGGL_PROJECT_ID",
        ]
        clean_env = {k: v for k, v in os.environ.items() if k not in required}
        with patch.dict(os.environ, clean_env, clear=True):
            with pytest.raises(SystemExit):
                utils.check_required_env_variables()

    def test_passes_when_all_present(self):
        env = {
            "JELLYFIN_URL": "https://jellyfin.example.com",
            "JELLYFIN_API_KEY": "key",
            "TOGGL_API_TOKEN": "token",
            "TOGGL_WORKSPACE_ID": "123",
            "TOGGL_PROJECT_ID": "456",
        }
        with patch.dict(os.environ, env):
            utils.check_required_env_variables()  # should not raise


class TestParsePlaybackDatetime:
    """Test the .NET-style DateCreated parsing helper."""

    def test_parses_without_fractional_seconds(self):
        result = _parse_playback_datetime("2026-01-15 20:30:00")
        assert result == datetime(2026, 1, 15, 20, 30, 0)

    def test_parses_with_7_digit_fractional_seconds(self):
        """The plugin can emit up to 7 fractional digits; Python only supports 6."""
        result = _parse_playback_datetime("2026-01-15 20:30:00.1234567")
        assert result == datetime(2026, 1, 15, 20, 30, 0, 123456)


class TestJellyfinAPI:
    """Test JellyfinAPI methods."""

    def test_user_id_normalized_dashless(self):
        api = JellyfinAPI("https://jellyfin.example.com", "key", "00000000-0000-4000-8000-000000000000")
        assert api.user_id == "00000000000040008000000000000000"

    def test_user_id_none_when_not_provided(self):
        api = JellyfinAPI("https://jellyfin.example.com", "key")
        assert api.user_id is None

    def test_invalid_user_id_raises(self):
        with pytest.raises(ValueError, match="does not look like a GUID"):
            JellyfinAPI("https://jellyfin.example.com", "key", "'; DROP TABLE PlaybackActivity; --")

    def test_fetch_history_builds_rows(self):
        api = JellyfinAPI("https://jellyfin.example.com", "key")
        response_mock = Mock()
        response_mock.json.return_value = {
            "colums": ["rowid", "DateCreated", "UserId", "ItemId", "ItemType", "ItemName", "PlayDuration"],
            "results": [
                [1, "2026-01-15 20:30:00", "abc123", "item1", "Movie", "The Matrix", 700],
            ],
            "message": "",
        }

        with patch("requests.post", return_value=response_mock) as mock_post:
            rows = api.fetch_history(datetime(2026, 1, 15), 600)

        assert len(rows) == 1
        assert rows[0]["ItemId"] == "item1"
        assert rows[0]["PlayDuration"] == 700
        assert rows[0]["DateCreated"] == datetime(2026, 1, 15, 20, 30, 0)
        sql = mock_post.call_args.kwargs["json"]["CustomQueryString"]
        assert "PlaybackActivity" in sql
        assert "PlayDuration >= 600" in sql

    def test_fetch_history_filters_by_user_id(self):
        api = JellyfinAPI("https://jellyfin.example.com", "key", user_id="00000000-0000-4000-8000-000000000000")
        response_mock = Mock()
        response_mock.json.return_value = {"colums": [], "results": [], "message": ""}

        with patch("requests.post", return_value=response_mock) as mock_post:
            api.fetch_history(datetime(2026, 1, 15), 600)

        sql = mock_post.call_args.kwargs["json"]["CustomQueryString"]
        assert "UserId = '00000000000040008000000000000000'" in sql

    def test_get_item_details_caches(self):
        api = JellyfinAPI("https://jellyfin.example.com", "key")
        response_mock = Mock()
        response_mock.json.return_value = {"Name": "The Matrix"}
        user_id = "00000000000040008000000000000000"

        with patch("requests.get", return_value=response_mock) as mock_get:
            first = api.get_item_details("item1", user_id)
            api.get_item_details("item1", user_id)

        assert first == {"Name": "The Matrix"}
        assert mock_get.call_count == 1


class TestTogglAPI:
    """Test Toggl API methods."""

    def test_parse_time_with_z(self):
        """Test parsing time string with Z suffix."""
        time_str = "2025-01-01T12:00:00Z"
        result = TogglAPI.parse_time(time_str)
        assert isinstance(result, datetime)
        assert result.year == 2025

    def test_normalize_timestamp(self):
        """Test timestamp normalization."""
        timestamp = "2025-01-01T12:00:00.123456Z"
        result = TogglAPI.normalize_timestamp(timestamp)
        assert result.microsecond == 0

    def test_entry_exists_with_rate_limit(self):
        """Test entry_exists returns True when rate limited."""
        api = TogglAPI("token", 123, 456, ["tag"])

        with patch.object(api, "get_cached_entries", return_value=None):
            result = api.entry_exists("Test", "2025-01-01T12:00:00Z", "2025-01-01T13:00:00Z")
            assert result is True


class TestTogglGetCachedEntries:
    """Test TogglAPI.get_cached_entries() caching and rate-limit behaviour."""

    def _make_api(self):
        return TogglAPI("token", 123, 456, ["jellyfin"])

    def test_fetches_on_first_call(self):
        api = self._make_api()
        entries = [{"id": 1}]
        response_mock = Mock()
        response_mock.json.return_value = entries

        with patch("requests.get", return_value=response_mock):
            result = api.get_cached_entries()

        assert result == entries

    def test_uses_cache_on_second_call(self):
        api = self._make_api()
        response_mock = Mock()
        response_mock.json.return_value = []

        with patch("requests.get", return_value=response_mock) as mock_get:
            api.get_cached_entries()
            api.get_cached_entries()

        assert mock_get.call_count == 1

    def test_force_refresh_bypasses_cache(self):
        api = self._make_api()
        response_mock = Mock()
        response_mock.json.return_value = []

        with patch("requests.get", return_value=response_mock) as mock_get:
            api.get_cached_entries()
            api.get_cached_entries(force_refresh=True)

        assert mock_get.call_count == 2

    def test_rate_limit_returns_none_and_sets_flag(self):
        """402 response sets _rate_limited and returns None."""
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 402
        http_error = requests.exceptions.HTTPError(response=error_response)

        with patch("requests.get") as mock_get:
            mock_get.return_value.raise_for_status.side_effect = http_error
            result = api.get_cached_entries()

        assert result is None
        assert api._rate_limited is True

    def test_start_date_uses_reports_api(self):
        """A start_date arg routes through the Reports API, not /me/time_entries."""
        api = self._make_api()
        with patch.object(api, "_fetch_reports_entries", return_value=[{"id": 1}]) as mock_fetch:
            with patch("requests.get") as mock_get:
                result = api.get_cached_entries(start_date="2025-01-01")

        assert result == [{"id": 1}]
        mock_fetch.assert_called_once()
        mock_get.assert_not_called()


class TestTogglReportsAPI:
    """Test TogglAPI._fetch_reports_page() / _fetch_reports_entries()."""

    def _make_api(self):
        return TogglAPI("token", 123, 456, ["jellyfin"])

    def _tags_response(self, tags=None):
        resp = Mock()
        resp.json.return_value = tags or [{"id": 1, "name": "jellyfin"}, {"id": 2, "name": "watching"}]
        return resp

    def _reports_response(self, rows, next_headers=None):
        resp = Mock()
        resp.json.return_value = rows
        resp.headers = next_headers or {}
        return resp

    def test_flattens_grouped_rows_and_resolves_tag_names(self):
        api = self._make_api()
        rows = [
            {
                "project_id": 456,
                "description": "🎞️ Movie X (2025)",
                "tag_ids": [1, 2],
                "time_entries": [{"id": 999, "start": "2025-01-01T10:00:00Z", "stop": "2025-01-01T12:00:00Z"}],
            }
        ]

        with patch("requests.get", return_value=self._tags_response()):
            with patch("requests.post", return_value=self._reports_response(rows)):
                entries = api._fetch_reports_entries("2025-01-01", "2025-01-02")

        assert entries == [
            {
                "id": 999,
                "project_id": 456,
                "start": "2025-01-01T10:00:00Z",
                "stop": "2025-01-01T12:00:00Z",
                "description": "🎞️ Movie X (2025)",
                "tags": ["jellyfin", "watching"],
                "wid": 123,
            }
        ]

    def test_paginates_using_next_headers(self):
        api = self._make_api()
        row1 = [
            {
                "project_id": 456,
                "description": "A",
                "tag_ids": [],
                "time_entries": [{"id": 1, "start": "2025-01-01T10:00:00Z", "stop": "2025-01-01T11:00:00Z"}],
            }
        ]
        row2 = [
            {
                "project_id": 456,
                "description": "B",
                "tag_ids": [],
                "time_entries": [{"id": 2, "start": "2025-01-02T10:00:00Z", "stop": "2025-01-02T11:00:00Z"}],
            }
        ]
        page1 = self._reports_response(
            row1, {"x-next-id": "2", "x-next-row-number": "2", "x-next-timestamp": "1700000000"}
        )
        page2 = self._reports_response(row2)

        with patch("requests.get", return_value=self._tags_response()):
            with patch("requests.post", side_effect=[page1, page2]) as mock_post:
                entries = api._fetch_reports_entries("2025-01-01", "2025-01-03")

        assert [e["id"] for e in entries] == [1, 2]
        assert mock_post.call_count == 2
        second_call_body = mock_post.call_args_list[1].kwargs["json"]
        assert second_call_body["first_id"] == 2
        assert second_call_body["first_row_number"] == 2
        assert second_call_body["first_timestamp"] == 1700000000

    def test_retries_on_402_then_succeeds(self):
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 402
        error_response.headers = {}
        http_error = requests.exceptions.HTTPError(response=error_response)

        rate_limited = Mock()
        rate_limited.raise_for_status.side_effect = http_error
        success = self._reports_response([])

        with patch("requests.get", return_value=self._tags_response()):
            with patch("requests.post", side_effect=[rate_limited, success]):
                with patch("time.sleep") as mock_sleep:
                    entries = api._fetch_reports_entries("2025-01-01", "2025-01-02")

        assert entries == []
        mock_sleep.assert_called_once()

    def test_tag_lookup_cached_across_calls(self):
        api = self._make_api()
        with patch("requests.get", return_value=self._tags_response()) as mock_get:
            with patch("requests.post", return_value=self._reports_response([])):
                api._fetch_reports_entries("2025-01-01", "2025-01-02")
                api._fetch_reports_entries("2025-02-01", "2025-02-02")

        assert mock_get.call_count == 1


class TestTogglRemoveDuplicatesReportsAPI:
    """Test that remove_duplicates() fetches via the Reports API and filters by project."""

    def _make_api(self):
        return TogglAPI("token", 123, 456, ["jellyfin"])

    def test_filters_to_configured_project(self):
        api = self._make_api()
        entries = [
            {"id": 1, "project_id": 456, "description": "A", "start": "2025-06-01T10:00:00Z", "stop": None},
            {"id": 2, "project_id": 999, "description": "B", "start": "2025-06-01T10:00:00Z", "stop": None},
        ]

        with patch.object(api, "_fetch_reports_entries", return_value=entries) as mock_fetch:
            with patch("requests.delete") as mock_delete:
                api.remove_duplicates()

        mock_fetch.assert_called_once()
        mock_delete.assert_not_called()  # no duplicates among the one project-456 entry

    def test_402_during_fetch_skips_gracefully(self):
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 402
        http_error = requests.exceptions.HTTPError(response=error_response)

        with patch.object(api, "_fetch_reports_entries", side_effect=http_error):
            api.remove_duplicates()  # must not raise

    def test_non_402_error_during_fetch_reraises(self):
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 500
        http_error = requests.exceptions.HTTPError(response=error_response)

        with patch.object(api, "_fetch_reports_entries", side_effect=http_error):
            with pytest.raises(requests.exceptions.HTTPError):
                api.remove_duplicates()


class TestTogglFindExistingEntry:
    """Test TogglAPI.find_existing_entry() matching logic."""

    def _make_api(self):
        return TogglAPI("token", 123, 456, ["jellyfin"])

    def _sample_entry(self, **overrides):
        base = {
            "description": "🎞️ The Matrix (1999)",
            "start": "2025-01-01T10:00:00Z",
            "stop": "2025-01-01T12:00:00Z",
            "project_id": 456,
            "tags": ["jellyfin"],
            "wid": 123,
        }
        base.update(overrides)
        return base

    def test_finds_matching_entry(self):
        api = self._make_api()
        entry = self._sample_entry()
        api._cached_entries = [entry]
        api._cache_timestamp = time.time()

        result = api.find_existing_entry("🎞️ The Matrix (1999)", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")
        assert result == entry

    def test_no_match_on_different_description(self):
        api = self._make_api()
        api._cached_entries = [self._sample_entry()]
        api._cache_timestamp = time.time()

        result = api.find_existing_entry("Different Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")
        assert result is None

    def test_no_match_on_different_times(self):
        api = self._make_api()
        api._cached_entries = [self._sample_entry()]
        api._cache_timestamp = time.time()

        result = api.find_existing_entry("🎞️ The Matrix (1999)", "2025-01-01T09:00:00Z", "2025-01-01T11:00:00Z")
        assert result is None

    def test_skips_entry_without_stop(self):
        """Entries with no stop time (running timers) are ignored."""
        api = self._make_api()
        entry = self._sample_entry()
        del entry["stop"]
        api._cached_entries = [entry]
        api._cache_timestamp = time.time()

        result = api.find_existing_entry("🎞️ The Matrix (1999)", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")
        assert result is None

    def test_no_match_on_different_project(self):
        api = self._make_api()
        api._cached_entries = [self._sample_entry(project_id=999)]
        api._cache_timestamp = time.time()

        result = api.find_existing_entry("🎞️ The Matrix (1999)", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")
        assert result is None


class TestTogglCreateEntry:
    """Test TogglAPI.create_entry()."""

    def _make_api(self):
        api = TogglAPI("token", 123, 456, ["jellyfin"])
        api._cached_entries = []
        api._cache_timestamp = time.time()
        return api

    def test_create_entry_success(self):
        api = self._make_api()
        response_mock = Mock()
        response_mock.json.return_value = {"id": 999}

        with patch("requests.post", return_value=response_mock):
            entry_id = api.create_entry("Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        assert entry_id == 999
        assert api._cached_entries is None  # cache invalidated after creation

    def test_create_entry_skips_when_rate_limited(self):
        api = self._make_api()
        with patch.object(api, "get_cached_entries", return_value=None):
            with patch("requests.post") as mock_post:
                result = api.create_entry("Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        mock_post.assert_not_called()
        assert result is None

    def test_create_entry_skips_existing(self):
        """If the entry already exists, skip the POST and return its id."""
        api = self._make_api()
        existing = {
            "id": 42,
            "description": "Test Movie",
            "start": "2025-01-01T10:00:00Z",
            "stop": "2025-01-01T12:00:00Z",
            "project_id": 456,
            "tags": ["jellyfin"],
            "wid": 123,
        }
        api._cached_entries = [existing]

        with patch("requests.post") as mock_post:
            result = api.create_entry("Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        mock_post.assert_not_called()
        assert result == 42

    def test_create_entry_402_retries_then_raises_after_max_attempts(self):
        """Persistent 402s are retried with backoff before finally giving up."""
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 402
        error_response.headers = {}
        http_error = requests.exceptions.HTTPError(response=error_response)

        with patch("requests.post") as mock_post:
            mock_post.return_value.raise_for_status.side_effect = http_error
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(requests.exceptions.HTTPError):
                    api.create_entry("Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        assert api._rate_limited is True
        assert mock_post.call_count == api.RATE_LIMIT_MAX_RETRIES + 1
        assert mock_sleep.call_count == api.RATE_LIMIT_MAX_RETRIES

    def test_create_entry_402_then_recovers(self):
        """A transient 402 followed by success returns the created entry's id."""
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 402
        error_response.headers = {}
        http_error = requests.exceptions.HTTPError(response=error_response)

        rate_limited_response = Mock()
        rate_limited_response.raise_for_status.side_effect = http_error
        success_response = Mock()
        success_response.json.return_value = {"id": 999}

        with patch("requests.post", side_effect=[rate_limited_response, success_response]):
            with patch("time.sleep") as mock_sleep:
                entry_id = api.create_entry("Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        assert entry_id == 999
        assert api._rate_limited is False
        mock_sleep.assert_called_once_with(api.RATE_LIMIT_RETRY_DELAY_SECONDS)

    def test_create_entry_402_honors_toggl_quota_reset_header(self):
        """When Toggl sends X-Toggl-Quota-Resets-In, wait that long instead of the fixed delay."""
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 402
        error_response.headers = {"x-toggl-quota-resets-in": "120"}
        http_error = requests.exceptions.HTTPError(response=error_response)

        rate_limited_response = Mock()
        rate_limited_response.raise_for_status.side_effect = http_error
        success_response = Mock()
        success_response.json.return_value = {"id": 999}

        with patch("requests.post", side_effect=[rate_limited_response, success_response]):
            with patch("time.sleep") as mock_sleep:
                entry_id = api.create_entry("Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        assert entry_id == 999
        mock_sleep.assert_called_once_with(120 + api.RATE_LIMIT_RETRY_BUFFER_SECONDS)


class TestTogglUpdateEntry:
    """Test TogglAPI.update_entry()."""

    def _make_api(self):
        return TogglAPI("token", 123, 456, ["jellyfin"])

    def test_update_entry_success(self):
        api = self._make_api()
        response_mock = Mock()
        response_mock.json.return_value = {"id": 42}

        with patch("requests.put", return_value=response_mock):
            result = api.update_entry(42, "Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        assert result == 42
        assert api._cached_entries is None  # cache invalidated

    def test_update_entry_404_returns_none(self):
        """404 means the entry was deleted in Toggl; return None gracefully."""
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 404
        http_error = requests.exceptions.HTTPError(response=error_response)

        with patch("requests.put") as mock_put:
            mock_put.return_value.raise_for_status.side_effect = http_error
            result = api.update_entry(42, "Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        assert result is None

    def test_update_entry_402_retries_then_raises_after_max_attempts(self):
        """Persistent 402s are retried with backoff before finally giving up."""
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 402
        error_response.headers = {}
        http_error = requests.exceptions.HTTPError(response=error_response)

        with patch("requests.put") as mock_put:
            mock_put.return_value.raise_for_status.side_effect = http_error
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(requests.exceptions.HTTPError):
                    api.update_entry(42, "Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        assert api._rate_limited is True
        assert mock_put.call_count == api.RATE_LIMIT_MAX_RETRIES + 1
        assert mock_sleep.call_count == api.RATE_LIMIT_MAX_RETRIES


class TestTogglRemoveDuplicates:
    """Test TogglAPI.remove_duplicates()'s near-duplicate (second) pass."""

    def _make_api(self):
        return TogglAPI("token", 123, 456, ["trakt"])

    @staticmethod
    def _iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _run(self, api, entries):
        with patch.object(api, "_fetch_reports_entries", return_value=entries) as mock_fetch:
            with patch("requests.delete") as mock_delete:
                api.remove_duplicates()
        return mock_fetch, mock_delete

    def test_collapses_sessions_split_across_days(self):
        """A movie paused overnight and finished the next day is the same watch-through."""
        api = self._make_api()
        now = datetime.now()
        entries = [
            {
                "id": 1,
                "project_id": 456,
                "description": "🎞️ Movie X (2025)",
                "start": self._iso(now - timedelta(hours=20)),
                "stop": self._iso(now - timedelta(hours=19)),
            },
            {
                "id": 2,
                "project_id": 456,
                "description": "🎞️ Movie X (2025)",
                "start": self._iso(now - timedelta(hours=1)),
                "stop": self._iso(now),
            },
        ]

        _, mock_delete = self._run(api, entries)

        mock_delete.assert_called_once()
        assert mock_delete.call_args.args[0].endswith("/time_entries/1")

    def test_keeps_entries_more_than_30_days_apart(self):
        """A repeat of the same title more than 30 days later is treated as a real rewatch."""
        api = self._make_api()
        now = datetime.now()
        entries = [
            {
                "id": 1,
                "project_id": 456,
                "description": "🎞️ Movie X (2025)",
                "start": self._iso(now - timedelta(days=45)),
                "stop": self._iso(now - timedelta(days=45) + timedelta(hours=2)),
            },
            {
                "id": 2,
                "project_id": 456,
                "description": "🎞️ Movie X (2025)",
                "start": self._iso(now - timedelta(hours=2)),
                "stop": self._iso(now),
            },
        ]

        _, mock_delete = self._run(api, entries)

        mock_delete.assert_not_called()

    def test_collapses_near_instant_duplicate(self):
        """Two entries re-recording the same session seconds apart are true duplicates."""
        api = self._make_api()
        now = datetime.now()
        entries = [
            {
                "id": 1,
                "project_id": 456,
                "description": "🎞️ Movie X (2025)",
                "start": self._iso(now - timedelta(minutes=2)),
                "stop": self._iso(now - timedelta(minutes=1)),
            },
            {
                "id": 2,
                "project_id": 456,
                "description": "🎞️ Movie X (2025)",
                "start": self._iso(now - timedelta(minutes=1, seconds=30)),
                "stop": self._iso(now),
            },
        ]

        _, mock_delete = self._run(api, entries)

        mock_delete.assert_called_once()
        assert mock_delete.call_args.args[0].endswith("/time_entries/1")

    def test_collapses_overlapping_entries_even_beyond_the_30_day_window(self):
        """Overlapping time ranges are always a true duplicate, even past the 30-day start-gap window."""
        api = self._make_api()
        now = datetime.now()
        entries = [
            {
                "id": 1,
                "project_id": 456,
                "description": "🎞️ Movie X (2025)",
                "start": self._iso(now - timedelta(days=80)),
                "stop": self._iso(now - timedelta(days=20)),
            },
            {
                "id": 2,
                "project_id": 456,
                "description": "🎞️ Movie X (2025)",
                "start": self._iso(now - timedelta(days=40)),
                "stop": self._iso(now),
            },
        ]

        _, mock_delete = self._run(api, entries)

        mock_delete.assert_called_once()
        assert mock_delete.call_args.args[0].endswith("/time_entries/1")


class TestBuildTitle:
    """Test sync.build_title() for movies and episodes."""

    def test_movie_title(self):
        from sync import build_title

        row = {"ItemType": "Movie", "ItemName": "The Matrix"}
        item = {"Name": "The Matrix", "ProductionYear": 1999}
        assert build_title(row, item) == "🎞️ The Matrix (1999)"

    def test_episode_title(self):
        from sync import build_title

        row = {"ItemType": "Episode", "ItemName": "Pilot"}
        item = {"SeriesName": "Breaking Bad", "ParentIndexNumber": 1, "IndexNumber": 1, "Name": "Pilot"}
        assert build_title(row, item) == "📺 Breaking Bad - S01E01 - Pilot"

    def test_movie_missing_year_falls_back(self):
        from sync import build_title

        row = {"ItemType": "Movie", "ItemName": "Unknown Film"}
        item = {"Name": "Unknown Film"}
        assert build_title(row, item) == "🎞️ Unknown Film (N/A)"


class TestSyncRows:
    """Test sync.sync_rows()'s graceful stop on rate limits and network errors."""

    def _make_toggl(self):
        api = TogglAPI("token", 123, 456, ["jellyfin"])
        api._cached_entries = []
        api._cache_timestamp = time.time()
        return api

    def _make_jellyfin(self, item):
        api = JellyfinAPI("https://jellyfin.example.com", "key")
        api._item_cache = {}
        api.get_item_details = Mock(return_value=item)
        return api

    def _row(self):
        return {
            "rowid": 1,
            "DateCreated": datetime(2025, 1, 1, 10, 0, 0),
            "UserId": "abc123",
            "ItemId": "movie1",
            "ItemType": "Movie",
            "ItemName": "The Matrix",
            "PlayDuration": 7200,
        }

    def test_processes_all_rows_on_success(self, tmp_path):
        from sync import sync_rows

        toggl = self._make_toggl()
        jellyfin = self._make_jellyfin({"Name": "The Matrix", "ProductionYear": 1999})
        state_file = str(tmp_path / "state.json")

        with patch.object(toggl, "create_entry", return_value=111) as mock_create:
            sync_rows([self._row()], jellyfin, toggl, {}, state_file)

        mock_create.assert_called_once()

    def test_stops_gracefully_on_402(self, tmp_path):
        from sync import sync_rows

        toggl = self._make_toggl()
        jellyfin = self._make_jellyfin({"Name": "The Matrix", "ProductionYear": 1999})
        state_file = str(tmp_path / "state.json")
        error_response = Mock()
        error_response.status_code = 402
        http_error = requests.exceptions.HTTPError(response=error_response)

        with patch.object(toggl, "create_entry", side_effect=http_error):
            sync_rows([self._row()], jellyfin, toggl, {}, state_file)  # must not raise

    def test_reraises_non_402_http_error(self, tmp_path):
        from sync import sync_rows

        toggl = self._make_toggl()
        jellyfin = self._make_jellyfin({"Name": "The Matrix", "ProductionYear": 1999})
        state_file = str(tmp_path / "state.json")
        error_response = Mock()
        error_response.status_code = 500
        http_error = requests.exceptions.HTTPError(response=error_response)

        with patch.object(toggl, "create_entry", side_effect=http_error):
            with pytest.raises(requests.exceptions.HTTPError):
                sync_rows([self._row()], jellyfin, toggl, {}, state_file)

    def test_stops_gracefully_on_network_error(self, tmp_path):
        """A transient network error (timeout, connection reset) must not crash the run."""
        from sync import sync_rows

        toggl = self._make_toggl()
        jellyfin = self._make_jellyfin({"Name": "The Matrix", "ProductionYear": 1999})
        state_file = str(tmp_path / "state.json")

        with patch.object(toggl, "create_entry", side_effect=requests.exceptions.ReadTimeout("timed out")):
            sync_rows([self._row()], jellyfin, toggl, {}, state_file)  # must not raise


class TestSyncProcessPlaybackRow:
    """Test sync.process_playback_row() for movies and episodes."""

    def _make_toggl(self):
        api = TogglAPI("token", 123, 456, ["jellyfin"])
        api._cached_entries = []
        api._cache_timestamp = time.time()
        return api

    def _make_jellyfin(self, item):
        api = JellyfinAPI("https://jellyfin.example.com", "key")
        api._item_cache = {}
        api.get_item_details = Mock(return_value=item)
        return api

    def _movie_row(self):
        return {
            "rowid": 1,
            "DateCreated": datetime(2025, 1, 1, 10, 0, 0),
            "UserId": "abc123",
            "ItemId": "movie1",
            "ItemType": "Movie",
            "ItemName": "The Matrix",
            "PlayDuration": 7200,
        }

    def _episode_row(self):
        return {
            "rowid": 2,
            "DateCreated": datetime(2025, 1, 1, 10, 0, 0),
            "UserId": "abc123",
            "ItemId": "ep1",
            "ItemType": "Episode",
            "ItemName": "Pilot",
            "PlayDuration": 1740,
        }

    def test_creates_movie_entry_and_saves_state(self, tmp_path):
        from sync import process_playback_row

        toggl = self._make_toggl()
        jellyfin = self._make_jellyfin({"Name": "The Matrix", "ProductionYear": 1999})
        state_file = str(tmp_path / "state.json")
        sync_state = {}

        with patch.object(toggl, "create_entry", return_value=999) as mock_create:
            process_playback_row(self._movie_row(), jellyfin, toggl, sync_state, state_file)

        mock_create.assert_called_once()
        assert "The Matrix" in mock_create.call_args.kwargs["description"]
        state_key = "Movie:movie1:2025-01-01T10:00:00"
        assert sync_state[state_key] == 999

    def test_creates_episode_entry_and_saves_state(self, tmp_path):
        from sync import process_playback_row

        toggl = self._make_toggl()
        jellyfin = self._make_jellyfin(
            {"SeriesName": "Breaking Bad", "ParentIndexNumber": 1, "IndexNumber": 1, "Name": "Pilot"}
        )
        state_file = str(tmp_path / "state.json")
        sync_state = {}

        with patch.object(toggl, "create_entry", return_value=888):
            process_playback_row(self._episode_row(), jellyfin, toggl, sync_state, state_file)

        state_key = "Episode:ep1:2025-01-01T10:00:00"
        assert sync_state[state_key] == 888

    def test_end_time_derived_from_play_duration(self, tmp_path):
        """end = DateCreated + PlayDuration seconds, not full media runtime."""
        from sync import process_playback_row

        toggl = self._make_toggl()
        jellyfin = self._make_jellyfin({"Name": "The Matrix", "ProductionYear": 1999})
        state_file = str(tmp_path / "state.json")
        sync_state = {}

        with patch.object(toggl, "create_entry", return_value=999) as mock_create:
            process_playback_row(self._movie_row(), jellyfin, toggl, sync_state, state_file)

        expected_end = (datetime(2025, 1, 1, 10, 0, 0) + timedelta(seconds=7200)).isoformat() + "Z"
        assert mock_create.call_args.kwargs["end_time"] == expected_end

    def test_updates_existing_state_entry(self, tmp_path):
        """If state already has an id for this session, update_entry is called instead."""
        from sync import process_playback_row

        toggl = self._make_toggl()
        jellyfin = self._make_jellyfin({"Name": "The Matrix", "ProductionYear": 1999})
        state_file = str(tmp_path / "state.json")
        state_key = "Movie:movie1:2025-01-01T10:00:00"
        sync_state = {state_key: 42}

        with patch.object(toggl, "update_entry", return_value=42) as mock_update:
            with patch.object(toggl, "create_entry") as mock_create:
                process_playback_row(self._movie_row(), jellyfin, toggl, sync_state, state_file)

        mock_update.assert_called_once()
        mock_create.assert_not_called()
        assert sync_state[state_key] == 42

    def test_recreates_when_update_returns_none(self, tmp_path):
        """If update returns None (entry deleted in Toggl), a new entry is created."""
        from sync import process_playback_row

        toggl = self._make_toggl()
        jellyfin = self._make_jellyfin({"Name": "The Matrix", "ProductionYear": 1999})
        state_file = str(tmp_path / "state.json")
        state_key = "Movie:movie1:2025-01-01T10:00:00"
        sync_state = {state_key: 42}

        with patch.object(toggl, "update_entry", return_value=None):
            with patch.object(toggl, "create_entry", return_value=999) as mock_create:
                process_playback_row(self._movie_row(), jellyfin, toggl, sync_state, state_file)

        mock_create.assert_called_once()
        assert sync_state[state_key] == 999

    def test_state_file_persisted_to_disk(self, tmp_path):
        """State is written to disk after each row so it survives crashes."""
        from sync import process_playback_row

        toggl = self._make_toggl()
        jellyfin = self._make_jellyfin({"Name": "The Matrix", "ProductionYear": 1999})
        state_file = str(tmp_path / "state.json")
        sync_state = {}

        with patch.object(toggl, "create_entry", return_value=777):
            process_playback_row(self._movie_row(), jellyfin, toggl, sync_state, state_file)

        on_disk = json.loads((tmp_path / "state.json").read_text())
        state_key = "Movie:movie1:2025-01-01T10:00:00"
        assert on_disk[state_key] == 777


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
