"""Jellyfin API client — reads playback sessions from the Playback Reporting plugin."""

import re
import uuid
from datetime import datetime

import requests

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")

DEFAULT_TIMEOUT = (3.05, 10)


# The Playback Reporting plugin stores DateCreated as local server time with
# up to 7 fractional digits (.NET "yyyy-MM-dd HH:mm:ss.FFFFFFF"). Python's
# datetime.fromisoformat only accepts up to 6 (microseconds), so truncate.
def _parse_playback_datetime(value):
    if "." in value:
        head, frac = value.split(".", 1)
        value = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(value)


class JellyfinAPI:
    """Jellyfin API client backed by the Playback Reporting plugin's custom-query endpoint."""

    def __init__(self, base_url, api_key, user_id=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # PlaybackActivity.UserId is stored dashless ("N" format); normalize
        # whatever the user pasted (dashed or not) to match it.
        self.user_id = user_id.replace("-", "") if user_id else None
        if self.user_id and not _HEX32_RE.match(self.user_id):
            raise ValueError(f"JELLYFIN_USER_ID does not look like a GUID: {user_id!r}")
        self._item_cache = {}

    def _headers(self):
        return {"X-Emby-Token": self.api_key}

    def fetch_history(self, since_dt, min_duration_seconds):
        """Return PlaybackActivity rows newer than since_dt with PlayDuration >= min_duration_seconds."""
        # The plugin's submit_custom_query endpoint takes a raw SQL string with no
        # bind-param support, so this is built by hand. Every interpolated value is
        # produced or validated by this class (strftime output, an int cast, and a
        # user_id already checked against _HEX32_RE in __init__) — never raw user input.
        since_str = since_dt.strftime("%Y-%m-%d %H:%M:%S")
        sql = (
            "SELECT rowid, DateCreated, UserId, ItemId, ItemType, ItemName, PlayDuration "
            "FROM PlaybackActivity "
            f"WHERE DateCreated > '{since_str}' AND PlayDuration >= {int(min_duration_seconds)} "  # nosec B608
        )
        if self.user_id:
            sql += f"AND UserId = '{self.user_id}' "  # nosec B608
        sql += "ORDER BY DateCreated ASC"

        response = requests.post(
            f"{self.base_url}/user_usage_stats/submit_custom_query",
            json={"CustomQueryString": sql, "ReplaceUserId": False},
            headers=self._headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        columns = payload["colums"]  # sic — plugin's own typo
        rows = []
        for raw_row in payload["results"]:
            row = dict(zip(columns, raw_row, strict=True))
            row["DateCreated"] = _parse_playback_datetime(row["DateCreated"])
            row["PlayDuration"] = int(row["PlayDuration"])
            row["rowid"] = int(row["rowid"])
            rows.append(row)
        return rows

    def get_item_details(self, item_id, user_id):
        """GET /Users/{user_id}/Items/{item_id}, cached per run."""
        if item_id in self._item_cache:
            return self._item_cache[item_id]

        canonical_user_id = str(uuid.UUID(hex=user_id))
        response = requests.get(
            f"{self.base_url}/Users/{canonical_user_id}/Items/{item_id}",
            headers=self._headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        item = response.json()
        self._item_cache[item_id] = item
        return item
