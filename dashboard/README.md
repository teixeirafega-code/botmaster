# BotMaster Dashboard

Flask dashboard for monitoring the four local bots in `C:\Users\tgt\Desktop\botmaster`.

## Local Run

```powershell
cd C:\Users\tgt\Desktop\botmaster\dashboard
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:5000`.

The login password is read from `DASHBOARD_PASSWORD` in `.env`. `FLASK_SECRET_KEY` is required for signed sessions.

## Data Sources

- Yield Optimizer: `project1\yield_optimizer_bot\app\data\state.json`, `logs\bot.log`
- Domain Hunter: `project2\projeto2` when present, otherwise `projeto2`; reads `data\domains.json`, `logs\domain_hunter_bot.log`
- Asset Flip: `project3\asset_flip_bot\data\profit_stats.json`, `data\assets_state.json`, `logs\asset_flip_bot.log`
- Trend Hunter: `project4\trend_hunter_bot\trend_hunter.db`, `logs\trend_hunter.log`

The dashboard does not mutate bot data. It only reads logs, JSON state, and SQLite state.

## Render Deploy

1. Push the `dashboard` directory to a Git repository.
2. Create a new Render Web Service.
3. Set build command:

```bash
pip install -r requirements.txt
```

4. Set start command:

```bash
gunicorn app:app
```

5. Add environment variables in Render:

```text
DASHBOARD_PASSWORD=<strong private password>
FLASK_SECRET_KEY=<64+ character random secret>
BOTMASTER_ROOT=/opt/render/project/src
```

For Render, bot data files must be present in the deployed repository or mounted storage path referenced by `BOTMASTER_ROOT`.
