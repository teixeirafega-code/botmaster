# ShortMaster Render Worker Deployment Checklist

ShortMaster runs on Render as a background worker with:

- `startCommand: python -m app.main run`
- five private upload slots per day
- paper mode enabled by default
- live upload disabled by default
- health heartbeat logs every five minutes
- persistent disk mounted at `/var/data`

## Persistent Storage

Render disk is required for production operation. ShortMaster stores SQLite state, queue history, rendered videos, generated voice/images, reports, logs, and optional refreshed OAuth token files. Without a persistent disk, every deploy or restart can lose the queue database and generated review assets.

The worker manifest mounts:

```yaml
disk:
  name: botmaster-shortmaster-data
  mountPath: /var/data
  sizeGB: 10
```

The worker uses:

```env
SHORTSMASTER_STORAGE_DIR=/var/data/shortmaster
DATABASE_PATH=/var/data/shortmaster/shortmaster.db
VIDEOS_DIR=videos
REPORTS_DIR=reports
LOGS_DIR=logs
```

These resolve to `/var/data/shortmaster/videos`, `/var/data/shortmaster/reports`, and `/var/data/shortmaster/logs`.

## Required Safe Defaults

Keep these defaults for the first deploy:

```env
PAPER_MODE=true
ENABLE_REAL_UPLOAD=false
LIVE_UPLOAD_ENABLED=false
YOUTUBE_PRIVACY_STATUS=private
MANUAL_APPROVAL_REQUIRED=true
TIKTOK_MONITOR_ENABLED=false
```

Do not set `YOUTUBE_PRIVACY_STATUS=public` until private rollout has been reviewed and explicitly approved.

## Live Upload Gates

A real upload is still blocked unless all of these are true:

```env
PAPER_MODE=false
ENABLE_REAL_UPLOAD=true
LIVE_UPLOAD_ENABLED=true
YOUTUBE_CHANNEL_ID=<channel id>
```

Each queued video must also have `approved_for_live_upload=true`, quality score >= 75, safety score >= 90, trust score passing, freshness passing for current topics, no duplicate topic/title, quota available, and daily upload limit available.

## YouTube OAuth Token Handling

Do not commit `youtube_client_secret.json` or `youtube_token.json`.

For Render, set secrets in the Render dashboard as environment variables:

```env
YOUTUBE_CLIENT_SECRET_JSON=<full Desktop App client secret JSON>
YOUTUBE_TOKEN_JSON=<full authorized-user token JSON generated locally>
YOUTUBE_OAUTH_MODE=disabled
```

`YOUTUBE_OAUTH_MODE=disabled` prevents the background worker from trying to open an interactive browser or console OAuth flow on Render. Generate `YOUTUBE_TOKEN_JSON` locally with:

```powershell
python -m app.main youtube-auth-check
```

Then copy the contents of the uncommitted local token file into the Render secret value.

## Channel Configuration

Set the configured channel in Render:

```env
YOUTUBE_CHANNEL_ID=UC5zWjQwJb5IWsTHVAgu0Pgw
YOUTUBE_CHANNEL_NAME=espankshort
```

Before enabling live upload, validate the token locally or in a controlled environment:

```powershell
python -m app.main youtube-auth-check
```

Expected:

```json
{
  "matches_configured_channel": true,
  "upload_attempted": false
}
```

## Health Heartbeat Logs

Render background workers do not expose a web health endpoint by default. ShortMaster writes heartbeat logs every `HEARTBEAT_INTERVAL_MINUTES`:

```text
ShortsMaster heartbeat: pending_queue=... waiting_upload=... next_upload=... paper_mode=... real_upload_enabled=...
```

Use Render logs to confirm that the worker is alive and that scheduler jobs are registered.

## Auto Restart

Render automatically restarts worker processes after crashes and deploys. The manifest enables `autoDeploy: true`; Render handles process supervision. ShortMaster also pauses YouTube upload slots internally after repeated upload failures or high validation failure rate.

## Deployment Steps

1. Create or update the Render Blueprint using the root `render.yaml`.
2. Confirm the `botmaster-shortmaster` worker is created with root directory `project4/shortmaster_bot`.
3. Confirm the persistent disk is attached at `/var/data`.
4. Set required Render env vars and secret values.
5. Deploy with `PAPER_MODE=true`, `ENABLE_REAL_UPLOAD=false`, and `LIVE_UPLOAD_ENABLED=false`.
6. Check Render logs for heartbeat messages.
7. Confirm generated videos and reports persist under `/var/data/shortmaster`.
8. Run private live upload only after setting all live gates explicitly and approving one video for live upload.
