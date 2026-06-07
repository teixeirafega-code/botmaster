# ShortsMaster Bot YouTube OAuth Setup Guide

This guide sets up YouTube OAuth for ShortsMaster Bot without uploading anything. The safe validation command uses `channels.list(mine=true)` to detect the authenticated channel, compare it with the configured channel, and confirm credentials work.

Current project paths:

- Project root: `C:\Users\tgt\Desktop\botmaster\project4\shortmaster_bot`
- OAuth client secret: `secrets\youtube_client_secret.json`
- OAuth token: `secrets\youtube_token.json`
- Config file: `config.yaml`
- Safe channel validation command: `python -m app.main youtube-auth-check`
- Pre-upload safety checklist: `python -m app.main pre-upload-checklist`

## 1. Confirm Live Upload Safety Stays Off

Before OAuth setup, keep live upload disabled:

```powershell
$env:PAPER_MODE="true"
$env:ENABLE_REAL_UPLOAD="false"
$env:LIVE_UPLOAD_ENABLED="false"
python -m app.main pre-upload-checklist
```

The checklist should show `real_upload_allowed: false`. This is expected and safe.

Real uploading requires all of these to be true at the same time:

- `PAPER_MODE=false`
- `ENABLE_REAL_UPLOAD=true`
- `LIVE_UPLOAD_ENABLED=true`
- `publishing.channel_id` configured
- `approved_for_live_upload=true` for the specific queue item
- OAuth credentials present
- daily upload limit and quota checks passing

Do not change those flags during OAuth setup.

## 2. Create a Google Cloud Project

1. Open the Google Cloud Console: <https://console.cloud.google.com/>
2. Sign in with the Google account that owns or manages the YouTube channel.
3. Open the project selector at the top of the page.
4. Select `New Project`.
5. Use a clear name, for example `ShortsMaster Bot`.
6. Create the project and make sure it is selected.

Google's YouTube Data API overview says a Google account, a Google Cloud project, API enablement, and authorization credentials are required before an application can submit API requests.

## 3. Enable YouTube Data API v3

1. In the selected Google Cloud project, open `APIs & Services` -> `Library`.
2. Search for `YouTube Data API v3`.
3. Open the API page.
4. Click `Enable`.
5. Then open `APIs & Services` -> `Enabled APIs & services` and confirm `YouTube Data API v3` is listed.

## 4. Configure the OAuth Consent Screen

1. Open `APIs & Services` -> `OAuth consent screen`.
2. Choose the user type:
   - `External` for a normal Google account or public testing.
   - `Internal` only if you are inside a Google Workspace organization and want only organization users.
3. Fill in app basics:
   - App name: `ShortsMaster Bot`
   - User support email: your email
   - Developer contact email: your email
4. Add scopes used by this bot:
   - `https://www.googleapis.com/auth/youtube.upload`
   - `https://www.googleapis.com/auth/youtube.readonly`
5. If the app is in testing mode, add the Google account that owns the target YouTube channel as a test user.
6. Save the consent screen.

Notes:

- The bot uses OAuth because YouTube uploads and authenticated channel detection require user authorization.
- YouTube Data API OAuth documentation states that service accounts are not supported for YouTube accounts. Use a real Google account with access to the YouTube channel.

## 5. Create OAuth Credentials

1. Open `APIs & Services` -> `Credentials`.
2. Click `Create Credentials`.
3. Select `OAuth client ID`.
4. Application type: `Desktop app`.
5. Name: `ShortsMaster Bot Local`.
6. Click `Create`.
7. Download the JSON file.

The downloaded file is usually named something like:

```text
client_secret_1234567890-abcdef.apps.googleusercontent.com.json
```

## 6. Save `youtube_client_secret.json`

Create the project secrets directory if needed:

```powershell
New-Item -ItemType Directory -Force -Path .\secrets
```

Move or rename the downloaded OAuth client file to:

```text
C:\Users\tgt\Desktop\botmaster\project4\shortmaster_bot\secrets\youtube_client_secret.json
```

From inside the project directory, the relative path should be:

```text
secrets\youtube_client_secret.json
```

Confirm `config.yaml` points to the same path:

```yaml
publishing:
  youtube_client_secrets_file: secrets/youtube_client_secret.json
  youtube_token_file: secrets/youtube_token.json
  oauth_mode: local_server
```

Do not commit the `secrets` directory.

## 7. Generate `youtube_token.json`

From the project root:

