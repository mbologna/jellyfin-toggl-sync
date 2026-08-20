# jellyfin-toggl-sync

> Automatically sync Jellyfin playback sessions to Toggl for complete time tracking

## Motivation

I use Toggl for holistic time management, tracking work, projects, and personal activities. However, entertainment time was missing from this picture.

By syncing my Jellyfin viewing history to Toggl, I now have:
- **Complete time tracking**: Work, hobbies, AND entertainment in one place
- **Better insights**: Understand actual free time vs. perceived free time
- **Accurate reporting**: No gaps in my daily timeline
- **Effortless tracking**: No manual entry for movies and TV shows

This is a sibling project to [trakt-toggl-sync](https://github.com/mbologna/trakt-toggl-sync), doing the same thing but reading watch history from a self-hosted Jellyfin server instead of Trakt.

### Why not just use trakt-toggl-sync?

In August 2026, developers started reporting that their existing Trakt API applications had disappeared without notice, and that creating a new one now requires a paid Trakt VIP subscription ([Reddit thread](https://www.reddit.com/r/trakt/comments/1vb2gyc/api_deleted_vip_required_to_be_a_developer_now/), [Trakt forum report](https://forums.trakt.tv/t/unable-to-create-a-new-api-application-after-purchasing-trakt-vip-the-create-button-does-nothing/119966)). Trakt hasn't published an official policy statement about this, so it's unclear whether it's permanent, but it means anyone without VIP may no longer be able to set up `trakt-toggl-sync` from scratch.

`jellyfin-toggl-sync` sidesteps the problem entirely: it only needs a self-hosted Jellyfin server and the free Playback Reporting plugin, no Trakt account, API application, or subscription required.

## Features

- ✅ Uses **actual watched time** (pauses excluded), not the media's full runtime
- ✅ Auto-deduplication of Toggl entries
- ✅ Smart syncing with a stateful sync log (prevents re-watch duplicates, updates in place while a session is still growing)
- ✅ Retroactive close-in-time duplicate cleanup (24-hour window)
- ✅ Graceful rate limit handling
- ✅ Multiple deployment options (local, Docker, Kubernetes)

## Prerequisite: Playback Reporting plugin

Jellyfin itself doesn't expose historical per-session watch duration. This project reads from the **[Playback Reporting](https://github.com/jellyfin/jellyfin-plugin-playbackreporting)** plugin, which must be installed on your Jellyfin server (Dashboard → Plugins → Catalog) *before* syncing will find anything.

## How It Works

1. **Deduplicate Toggl** - Removes duplicate time entries, including a second pass that clusters entries with the same title within 24 hours and keeps only the most recently created one
2. **Sync** - Queries the Playback Reporting plugin for sessions in the last `JELLYFIN_HISTORY_HOURS` (default: 24) with actual watched time over `JELLYFIN_MIN_DURATION_MINUTES` (default: 10), and creates or updates a Toggl entry per session, using a persistent state file (`.sync_state.json`) to map each Jellyfin session to its Toggl entry ID

Because a Playback Reporting row is written at playback **start** and its duration keeps growing until the session stops, a session synced mid-watch will get its Toggl entry **updated in place** on the next run once more of it has been watched (or once it's finished). A rewatch of the same item later gets its own new session/entry; it doesn't overwrite the earlier one.

Rate limits are handled gracefully: if Toggl returns 402, deduplication is skipped and sync continues.

## Example Output

```
[2026-08-20 12:47:51] ===== Starting jellyfin-toggl-sync =====

[2026-08-20 12:47:51] === Step 1: Removing Toggl Duplicates ===
[2026-08-20 12:47:52] No exact Toggl duplicates found

[2026-08-20 12:47:53] === Step 2: Syncing Jellyfin to Toggl ===
[2026-08-20 12:47:53] Fetching Jellyfin plays since 2026-08-19T12:47:53 (> 10 min)...
[2026-08-20 12:47:55] ✓ Created: 📺 The Office - S03E15 - Business School (at 2026-08-19 20:00)
[2026-08-20 12:47:56] Skipped (exists): 🎞️ Inception (2010)

[2026-08-20 12:48:00] ===== Sync Complete =====
```

## Prerequisites

**API Credentials:**
- Jellyfin API key: Dashboard → Advanced → API Keys (any admin-created key works)
- Toggl API: https://track.toggl.com/profile

**Runtime:**
- Python 3.14+ with [uv](https://github.com/astral-sh/uv)
- Docker (optional)
- Kubernetes (optional)

## Usage

### Quick Start

```bash
cp .env.template .env
# Edit .env with your API credentials

# Run locally
make run

# Or with Docker
make docker-run
```

### Configuration

Edit `.env`:

```env
# Jellyfin
JELLYFIN_URL=https://jellyfin.example.com
JELLYFIN_API_KEY=your_jellyfin_api_key
JELLYFIN_USER_ID=                        # optional, blank = all users
JELLYFIN_HISTORY_HOURS=24
JELLYFIN_MIN_DURATION_MINUTES=10

# Toggl
TOGGL_API_TOKEN=your_api_token
TOGGL_WORKSPACE_ID=your_workspace_id
TOGGL_PROJECT_ID=your_project_id
TOGGL_TAGS=watching,entertainment

# State (optional, defaults to .sync_state.json)
SYNC_STATE_FILE=.sync_state.json
```

> **Timezone note:** the Playback Reporting plugin records session start times in the Jellyfin *server's local time*, not UTC. This tool treats those timestamps as-is when creating Toggl entries. If your Jellyfin server and Toggl account expect different timezones, entry times may look shifted; this is a Playback Reporting plugin limitation, not something this tool can correct without knowing the server's TZ.

### Docker

```bash
# One-time setup for multi-platform builds
make docker-setup

# Build for local architecture
make docker-build

# Build and push multi-platform (amd64 + arm64) to Docker Hub
make docker-push
```

> **Note:** The Docker image starts `src/server.py` by default (an HTTP server on `$PORT`, default 8080). This is the entrypoint used when deployed to GCP Cloud Run. To run a one-shot sync from the image, override the command:
>
> ```bash
> docker run --env-file .env jellyfin-toggl-sync:latest python src/sync.py
> ```
>
> For local development, `make run` (which calls `uv run python -u sync.py` directly) is simpler and does not require Docker.

### Kubernetes

The Kubernetes deployment uses a CronJob that runs every hour, with persistent storage for the sync state file.

#### Initial Setup

```bash
# Create namespace + state PVC
kubectl apply -f k8s/base/namespace.yaml
kubectl apply -f k8s/base/pvc.yaml

# Copy and edit configuration templates
cp k8s/base/configmap-template.yaml k8s/secrets/configmap.yaml
cp k8s/base/secret-template.yaml k8s/secrets/secret.yaml

# Edit k8s/secrets/configmap.yaml with your Jellyfin URL / Toggl IDs
# Edit k8s/secrets/secret.yaml with base64-encoded API credentials:
echo -n "your_jellyfin_api_key" | base64
echo -n "your_toggl_api_token" | base64

# Apply configuration, secrets, and the CronJob
kubectl apply -f k8s/secrets/configmap.yaml
kubectl apply -f k8s/secrets/secret.yaml
kubectl apply -f k8s/base/cronjob.yaml
```

Or just run `make k8s-deploy` once the templates above are copied and edited.

##### Management Commands

```bash
# Check CronJob status
make k8s-status

# View logs from latest run
make k8s-logs

# Manually trigger a sync
make k8s-manual

# Delete everything
make k8s-delete
```

## Troubleshooting

### Rate Limiting (402 Error)
- Handled gracefully: sync continues
- Deduplication skipped temporarily
- Try again in a few minutes

### No Entries Syncing
- Confirm the Playback Reporting plugin is installed and has recorded sessions (check its dashboard page in Jellyfin)
- Verify `TOGGL_PROJECT_ID` is correct
- Check logs for "Skipped (exists)" messages
- Adjust `JELLYFIN_HISTORY_HOURS` to look further back
- Adjust `JELLYFIN_MIN_DURATION_MINUTES` if your plays are shorter than the default 10-minute threshold

### Kubernetes Pod Crashes
```bash
# Check pod logs
kubectl logs -l app=jellyfin-toggl-sync -n jellyfin-toggl

# Check pod events
kubectl describe pod -l app=jellyfin-toggl-sync -n jellyfin-toggl

# Verify secrets are created correctly
kubectl get secrets -n jellyfin-toggl
kubectl describe secret jellyfin-toggl-credentials -n jellyfin-toggl
```

## Development

```bash
make install    # Install dependencies
make run        # Run sync
make test       # Run tests
make lint       # Check code
make format     # Format code
make check      # Lint and format
make test-cov   # Run tests with coverage
make clean      # Clean cache
```

### End-to-End Testing

The project includes E2E tests that verify integration with real Jellyfin and Toggl APIs.

```bash
export E2E_JELLYFIN_URL="https://jellyfin.example.com"
export E2E_JELLYFIN_API_KEY="your_api_key"
export E2E_TOGGL_API_TOKEN="your_api_token"
export E2E_TOGGL_WORKSPACE_ID="123456"
export E2E_TOGGL_PROJECT_ID="789012"
export E2E_TEST_ENABLED=true

make test-e2e
```

**Note:** E2E tests will create temporary test entries in your Toggl project. These are tagged with `e2e-test` and should be cleaned up manually.

### Pre-commit Hooks

Install pre-commit hooks to automatically check code quality before commits:

```bash
make pre-commit-install

# Run hooks manually
make pre-commit-run
```

Hooks include:
- Ruff linting and formatting
- YAML/TOML syntax checking
- Trailing whitespace removal
- Security vulnerability scanning (Bandit)
