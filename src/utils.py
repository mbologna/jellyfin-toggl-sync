"""Utility functions for jellyfin-toggl-sync."""

import json
import os
import sys
from datetime import datetime


def timestamp():
    """Generate current timestamp for logging."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_gcs_path(file_path):
    """Parse a gs://bucket/object path into (bucket, blob_name)."""
    path = file_path[len("gs://") :]
    bucket, _, blob_name = path.partition("/")
    return bucket, blob_name


def load_json_file(file_path):
    """Load JSON data from a local path or gs:// URI."""
    if file_path.startswith("gs://"):
        from google.cloud import storage
        from google.cloud.exceptions import NotFound

        bucket_name, blob_name = _parse_gcs_path(file_path)
        try:
            client = storage.Client()
            blob = client.bucket(bucket_name).blob(blob_name)
            content = blob.download_as_text().strip()
            if not content:
                return None
            return json.loads(content)
        except NotFound:
            return None
        except json.JSONDecodeError, ValueError:
            print(f"[{timestamp()}] Warning: Invalid state file, starting fresh")
            return None

    if os.path.exists(file_path):
        try:
            with open(file_path) as f:
                content = f.read().strip()
                if not content:  # Empty file
                    return None
                return json.loads(content)
        except json.JSONDecodeError, ValueError:
            # Invalid or empty JSON file
            print(f"[{timestamp()}] Warning: Invalid state file, starting fresh")
            return None
    return None


def save_json_file(file_path, data):
    """Save JSON data to a local path or gs:// URI."""
    if file_path.startswith("gs://"):
        from google.cloud import storage

        bucket_name, blob_name = _parse_gcs_path(file_path)
        client = storage.Client()
        blob = client.bucket(bucket_name).blob(blob_name)
        blob.upload_from_string(json.dumps(data, indent=2), content_type="application/json")
        return

    # Ensure directory exists
    os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(file_path, 0o600)


def check_required_env_variables():
    """Validate all required environment variables are set."""
    required_env_vars = [
        "JELLYFIN_URL",
        "JELLYFIN_API_KEY",
        "TOGGL_API_TOKEN",
        "TOGGL_WORKSPACE_ID",
        "TOGGL_PROJECT_ID",
    ]
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]
    if missing_vars:
        print(
            f"[{timestamp()}] Error: Missing required environment variables: {', '.join(missing_vars)}",
            file=sys.stderr,
        )
        sys.exit(1)
