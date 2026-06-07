# ShortsMaster Bot

ShortsMaster Bot is a free-tier-first YouTube Shorts automation pipeline. It monitors trends or Reddit story sources, generates original short scripts locally, creates narration, renders vertical videos, stores them for review, and simulates publishing by default.

Real YouTube upload is optional and disabled unless `PAPER_MODE=false`, `ENABLE_REAL_UPLOAD=true`, and `LIVE_UPLOAD_ENABLED=true` are all set.

## Free MVP Pipeline

- Reddit Story Shorts mode for AskReddit, TodayILearned, LetsNotMeet, NoStupidQuestions, LifeProTips, TIFU, and InterestingAsFuck.
- Legacy Reddit trend monitoring remains available when story mode is disabled.
- Google Trends through `pytrends`.
- TikTok disabled by default; enable only when a provider is configured.
- Ollama local script generation if available.
- Rule-based original script templates when Ollama is unavailable.
- Neural pt-BR narration with a male storyteller profile and gTTS fallback.
- Pollinations.ai image generation for non-story mode.
- MoviePy vertical video assembly, including procedural royalty-free retention-category backgrounds for story mode.
- SQLite queue, history, safety decisions, and metrics.
- Paper-mode simulated publishing by default; production flags are accepted only with `SHORTSMASTER_ENV=production`.

## Safety Defaults

- Never queues the same normalized topic twice.
- Never requires paid APIs to run.
- Never uses copied scripts, copyrighted clips, or copyrighted music.
- Never reads Reddit post or comment text verbatim in story mode.
- Stores Reddit source URLs for audit and frames Reddit anecdotes as unverified stories.
- Saves every rendered video under `videos/rendered` before publishing.
- Computes a quality score before upload or simulated publish.
- Scores hook strength, curiosity, storytelling, retention structure, and visual interest before allowing publication.
- Blocks static slide-style videos, weak hooks, and low-variation visuals.
- Blocks spammy titles/scripts.
- Caps real uploads at `DAILY_UPLOAD_LIMIT`, never above `MAX_DAILY_UPLOAD_LIMIT`.
- Stops real uploads when recent performance falls below configured thresholds.
- Blocks any background whose commercial-use rights are not explicitly verified.
- Auto-approves live upload only when quality score >= 75, safety score >= 90, and background rights are verified.

## Setup

