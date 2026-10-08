"""Toggl API client for time tracking."""

import sys
import time
from datetime import datetime, timedelta

import requests

from utils import timestamp


class TogglAPI:
    """Toggl API client for time tracking."""

    BASE_URL = "https://api.track.toggl.com/api/v9"
    REPORTS_BASE_URL = "https://api.track.toggl.com/reports/api/v3"
    DEFAULT_TIMEOUT = (3.05, 10)
    CREATED_WITH = "jellyfin-toggl-sync"
    RATE_LIMIT_MAX_RETRIES = 3
    RATE_LIMIT_RETRY_DELAY_SECONDS = 30  # fallback when Toggl doesn't send a reset hint
    RATE_LIMIT_RETRY_BUFFER_SECONDS = 2

    def __init__(self, api_token, workspace_id, project_id, tags):
        self.api_token = api_token
        self.workspace_id = workspace_id
        self.project_id = project_id
        self.tags = tags
        self._cached_entries = None
        self._cache_timestamp = None
        self._cache_duration = 300  # Cache for 5 minutes
        self._cache_start_date = None
        self._rate_limited = False
        self._tag_id_to_name = None

    @classmethod
    def _rate_limit_wait_seconds(cls, response):
        """Seconds to wait before retrying a 402, honoring Toggl's quota-reset hint.

        Toggl's free plan enforces an hourly quota, not a short burst limit — a
        402 response carries an X-Toggl-Quota-Resets-In header (seconds) telling
        us exactly when it clears, which can be up to ~3600s. Fall back to the
        fixed delay if the header is missing or unparseable.
        """
        reset_in = response.headers.get("x-toggl-quota-resets-in") if response is not None else None
        if reset_in is not None:
            try:
                return int(reset_in) + cls.RATE_LIMIT_RETRY_BUFFER_SECONDS
            except ValueError:
                pass
        return cls.RATE_LIMIT_RETRY_DELAY_SECONDS

    def _tag_names(self):
        """Lazily fetch and cache the workspace's tag id -> name mapping.

        The Reports API only returns tag_ids, not names, so this is needed
        to translate its rows into the same {"tags": [names...]} shape the
        core API gives us (which find_existing_entry compares against).
        """
        if self._tag_id_to_name is None:
            response = requests.get(
                f"{self.BASE_URL}/workspaces/{self.workspace_id}/tags",
                auth=(self.api_token, "api_token"),
                timeout=self.DEFAULT_TIMEOUT,
            )
            response.raise_for_status()
            self._tag_id_to_name = {t["id"]: t["name"] for t in response.json()}
        return self._tag_id_to_name

    def _fetch_reports_page(self, start_date, end_date, cursor=None):
        """One page of the Reports API's detailed search, flattened to the
        core API's entry shape. Returns (entries, next_cursor_or_None).

        Unlike /me/time_entries (capped at ~90 days on start_date/since/before,
        all confirmed directly against the live API), this endpoint has no
        such floor — only a 366-day span per request, enforced by the caller.
        """
        body = {"start_date": start_date, "end_date": end_date, "page_size": 1000}
        if cursor:
            body.update(cursor)

        for attempt in range(self.RATE_LIMIT_MAX_RETRIES + 1):
            try:
                response = requests.post(
                    f"{self.REPORTS_BASE_URL}/workspace/{self.workspace_id}/search/time_entries",
                    json=body,
                    auth=(self.api_token, "api_token"),
                    timeout=self.DEFAULT_TIMEOUT,
                )
                response.raise_for_status()
                rows = response.json()
                tag_names = self._tag_names()
                entries = []
                for row in rows:
                    tags = [tag_names.get(tid, "") for tid in row.get("tag_ids") or []]
                    for te in row.get("time_entries", []):
                        entries.append(
                            {
                                "id": te["id"],
                                "project_id": row.get("project_id"),
                                "start": te["start"],
                                "stop": te.get("stop"),
                                "description": row.get("description", ""),
                                "tags": tags,
                                "wid": self.workspace_id,
                            }
                        )

                next_id = response.headers.get("x-next-id")
                next_cursor = None
                if next_id is not None:
                    next_cursor = {
                        "first_id": int(next_id),
                        "first_row_number": int(response.headers["x-next-row-number"]),
                        "first_timestamp": int(response.headers["x-next-timestamp"]),
                    }
                return entries, next_cursor
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 402 and attempt < self.RATE_LIMIT_MAX_RETRIES:
                    delay = self._rate_limit_wait_seconds(e.response)
                    print(
                        f"[{timestamp()}] ⚠ Rate limit reached. "
                        f"Retrying in {delay}s ({attempt + 1}/{self.RATE_LIMIT_MAX_RETRIES})..."
                    )
                    time.sleep(delay)
                    continue
                raise
        raise AssertionError("unreachable")  # loop always returns or raises

    def _fetch_reports_entries(self, start_date, end_date):
        """Fetch every entry in [start_date, end_date] (<=366 days) via the Reports API."""
        all_entries = []
        cursor = None
        while True:
            entries, cursor = self._fetch_reports_page(start_date, end_date, cursor)
            all_entries.extend(entries)
            if cursor is None:
                break
        return all_entries

    @staticmethod
    def parse_time(time_str):
        """Parse Toggl time strings to datetime."""
        if time_str.endswith("Z"):
            time_str = time_str[:-1] + "+00:00"
        return datetime.fromisoformat(time_str)

    @staticmethod
    def normalize_timestamp(timestamp_str):
        """Normalize timestamps for comparison."""
        return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00")).replace(microsecond=0)

    def get_cached_entries(self, start_date=None, force_refresh=False):
        """Get cached Toggl entries or fetch if cache is stale.

        A `start_date` from an earlier call is remembered and reused on later
        no-arg calls, so a cache expiry mid-run (e.g. after a long rate-limit
        wait) can't silently narrow dedup visibility back down to the core
        API's short default range.
        """
        now = time.time()
        if start_date:
            self._cache_start_date = start_date
        effective_start_date = start_date or self._cache_start_date

        if (
            force_refresh
            or self._cached_entries is None
            or self._cache_timestamp is None
            or now - self._cache_timestamp > self._cache_duration
        ):
            try:
                if effective_start_date:
                    end_date = datetime.now().strftime("%Y-%m-%d")
                    self._cached_entries = self._fetch_reports_entries(effective_start_date, end_date)
                else:
                    response = requests.get(
                        f"{self.BASE_URL}/me/time_entries",
                        auth=(self.api_token, "api_token"),
                        timeout=self.DEFAULT_TIMEOUT,
                    )
                    response.raise_for_status()
                    self._cached_entries = response.json()
                self._cache_timestamp = now
                self._rate_limited = False
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 402:
                    if not self._rate_limited:
                        print(f"[{timestamp()}] ⚠ Toggl rate limit reached.")
                        print(
                            f"[{timestamp()}] Cannot check for duplicates - will skip creating entries to avoid duplicates."
                        )
                        self._rate_limited = True
                    # Return None to signal rate limiting
                    return None
                else:
                    raise

        return self._cached_entries

    def find_existing_entry(self, description, start_time, end_time):
        """Find an existing entry matching description+times. Returns entry dict or None."""
        entries = self.get_cached_entries()
        if entries is None:
            return None

        start_dt = self.normalize_timestamp(start_time)
        end_dt = self.normalize_timestamp(end_time)

        for entry in entries:
            if not entry.get("stop"):
                continue
            if (
                entry.get("description") == description
                and self.normalize_timestamp(entry["start"]) == start_dt
                and self.normalize_timestamp(entry["stop"]) == end_dt
                and entry.get("project_id") == self.project_id
                and set(entry.get("tags", [])) == set(self.tags)
                and entry.get("wid") == self.workspace_id
            ):
                return entry
        return None

    def entry_exists(self, description, start_time, end_time):
        """Check if entry exists using cached data."""
        if self.get_cached_entries() is None:
            return True  # Assume exists to avoid creating duplicates when rate limited
        return self.find_existing_entry(description, start_time, end_time) is not None

    def create_entry(self, description, start_time, end_time):
        """Create a new Toggl time entry. Returns the entry ID, or None on failure."""
        if self.get_cached_entries() is None:
            print(f"[{timestamp()}] Skipped (rate limited): {description}")
            return None

        existing = self.find_existing_entry(description, start_time, end_time)
        if existing:
            print(f"[{timestamp()}] Skipped (exists): {description}")
            return existing["id"]

        data = {
            "description": description,
            "start": start_time,
            "stop": end_time,
            "created_with": self.CREATED_WITH,
            "project_id": self.project_id,
            "tags": self.tags,
            "wid": self.workspace_id,
        }

        for attempt in range(self.RATE_LIMIT_MAX_RETRIES + 1):
            try:
                response = requests.post(
                    f"{self.BASE_URL}/workspaces/{self.workspace_id}/time_entries",
                    json=data,
                    auth=(self.api_token, "api_token"),
                    timeout=self.DEFAULT_TIMEOUT,
                )
                response.raise_for_status()
                entry = response.json()
                start_dt = self.parse_time(start_time).strftime("%Y-%m-%d %H:%M")
                print(f"[{timestamp()}] ✓ Created: {description} (at {start_dt})")
                self._cached_entries = None
                return entry.get("id")
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 402:
                    if attempt < self.RATE_LIMIT_MAX_RETRIES:
                        delay = self._rate_limit_wait_seconds(e.response)
                        print(
                            f"[{timestamp()}] ⚠ Rate limit reached. "
                            f"Retrying in {delay}s ({attempt + 1}/{self.RATE_LIMIT_MAX_RETRIES})..."
                        )
                        time.sleep(delay)
                        continue
                    print(f"[{timestamp()}] ⚠ Rate limit reached. Stopping sync.")
                    self._rate_limited = True
                    raise
                else:
                    print(f"[{timestamp()}] ✗ Failed to create: {description} - {e.response.text}", file=sys.stderr)
                    return None
        return None

    def update_entry(self, entry_id, description, start_time, end_time):
        """Update an existing Toggl time entry. Returns the entry ID, or None if not found."""
        data = {
            "description": description,
            "start": start_time,
            "stop": end_time,
            "created_with": self.CREATED_WITH,
            "project_id": self.project_id,
            "tags": self.tags,
            "wid": self.workspace_id,
        }
        for attempt in range(self.RATE_LIMIT_MAX_RETRIES + 1):
            try:
                response = requests.put(
                    f"{self.BASE_URL}/workspaces/{self.workspace_id}/time_entries/{entry_id}",
                    json=data,
                    auth=(self.api_token, "api_token"),
                    timeout=self.DEFAULT_TIMEOUT,
                )
                response.raise_for_status()
                start_dt = self.parse_time(start_time).strftime("%Y-%m-%d %H:%M")
                print(f"[{timestamp()}] ↻ Updated: {description} (at {start_dt})")
                self._cached_entries = None
                return response.json().get("id")
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    return None  # Entry was deleted from Toggl
                elif e.response.status_code == 402:
                    if attempt < self.RATE_LIMIT_MAX_RETRIES:
                        delay = self._rate_limit_wait_seconds(e.response)
                        print(
                            f"[{timestamp()}] ⚠ Rate limit reached. "
                            f"Retrying in {delay}s ({attempt + 1}/{self.RATE_LIMIT_MAX_RETRIES})..."
                        )
                        time.sleep(delay)
                        continue
                    print(f"[{timestamp()}] ⚠ Rate limit reached. Stopping sync.")
                    self._rate_limited = True
                    raise
                else:
                    print(f"[{timestamp()}] ✗ Failed to update: {description} - {e.response.text}", file=sys.stderr)
                    return None
        return None

    def remove_duplicates(self):
        """Remove duplicate entries from Toggl, keeping most recent."""
        print(f"[{timestamp()}] Starting Toggl deduplication...")

        try:
            # Fetch the last year via the Reports API (one request, <=366 day span,
            # no lower-bound floor) instead of /me/time_entries' "before" cursor,
            # which hits the same ~90-day floor as start_date/since and would
            # eventually crash here once the account has >90 days of entries.
            today = datetime.now()
            one_year_ago = today - timedelta(days=365)
            all_entries = self._fetch_reports_entries(one_year_ago.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
            filtered_entries = [e for e in all_entries if e.get("project_id") == self.project_id]

            print(f"[{timestamp()}] Found {len(filtered_entries)} Toggl entries in project")

            # First pass: exact duplicates by (description, start, stop)
            entries_by_key = {}
            for entry in filtered_entries:
                desc = entry.get("description", "")
                if desc:
                    start = self.normalize_timestamp(entry["start"]).isoformat()
                    stop = self.normalize_timestamp(entry["stop"]).isoformat() if entry.get("stop") else ""
                    key = (desc, start, stop)
                    if key not in entries_by_key:
                        entries_by_key[key] = []
                    entries_by_key[key].append(entry)

            duplicates = {key: entries for key, entries in entries_by_key.items() if len(entries) > 1}

            first_pass_deleted_ids = set()
            if duplicates:
                total_deleted = 0
                entries_to_delete_count = sum(len(entries) - 1 for entries in duplicates.values())
                print(f"[{timestamp()}] Found {entries_to_delete_count} duplicate Toggl entries to remove:")

                for key, entries in duplicates.items():
                    print(f"  - {key[0]} ({len(entries)} occurrences)")
                    entries.sort(key=lambda x: x.get("id", 0))
                    entries_to_delete = entries[:-1]

                    for entry in entries_to_delete:
                        start = self.parse_time(entry["start"]).strftime("%Y-%m-%d %H:%M")
                        response = requests.delete(
                            f"{self.BASE_URL}/time_entries/{entry['id']}",
                            auth=(self.api_token, "api_token"),
                            timeout=self.DEFAULT_TIMEOUT,
                        )
                        if response.status_code == 200:
                            print(f"    ✓ Deleted: {start}")
                            total_deleted += 1
                            first_pass_deleted_ids.add(entry["id"])
                        else:
                            print(
                                f"    ✗ Failed to delete: {start} - {response.status_code}",
                                file=sys.stderr,
                            )

                print(f"[{timestamp()}] Successfully removed {total_deleted} duplicate Toggl entries")
                self._cached_entries = None
            else:
                print(f"[{timestamp()}] No exact Toggl duplicates found")

            # Second pass: same-watch entries — multiple Jellyfin playback sessions
            # (and thus multiple Toggl entries) for what is really one watch-through,
            # e.g. a movie paused overnight and finished days later, multiple clients
            # each reporting their own session for the same watch, or a sync that
            # recreated an entry Toggl already had. For this user, a genuine rewatch
            # of the same title doesn't happen inside of a year, so any repeat within
            # a rolling 30 days is assumed to be the same watch-through and gets
            # collapsed; that's generous enough to absorb multi-client reporting
            # quirks while staying a small fraction of the real rewatch cadence.
            NEAR_DUPLICATE_START_GAP_SECONDS = 30 * 24 * 3600
            entries_by_desc: dict = {}
            for entry in filtered_entries:
                desc = entry.get("description", "")
                if desc:
                    entries_by_desc.setdefault(desc, []).append(entry)

            close_dups = []
            for desc_entries in entries_by_desc.values():
                if len(desc_entries) < 2:
                    continue
                desc_entries.sort(key=lambda x: x["start"])
                i = 0
                while i < len(desc_entries):
                    cluster = [desc_entries[i]]
                    j = i + 1
                    while j < len(desc_entries):
                        prev = desc_entries[j - 1]
                        curr = desc_entries[j]
                        prev_start = self.normalize_timestamp(prev["start"])
                        curr_start = self.normalize_timestamp(curr["start"])
                        start_gap = (curr_start - prev_start).total_seconds()

                        prev_stop = self.normalize_timestamp(prev["stop"]) if prev.get("stop") else None
                        overlaps = prev_stop is not None and curr_start < prev_stop

                        if overlaps or start_gap <= NEAR_DUPLICATE_START_GAP_SECONDS:
                            cluster.append(curr)
                            j += 1
                        else:
                            break
                    if len(cluster) > 1:
                        cluster.sort(key=lambda x: x.get("id", 0))
                        close_dups.extend(cluster[:-1])
                    i = j

            # Exclude IDs already deleted in the first pass
            close_dups = [e for e in close_dups if e["id"] not in first_pass_deleted_ids]

            if close_dups:
                total_close_deleted = 0
                print(f"[{timestamp()}] Found {len(close_dups)} close-in-time Toggl duplicates to remove:")
                for entry in close_dups:
                    start = self.parse_time(entry["start"]).strftime("%Y-%m-%d %H:%M")
                    desc = entry.get("description", "")
                    response = requests.delete(
                        f"{self.BASE_URL}/time_entries/{entry['id']}",
                        auth=(self.api_token, "api_token"),
                        timeout=self.DEFAULT_TIMEOUT,
                    )
                    if response.status_code == 200:
                        print(f"  ✓ Deleted: {desc} (at {start})")
                        total_close_deleted += 1
                    else:
                        print(
                            f"  ✗ Failed to delete: {desc} (at {start}) - {response.status_code}",
                            file=sys.stderr,
                        )
                print(f"[{timestamp()}] Successfully removed {total_close_deleted} close-in-time Toggl duplicates")
                self._cached_entries = None
            else:
                print(f"[{timestamp()}] No close-in-time Toggl duplicates found")

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 402:
                print(f"[{timestamp()}] ⚠ Toggl rate limit reached. Skipping deduplication.")
                print(f"[{timestamp()}] This is temporary - try again in a few minutes.")
            else:
                raise
