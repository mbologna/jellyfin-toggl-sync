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

    def test_create_entry_402_sets_rate_limited_and_raises(self):
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 402
        http_error = requests.exceptions.HTTPError(response=error_response)

        with patch("requests.post") as mock_post:
            mock_post.return_value.raise_for_status.side_effect = http_error
            with pytest.raises(requests.exceptions.HTTPError):
                api.create_entry("Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        assert api._rate_limited is True


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

    def test_update_entry_402_sets_rate_limited_and_raises(self):
        api = self._make_api()
        error_response = Mock()
        error_response.status_code = 402
        http_error = requests.exceptions.HTTPError(response=error_response)

        with patch("requests.put") as mock_put:
            mock_put.return_value.raise_for_status.side_effect = http_error
            with pytest.raises(requests.exceptions.HTTPError):
                api.update_entry(42, "Test Movie", "2025-01-01T10:00:00Z", "2025-01-01T12:00:00Z")

        assert api._rate_limited is True


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