```powershell
cd C:\Users\tgt\Desktop\botmaster\project4\shortmaster_bot
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

Optional local LLM:

```powershell
ollama pull llama3.1:8b
```

If Ollama is not installed or running, ShortsMaster automatically falls back to local templates.

## Default Environment

```env
PAPER_MODE=true
ENABLE_REAL_UPLOAD=false
DAILY_UPLOAD_LIMIT=5
MAX_DAILY_UPLOAD_LIMIT=5
MANUAL_APPROVAL_REQUIRED=true
TIKTOK_MONITOR_ENABLED=false
REDDIT_STORY_MODE_ENABLED=true
```

## Commands

Run one cycle:

```powershell
.\.venv\Scripts\python.exe -m app.main once
```

## Retention Design

Generated Shorts use a retention-first structure: hook, curiosity, escalation, reveal, and fast ending. Reddit Story Shorts mode rewrites eligible high-engagement Reddit posts or top comments into original story scripts, keeps the source URL in `story_source_report.json`, and burns center-screen Shorts-style subtitles over a royalty-free procedural background randomly selected from approved retention categories. It also generates three story-connected engagement prompts, scores them for comment and retention potential, and appends exactly one winner as the final narration sentence. Emotional blackmail, fake percentage challenges, intelligence bait, and bad-luck threats are blocked. CTA performance is grouped by prompt and type in `engagement_prompt_performance_report.json` as YouTube comments, likes, and views become available. Non-story mode keeps relevant generated visuals as the primary asset, limits on-screen text, and creates visual motion/cuts at least every two seconds with `MAX_VISUAL_BEAT_SECONDS=2.0`.

Run continuously:

```powershell
.\.venv\Scripts\python.exe -m app.main run
```

Generate the YouTube scheduler dashboard report without publishing:

```powershell
.\.venv\Scripts\python.exe -m app.main youtube-scheduler-report
```

List queued content:

```powershell
.\.venv\Scripts\python.exe -m app.main queue
```

Approve and process an item:

```powershell
.\.venv\Scripts\python.exe -m app.main approve 1 --process
```

Process approved items:

```powershell
.\.venv\Scripts\python.exe -m app.main process --limit 3
```

Refresh YouTube metrics:

```powershell
.\.venv\Scripts\python.exe -m app.main metrics
```

## Real Upload

Keep real upload disabled until you have reviewed generated videos and configured YouTube OAuth.

1. Add a YouTube OAuth Desktop App client JSON at `secrets/youtube_client_secret.json`.
2. Set `PAPER_MODE=false`.
3. Set `ENABLE_REAL_UPLOAD=true`.
4. Set `LIVE_UPLOAD_ENABLED=true`.
5. Keep `YOUTUBE_PRIVACY_STATUS=private` for the first rollout.

Run `python -m app.main youtube-auth-check` to open the local OAuth consent flow and store `secrets/youtube_token.json` without uploading anything.

## Scheduled YouTube Publishing

ShortMaster does not require a 24/7 worker. `.github/workflows/shortmaster-scheduled.yml` runs five ephemeral GitHub Actions jobs per day at 08:00, 11:30, 15:00, 18:30, and 22:00 in `America/Sao_Paulo`. Each run restores the SQLite state cache, generates at most one video, uploads at most one video, writes logs and reports, saves state, and exits.

Scheduled runs enable real upload only in production and force `YOUTUBE_PRIVACY_STATUS=private`. Every upload still requires `approved_for_live_upload=true`, quality score >= 75, safety score >= 90, verified background commercial rights, pt-BR validation, trust/freshness gates, duplicate checks, quota availability, and the daily limit.

Configure these GitHub Actions secrets:

- `YOUTUBE_CHANNEL_ID`
- `YOUTUBE_CLIENT_SECRET_JSON`
- `YOUTUBE_TOKEN_JSON`
- `YOUTUBE_CHANNEL_NAME` (optional)
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (optional)
- `BOTMASTER_STATUS_TOKEN` (optional)

Run one bounded job locally with:

```powershell
python -m app.main scheduled-job --job-id local-test
```

Manual workflow runs default to paper mode. Real manual upload must be explicitly selected in the workflow input. Logs and reports are retained as GitHub Actions artifacts for 30 days. Operational details are in `docs/github_actions_scheduled_jobs.md`.

## Docker

```powershell
docker build -t shortsmaster-bot .
docker run --env-file .env -v ${PWD}\data:/app/data -v ${PWD}\videos:/app/videos -v ${PWD}\logs:/app/logs shortsmaster-bot
```
## Custom Background Library

Reddit Story Mode uses only local MP4 files listed in the manifest. Add satisfying videos once to:

```text
background_library/
```

Then add one manifest entry per file in `background_library_manifest.json`:

```json
{
  "version": 2,
  "backgrounds": [
    {
      "filename": "cleaning-01.mp4",
      "source_url": "https://example.com/original-license-page",
      "license_type": "royalty-free commercial license",
      "added_by_user": "channel-owner",
      "approved_for_use": true,
      "commercial_rights_verified": true,
      "category": "deep_cleaning"
    }
  ]
}
```

Only `.mp4` files inside `background_library/` with both `approved_for_use=true` and `commercial_rights_verified=true` are eligible. The bot chooses randomly, loops short files, trims long files, skips corrupted files, and burns centered pt-BR subtitles over the selected background. Downloaded clips remain blocked until their commercial license is documented.

If no approved playable background remains, publishing is paused and the configured Telegram alert is sent. Inspect library status with:

```powershell
python -m app.main background-library-report
```

Reports are written to `reports/background_library_report.json` and `.html`.
