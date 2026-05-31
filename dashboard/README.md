# Painel BotMaster

Dashboard Flask para monitorar os quatro bots locais em `C:\Users\tgt\Desktop\botmaster`.

## Execucao Local

```powershell
cd C:\Users\tgt\Desktop\botmaster\dashboard
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Abra `http://127.0.0.1:5000`.

A senha de login e lida de `DASHBOARD_PASSWORD` no `.env`. `FLASK_SECRET_KEY` e obrigatoria para sessoes assinadas.

## Fontes de Dados

- Yield Optimizer: `project1\yield_optimizer_bot\app\data\state.json`, `logs\bot.log`
- Domain Hunter: `project2\projeto2` quando presente, caso contrario `projeto2`; le `data\domains.json` e `logs\domain_hunter_bot.log`
- Asset Flip: `project3\asset_flip_bot\data\profit_stats.json`, `data\assets_state.json`, `logs\asset_flip_bot.log`
- Trend Hunter: `project4\trend_hunter_bot\trend_hunter.db`, `logs\trend_hunter.log`

O dashboard nao altera dados dos bots. Ele apenas le logs, estado JSON e estado SQLite.

## Deploy no Render

1. Envie o diretorio `dashboard` para um repositorio Git.
2. Crie um novo Render Web Service.
3. Defina o comando de build:

```bash
pip install -r requirements.txt
```

4. Defina o comando de start:

```bash
gunicorn app:app
```

5. Adicione as variaveis de ambiente no Render:

```text
DASHBOARD_PASSWORD=<senha privada forte>
FLASK_SECRET_KEY=<segredo aleatorio com 64+ caracteres>
BOTMASTER_ROOT=/opt/render/project/src
```

No Render, os arquivos de dados dos bots precisam estar no repositorio implantado ou em um caminho de storage montado e referenciado por `BOTMASTER_ROOT`.
