"""Main sync script for jellyfin-toggl-sync."""

import os
import sys
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

from jellyfin import JellyfinAPI
from toggl import TogglAPI
from utils import check_required_env_variables, load_json_file, save_json_file, timestamp

# Load environment variables
load_dotenv()

# Configuration
JELLYFIN_URL = os.getenv("JELLYFIN_URL")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY")
JELLYFIN_USER_ID = os.getenv("JELLYFIN_USER_ID") or None
JELLYFIN_HISTORY_HOURS = int(os.getenv("JELLYFIN_HISTORY_HOURS", 24))
JELLYFIN_MIN_DURATION_MINUTES = int(os.getenv("JELLYFIN_MIN_DURATION_MINUTES", 10))

TOGGL_API_TOKEN = os.getenv("TOGGL_API_TOKEN")
TOGGL_WORKSPACE_ID = int(v) if (v := os.getenv("TOGGL_WORKSPACE_ID")) is not None else None
TOGGL_PROJECT_ID = int(v) if (v := os.getenv("TOGGL_PROJECT_ID")) is not None else None
TOGGL_TAGS = [tag.strip() for tag in os.getenv("TOGGL_TAGS", "").split(",") if tag.strip()]

# Sync state file — maps Jellyfin playback session IDs to Toggl entry IDs to enable update-in-place
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")


def build_title(row, item):
    """Build a Trakt-style title from a PlaybackActivity row + /Items metadata."""
    if row["ItemType"] == "Episode":
        return (
            f"📺 {item.get('SeriesName', 'Unknown')} - "
            f"S{item.get('ParentIndexNumber', 0):02}E{item.get('IndexNumber', 0):02} - "
            f"{item.get('Name', row['ItemName'])}"
        )
    return f"🎞️ {item.get('Name', row['ItemName'])} ({item.get('ProductionYear', 'N/A')})"


def process_playback_row(row, jellyfin_api, toggl_api, sync_state, state_file):
    """Process a single playback session: update existing Toggl entry or create a new one."""
    item = jellyfin_api.get_item_details(row["ItemId"], row["UserId"])
    title = build_title(row, item)

    start_time = row["DateCreated"]
    end_time = start_time + timedelta(seconds=row["PlayDuration"])
    start_iso = start_time.isoformat() + "Z"
    end_iso = end_time.isoformat() + "Z"

    state_key = f"{row['ItemType']}:{row['ItemId']}:{start_time.isoformat()}"

    existing_toggl_id = sync_state.get(state_key)
    new_id = None

    if existing_toggl_id:
        new_id = toggl_api.update_entry(existing_toggl_id, title, start_iso, end_iso)
        if new_id is None:
            # Entry was deleted from Toggl; fall through to create
            print(f"[{timestamp()}] State entry gone from Toggl, recreating: {title}")

    if new_id is None:
        new_id = toggl_api.create_entry(description=title, start_time=start_iso, end_time=end_iso)

    if new_id and sync_state.get(state_key) != new_id:
        sync_state[state_key] = new_id
        save_json_file(state_file, sync_state)


def main():
    """Main sync process."""
    print(f"[{timestamp()}] ===== Starting jellyfin-toggl-sync =====")
    sys.stdout.flush()

    check_required_env_variables()

    jellyfin = JellyfinAPI(JELLYFIN_URL, JELLYFIN_API_KEY, JELLYFIN_USER_ID)
    toggl = TogglAPI(TOGGL_API_TOKEN, TOGGL_WORKSPACE_ID, TOGGL_PROJECT_ID, TOGGL_TAGS)

    # Step 1: Remove Toggl duplicates
    print(f"\n[{timestamp()}] === Step 1: Removing Toggl Duplicates ===")
    sys.stdout.flush()
    toggl.remove_duplicates()

    # Step 2: Sync from Jellyfin to Toggl
    print(f"\n[{timestamp()}] === Step 2: Syncing Jellyfin to Toggl ===")
    since_dt = datetime.now() - timedelta(hours=JELLYFIN_HISTORY_HOURS)
    print(
        f"[{timestamp()}] Fetching Jellyfin plays since {since_dt.isoformat()} "
        f"(> {JELLYFIN_MIN_DURATION_MINUTES} min)..."
    )
    sys.stdout.flush()
    rows = jellyfin.fetch_history(since_dt, JELLYFIN_MIN_DURATION_MINUTES * 60)

    # Pre-fetch Toggl entries for the same date range to improve duplicate detection
    toggl.get_cached_entries(start_date=since_dt.strftime("%Y-%m-%d"), force_refresh=True)

    sync_state = load_json_file(SYNC_STATE_FILE) or {}

    print(f"[{timestamp()}] Processing {len(rows)} playback sessions...")
    sys.stdout.flush()
    try:
        for row in rows:
            process_playback_row(row, jellyfin, toggl, sync_state, SYNC_STATE_FILE)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 402:
            print(f"[{timestamp()}] ⚠ Sync stopped due to rate limits.")
            print(f"[{timestamp()}] Run again later to sync remaining entries.")
            sys.stdout.flush()
        else:
            raise

    print(f"\n[{timestamp()}] ===== Sync Complete =====")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
