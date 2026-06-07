# GitHub Actions Scheduled Jobs

ShortMaster uses bounded GitHub Actions jobs instead of a persistent Render worker.

## Schedule

The workflow `.github/workflows/shortmaster-scheduled.yml` runs five times daily:

| Sao Paulo | UTC cron |
| --- | --- |
| 08:00 | `0 11 * * *` |
| 11:30 | `30 14 * * *` |
| 15:00 | `0 18 * * *` |
| 18:30 | `30 21 * * *` |
| 22:00 | `0 1 * * *` |

GitHub scheduled workflows use UTC. The jobs are serialized by a workflow concurrency group.

## Execution Contract

Each run:

1. Checks out the default branch.
2. Installs Python, ffmpeg, fonts, and project dependencies.
3. Restores the latest `data/` cache containing SQLite queue and safety state.
4. Validates production secrets.
5. Runs `python -m app.main scheduled-job`.
6. Generates at most one video.
7. Uploads at most one video.
8. Saves the updated state cache.
9. Uploads `logs/` and `reports/` as a 30-day artifact.
10. Exits.

No generated MP4 is retained by GitHub after a successful upload.

## Required Secrets

Add these repository or environment secrets under GitHub Actions:

- `YOUTUBE_CHANNEL_ID`
- `YOUTUBE_CLIENT_SECRET_JSON`
- `YOUTUBE_TOKEN_JSON`

Optional integrations:

- `YOUTUBE_CHANNEL_NAME`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `BOTMASTER_STATUS_TOKEN`

Never commit OAuth JSON or tokens.

## Upload Guardrails

Production jobs force private uploads. A video is blocked unless:

- `quality_score >= 75`
- `safety_score >= 90`
- `approved_for_live_upload=true`
- background commercial rights are verified
- script, narration, subtitles, and title are pt-BR
- trust and freshness checks pass
- duplicate and daily quota checks pass

Manual dispatch defaults to paper mode. Selecting the `publish` input explicitly enables the same private production path used by scheduled runs.

## State And Cost

GitHub-hosted runners are ephemeral. Queue history, duplicate state, daily quota usage, and scheduler pause state live in the cached SQLite `data/` directory. A unique cache key is written after every run, and the next serialized run restores the latest matching cache.

GitHub Actions is free on standard hosted runners for public repositories. Private repositories use the account's included Actions minutes and storage before any paid usage. A self-hosted runner can use the same workflow without hosted-runner minute charges.

## Failure Handling

The workflow always attempts to save state and upload logs, even after a failed publishing step. Guardrail failures, missing backgrounds, missing secrets, OAuth errors, and upload errors remain visible in the workflow summary and `scheduled_job_report.json`.
