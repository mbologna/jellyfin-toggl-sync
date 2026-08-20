"""End-to-end tests for jellyfin-toggl-sync.

These tests use real API credentials stored in environment variables.
They are only run when E2E_TEST_ENABLED=true.

Setup Instructions:
1. Create a Jellyfin API key: Dashboard -> Advanced -> API Keys
2. Make sure the Playback Reporting plugin is installed and has some history
3. Set environment variables:
   export E2E_JELLYFIN_URL="https://jellyfin.example.com"
   export E2E_JELLYFIN_API_KEY="your_api_key"
4. Set Toggl credentials:
   export E2E_TOGGL_API_TOKEN="your_token"
   export E2E_TOGGL_WORKSPACE_ID="123"
   export E2E_TOGGL_PROJECT_ID="456"
5. Enable E2E tests:
   export E2E_TEST_ENABLED=true
6. Run tests:
   make test-e2e

Note: E2E tests will create temporary test entries in your Toggl project.
These are tagged with `e2e-test` and should be cleaned up manually.
"""

import os
import time
from datetime import datetime, timedelta

import pytest

from jellyfin import JellyfinAPI
from toggl import TogglAPI

# Skip all E2E tests if not explicitly enabled
pytestmark = pytest.mark.skipif(
    os.getenv("E2E_TEST_ENABLED") != "true",
    reason="E2E tests only run when E2E_TEST_ENABLED=true",
)


@pytest.fixture(scope="session")
def jellyfin_api():
    """Create JellyfinAPI client with test credentials."""
    base_url = os.getenv("E2E_JELLYFIN_URL")
    api_key = os.getenv("E2E_JELLYFIN_API_KEY")

    if not base_url or not api_key:
        pytest.skip("E2E Jellyfin credentials not configured")

    return JellyfinAPI(base_url, api_key)


@pytest.fixture(scope="session")
def toggl_api():
    """Create TogglAPI client with test credentials."""
    api_token = os.getenv("E2E_TOGGL_API_TOKEN")
    workspace_id = int(os.getenv("E2E_TOGGL_WORKSPACE_ID", "0"))
    project_id = int(os.getenv("E2E_TOGGL_PROJECT_ID", "0"))

    if not api_token or not workspace_id or not project_id:
        pytest.skip("E2E Toggl credentials not configured")

    return TogglAPI(api_token, workspace_id, project_id, ["e2e-test"])


class TestJellyfinE2E:
    """End-to-end tests for Jellyfin API."""

    def test_fetch_recent_history(self, jellyfin_api):
        """Test fetching recent playback sessions from the Playback Reporting plugin."""
        since_dt = datetime.now() - timedelta(days=7)
        rows = jellyfin_api.fetch_history(since_dt, min_duration_seconds=600)

        assert isinstance(rows, list)
        print(f"\n✓ Fetched {len(rows)} playback sessions (last 7 days, >10min)")

        if rows:
            row = rows[0]
            assert "ItemId" in row
            assert "ItemType" in row
            assert "PlayDuration" in row
            assert row["PlayDuration"] >= 600
            print(f"  Sample: {row['ItemType']} {row['ItemName']} ({row['PlayDuration']}s)")

    def test_get_item_details(self, jellyfin_api):
        """Test fetching item metadata for a recent play, if any exist."""
        since_dt = datetime.now() - timedelta(days=30)
        rows = jellyfin_api.fetch_history(since_dt, min_duration_seconds=600)

        if not rows:
            pytest.skip("No recent playback history to test with")

        row = rows[0]
        item = jellyfin_api.get_item_details(row["ItemId"], row["UserId"])
        assert "Name" in item
        print(f"\n✓ Fetched item details: {item.get('Name')}")


class TestTogglE2E:
    """End-to-end tests for Toggl API."""

    def test_get_entries(self, toggl_api):
        """Test fetching Toggl entries."""
        entries = toggl_api.get_cached_entries()

        if toggl_api._rate_limited:
            pytest.skip("Toggl API rate limited")

        assert entries is not None
        assert isinstance(entries, list)
        print(f"\n✓ Fetched {len(entries)} Toggl entries")

    def test_create_and_verify_entry(self, toggl_api):
        """Test creating a time entry and verifying it exists."""
        timestamp_suffix = int(datetime.now().timestamp())
        description = f"E2E Test {timestamp_suffix}"
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=15)

        start_iso = start_time.isoformat() + "Z"
        end_iso = end_time.isoformat() + "Z"

        exists_before = toggl_api.entry_exists(description, start_iso, end_iso)
        assert not exists_before, "Entry should not exist before creation"

        toggl_api.create_entry(description, start_iso, end_iso)

        if toggl_api._rate_limited:
            pytest.skip("Toggl API rate limited during test")

        print(f"\n✓ Created test entry: {description}")

        time.sleep(2)  # Brief wait for API consistency
        toggl_api.get_cached_entries(force_refresh=True)
        exists_after = toggl_api.entry_exists(description, start_iso, end_iso)
        assert exists_after, "Entry should exist after creation"

        print("✓ Entry verified in Toggl")
        print("⚠ Clean up test entries manually or they'll accumulate")

    def test_remove_duplicates(self, toggl_api):
        """Test deduplication (should run without errors)."""
        toggl_api.remove_duplicates()

        if toggl_api._rate_limited:
            pytest.skip("Toggl API rate limited during deduplication")

        print("\n✓ Toggl deduplication completed successfully")


class TestIntegrationE2E:
    """Full integration tests."""

    def test_sync_workflow(self, jellyfin_api, toggl_api):
        """Test a simplified sync workflow."""
        since_dt = datetime.now() - timedelta(days=2)
        rows = jellyfin_api.fetch_history(since_dt, min_duration_seconds=600)

        print(f"\n✓ Fetched {len(rows)} playback sessions (last 2 days)")

        if not rows:
            pytest.skip("No recent Jellyfin playback history to test with")

        start_date_str = since_dt.strftime("%Y-%m-%d")
        toggl_entries = toggl_api.get_cached_entries(start_date=start_date_str, force_refresh=True)

        if toggl_api._rate_limited:
            pytest.skip("Toggl API rate limited")

        print(f"✓ Fetched {len(toggl_entries or [])} Toggl entries")

        tested = 0
        for row in rows[:5]:  # Test only first 5 to avoid rate limits
            item = jellyfin_api.get_item_details(row["ItemId"], row["UserId"])
            if row["ItemType"] == "Episode":
                title = (
                    f"📺 {item.get('SeriesName', 'Unknown')} - "
                    f"S{item.get('ParentIndexNumber', 0):02}E{item.get('IndexNumber', 0):02} - "
                    f"{item.get('Name', row['ItemName'])}"
                )
            else:
                title = f"🎞️ {item.get('Name', row['ItemName'])} ({item.get('ProductionYear', 'N/A')})"

            start_time = row["DateCreated"]
            end_time = start_time + timedelta(seconds=row["PlayDuration"])

            exists = toggl_api.entry_exists(
                title,
                start_time.isoformat() + "Z",
                end_time.isoformat() + "Z",
            )

            status = "exists" if exists else "would be created"
            print(f"  {title[:60]}: {status}")
            tested += 1

        print(f"\n✓ Checked {tested} entries successfully")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