```powershell
cd C:\Users\tgt\Desktop\botmaster\project4\shortmaster_bot
$env:PAPER_MODE="true"
$env:ENABLE_REAL_UPLOAD="false"
$env:LIVE_UPLOAD_ENABLED="false"
python -m app.main youtube-auth-check
```

What happens:

1. The command loads `secrets\youtube_client_secret.json`.
2. It confirms the client file is a Desktop App credential with a top-level `installed` section.
3. If `secrets\youtube_token.json` belongs to a different OAuth client, the bot archives it and starts a fresh authorization.
4. If `secrets\youtube_token.json` does not exist, it starts a localhost OAuth flow with `run_local_server(port=0)`.
5. Your browser opens the Google authorization URL.
6. Sign in as the Google account that owns or manages the YouTube channel.
7. Grant the requested YouTube scopes.
8. Google redirects back to the local callback server and the bot writes `secrets\youtube_token.json`.
9. The bot calls `channels.list(mine=true)` to detect the authenticated YouTube channel.

This does not upload a video.

## 8. Detect and Configure the Channel

After OAuth succeeds, the command prints a result like:

```json
{
  "authenticated_channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx",
  "authenticated_channel_title": "Your Channel Name",
  "authenticated_channel_handle": "@yourhandle",
  "configured_channel_id": "",
  "configured_channel_name": "",
  "matches_configured_channel": false,
  "upload_attempted": false
}
```

Copy the detected channel ID and title into `config.yaml`:

```yaml
publishing:
  channel_id: "UCxxxxxxxxxxxxxxxxxxxxxx"
  channel_name: "Your Channel Name"
```

Run the check again:

```powershell
python -m app.main youtube-auth-check
```

Expected result:

```json
"matches_configured_channel": true
```

## 9. Validate Connection Without Uploading

Run:

```powershell
python -m app.main youtube-auth-check
python -m app.main pre-upload-checklist
```

Validation passes when:

- `youtube-auth-check` returns the authenticated channel.
- `matches_configured_channel` is `true`.
- `upload_attempted` is `false`.
- `pre-upload-checklist` still shows `real_upload_allowed: false` while the live flags are off.

The channel check uses the YouTube `channels.list` method with `mine=true`, which returns channels owned by the authenticated user. That API method has a quota cost of 1 unit.

## 10. Troubleshooting

### `YouTube OAuth client secret file not found`

Check that this file exists:

```text
secrets\youtube_client_secret.json
```

Also confirm `config.yaml` uses the same path.

### `missing required parameter: redirect_uri` or `redirect_uri_mismatch`

Create OAuth credentials as `Desktop app`. If you used a web application client, create a new desktop OAuth client and replace `youtube_client_secret.json`.

Also confirm:

```yaml
publishing:
  oauth_mode: local_server
```

If you replaced the OAuth client, delete or let the bot archive the old `secrets\youtube_token.json` before trying again.

### Browser says the app is unverified

For private testing, keep the OAuth app in testing mode and add your Google account as a test user. For public use, complete Google's OAuth app verification process.

### Wrong YouTube channel detected

Delete `secrets\youtube_token.json`, then rerun:

```powershell
python -m app.main youtube-auth-check
```

Sign in with the correct Google account.

### `matches_configured_channel` is false

Copy `authenticated_channel_id` into:

```yaml
publishing:
  channel_id: "..."
```

Then rerun `python -m app.main youtube-auth-check`.

## 11. Live Upload Readiness Checklist

After OAuth and channel detection work, keep live uploads disabled until you intentionally approve a video.

Before a real upload, all of these must pass:

```powershell
python -m app.main pre-upload-checklist --queue-id <QUEUE_ID>
```

Required:

- `paper_mode_disabled`
- `enable_real_upload_true`
- `live_upload_enabled_true`
- `channel_id_configured`
- `client_secrets_present`
- `oauth_token_present`
- `approved_for_live_upload`
- `duplicate_upload_absent`
- `daily_upload_limit_available`
- `youtube_quota_available`
- `video_file_exists`

Final per-video approval:

```powershell
python -m app.main approve-live <QUEUE_ID>
```

## References

- YouTube Data API overview: <https://developers.google.com/youtube/v3/getting-started>
- YouTube OAuth guide: <https://developers.google.com/youtube/v3/guides/authentication>
- OAuth 2.0 for desktop apps: <https://developers.google.com/identity/protocols/oauth2/native-app>
- YouTube `channels.list`: <https://developers.google.com/youtube/v3/docs/channels/list>
