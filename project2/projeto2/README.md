# Domain Hunter Bot

Bot de automacao assíncrona orientado a producao para monitorar, pontuar, registrar, listar e acompanhar dominios em expiracao.

O bot e seguro por padrao: o `.env` vem com `PAPER_MODE=true`, `SAFE_MODE=true`, `AUTO_BUY_ENABLED=false` e `DRY_RUN_PURCHASES=true`, entao registros e listagens em marketplaces ficam bloqueados ate que voce remova cada protecao de forma intencional.

## Estrutura de Pastas

```text
app/
  core/                 contextvars, barramento de eventos e primitivas de resiliencia
  db/                   repositorio PostgreSQL e schema
  observability/        logs JSON, metricas e servidor de health/status
  economics/            valuation, ROI, alocacao, precificacao, backtests e relatorios
  config/               configuracoes Pydantic
  scrapers/             feeds WhoisXML, ExpiredDomains, GoDaddy Auctions, NameJet, SnapNames e DropCatch
  analyzers/            scoring, backlinks e palavras-chave
  registrars/           clientes GoDaddy e Namecheap
  marketplaces/         clientes GoDaddy Auctions, Sedo e Afternic
  services/             gerenciador de dominios, Telegram, risco, transacoes e rebalanceamento
  utils/                auxiliares de compatibilidade
tests/                  testes async, resiliencia, idempotencia, Telegram e DB
```

## Arquitetura

- Monolito modular, nao microservicos.
- Barramento interno de eventos para `DOMAIN_SCANNED`, `DOMAIN_SCORED`, `DOMAIN_APPROVED`, `DOMAIN_REJECTED`, `DOMAIN_REGISTERED`, `LISTING_CREATED`, `ALERT_TRIGGERED` e `CRITICAL_FAILURE`.
- Propagacao de contexto com `contextvars` para `correlation_id`, `operation_id` e `execution_mode`.
- Logs JSON estruturados com redacao automatica de segredos.
- Persistencia PostgreSQL por pool `asyncpg`. Se `DATABASE__URL` estiver vazio, execucoes locais usam um repositorio em memoria para desenvolvimento seguro.
- Circuit breakers e backoff exponencial com jitter em scrapers, GoDaddy, Sedo, Afternic e Telegram.
- APScheduler executa ciclos de varredura e relatorios diarios.
- `aiohttp` expoe `/health`, `/metrics` e `/status` no modo scheduler.
- Aquisicao agora e guiada por economia: valuation, ROI, liquidez, prazo de carregamento e concentracao de capital precisam passar antes do registro.

## Instalacao

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Configuracao

Copie `.env.example` para `.env` e preencha apenas os valores necessarios para o seu modo.

```env
PAPER_MODE=true
DATABASE__URL=postgresql://domain_hunter:domain_hunter@localhost:5432/domain_hunter
GODADDY_API_KEY=
GODADDY_API_SECRET=
SEDO_API_KEY=
AFTERNIC_API_KEY=
DAN_API_KEY=
DROPCATCH_API_KEY=
NAMEBIO_EMAIL=
NAMEBIO_API_KEY=
BACKLINK_PROXY_URL=https://backlinklog.com/api/backlinks?domain={domain}
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Mantenha `.env` fora do controle de versao. Os scrapers de descoberta incluidos nao exigem credenciais de registrador. Chaves de registrador podem controlar compras, listagens e operacoes de conta.

As fontes padrao de varredura nao exigem credenciais quando feeds publicos estao disponiveis:

- `scraper.whoisxml_url` descobre downloads publicos de amostras do feed WhoisXML, e `scraper.whoisxml_download_urls` pode fixar URLs diretas de feeds CSV, JSON, ZIP, GZ ou TAR.GZ.
- `scraper.expireddomains_url` usa `https://www.expireddomains.net/deleted-domains/` por padrao e segue paginacao ate `scraper.expireddomains_max_pages`.
- `scraper.godaddy_auctions_urls` le feeds CSV ZIP do inventario GoDaddy Auctions.
- `scraper.namejet_urls` le feeds CSV de dominios expirando na NameJet.
- `scraper.snapnames_urls` le feeds CSV da SnapNames.
- `scraper.dropcatch_expiring_url` pode ler feeds JSON de expiracao/leilao da DropCatch quando uma chave de API estiver configurada.

Defina `PAPER_MODE=false` apenas depois de:

- configurar credenciais GoDaddy;
- confirmar que o PostgreSQL esta acessivel;
- escolher de forma intencional os limites de risco em `config.yaml`;
- concluir com sucesso uma pequena execucao em paper mode.

## Execucao

```bash
python -m app.main dashboard
python -m app.main run-once
python -m app.main scheduler
python -m app.main sniper --sniper-cycles 1
python -m app.main reprice
python -m app.main portfolio
```

O modo scheduler inicia:

- jobs de varredura;
- relatorio diario no Telegram as 09:00;
- `/health`;
- `/metrics`;
- `/status`.

## Telegram

O notificador usa `python-telegram-bot` com chamadas assíncronas de API. Usuario do bot: `@saldogodaddy_bot`.

Alertas suportados:

- health check de inicializacao;
- aprovacao pendente criada;
- candidato com score >= 90;
- candidato com liquidez A;
- risco de marca detectado;
- limite diario/semanal de orcamento atingido;
- bloqueios de `SAFE_MODE` e `DRY_RUN_PURCHASES`;
- excecao critica;
- deteccao de bot offline;
- resumo diario de seguranca.

Segredos sao carregados do `.env`; tokens nunca sao hardcoded nem registrados em logs. O Telegram e somente notificacao: comandos via Telegram nao aprovam nem compram dominios.

## Migracao PostgreSQL

1. Crie um banco e um usuario.
2. Defina `DATABASE__URL` no `.env`.
3. Rode:

```bash
python -m app.main dashboard
```

O repositorio cria automaticamente estas tabelas:

- `scanned_domains`
- `scored_domains`
- `registrations`
- `listings`
- `sales`
- `risk_events`
- `scheduler_runs`
- `alert_history`

Todos os dominios analisados sao persistidos, inclusive os rejeitados.

## Observabilidade

Logs estruturados incluem:

- `timestamp`
- `service`
- `event_name`
- `severity`
- `correlation_id`
- `execution_mode`
- `domain`
- `score`
- `operation_id`

Metricas compativeis com Prometheus ficam disponiveis em:

```text
GET /metrics
```

O status operacional fica disponivel em:

```text
GET /health
GET /status
```

## Controles de Risco

Configurados em `config.yaml`:

- safe mode ativo por padrao;
- aprovacao manual por `data/pending_approvals.json`;
- auditoria de compra dry-run por `data/purchase_attempts.json`;
- rejeicao de marcas famosas e variacoes confusas;
- verificacoes de liquidez, probabilidade de venda, meses esperados em carteira e valor esperado;
- gasto maximo diario e semanal;
- compras maximas por dia;
- tamanho maximo de portfolio;
- cooldown entre compras;
- aquisicoes somente `.com`, salvo liberacao explicita;
- preco maximo de compra por dominio;
- registros maximos por dia;
- exposicao maxima de capital;
- blacklist;
- limiar minimo de score;
- periodos de cooldown;
- parada de emergencia;
- modo de auditoria dry-run.

Quando `SAFE_MODE=true`, o bot pode pontuar, rejeitar, colocar em watchlist e notificar, mas nao compra um candidato a menos que `pending_approvals.json` contenha esse dominio com `approved=true`.

Quando `DRY_RUN_PURCHASES=true`, mesmo um candidato aprovado manualmente em modo live nao e enviado para GoDaddy. O bot grava o registro de "compraria" em `data/purchase_attempts.json` com dominio, preco, registrador, aprovador, timestamp, flag de bloqueio dry-run e snapshot da politica que permitiu a tentativa.

## Engine Economica

A plataforma usa uma engine de valuation multifator interpretavel em vez de um score heuristico simples. Ela estima:

- valor justo de mercado;
- probabilidade de revenda;
- tempo esperado em carteira;
- ROI esperado;
- ROI ajustado por liquidez;
- ROI ajustado por tempo;
- confianca de compra;
- preco recomendado de listagem.

Os fatores incluem vendas comparaveis, intencao comercial, proxy de CPC, demanda de busca, qualidade do TLD, qualidade linguistica, potencial de marca, tamanho, pronunciabilidade, momentum de tendencia, autoridade SEO, qualidade de backlinks, seguranca contra spam, seguranca de marca, qualidade de arquivo historico e liquidez.

A alocacao de capital rejeita aquisicoes que concentrariam demais o portfolio por extensao ou nicho, ou que ultrapassariam a exposicao de capital configurada.

A precificacao dinamica usa valuation, valor de palavra-chave, backlinks, idade do dominio, vendas comparaveis, liquidez e idade do inventario. Dominios parados sao descontados pela engine de repricing depois de 7 dias, em vez de acumular capital morto indefinidamente.

O caminho live de valuation enriquece candidatos com:

- historico Wayback CDX, primeira data vista e densidade de capturas;
- vendas comparaveis NameBio e medias por palavra-chave/TLD quando `NAMEBIO_EMAIL` e `NAMEBIO_API_KEY` estao configurados;
- contagem de backlinks por `BACKLINK_PROXY_URL`.

O paper mode continua deterministico e nao compra nem lista ativos reais.

## Domain Sniper

`python -m app.main sniper --sniper-cycles 0` roda continuamente. O sniper atualiza fontes a cada 30 segundos, observa dominios expirando na proxima hora, pre-envia um backorder DropCatch quando configurado e agenda a tentativa de registro no registrador no timestamp alvo de expiracao, com um pequeno loop de retry.

A disponibilidade exata no registro ainda depende de latencia de registrador/rede e do timestamp do feed de drop, entao comece em paper mode e verifique a sincronizacao do relogio antes de usar capital real.

## Rastreador de Portfolio

`python -m app.main portfolio` grava `data/portfolio_report.csv` com:

- dominio;
- custo;
- preco de listagem;
- dias listado;
- ROI por dominio;
- marketplaces.

O dashboard e o relatorio diario do Telegram incluem valor total do portfolio, lucro realizado e alertas de dominio vendido.

## KPIs de Lucratividade

Acompanhe estes indicadores antes de aumentar capital live:

- ROI esperado vs ROI real;
- calibracao da probabilidade de revenda;
- taxa de acerto;
- media de dias em carteira;
- utilizacao de capital;
- ROI por extensao;
- ROI por nicho;
- ROI por faixa de score;
- taxa de falsos positivos;
- proporcao de inventario parado;
- relacao aquisicao-venda;
- taxa de conversao por marketplace.

## Armadilhas de Lucratividade

- Comprar dominios de score alto com liquidez fraca.
- Exposicao excessiva a `.io`, `.net` ou a uma unica tendencia.
- Manter inventario de baixa qualidade ate a renovacao.
- Tratar backlinks como valor sem checar spam e qualidade de anchor text.
- Pagar custos premium de aquisicao por nomes com longo prazo esperado em carteira.
- Ignorar termos parecidos com marcas que parecem bons para branding, mas trazem risco legal.
- Aumentar limites live antes de calibrar esperado vs real em paper mode.

## Desenvolvimento

```bash
make install
make test
make lint
make typecheck
```

Rode testes de integracao dependentes de banco com:

```bash
set TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/domain_hunter_test
python -m pytest tests/test_db_integration.py
```

## Docker

```bash
docker build -t domain-hunter-bot .
docker run --env-file .env -p 8080:8080 domain-hunter-bot
```

O container roda como usuario nao-root e expoe a porta `8080`.

## Rollout de Producao Priorizado

1. Rode em paper mode com PostgreSQL ativo.
2. Verifique `/health`, `/status`, logs e alertas de inicializacao no Telegram.
3. Ajuste limites de risco e blacklist.
4. Rode `run-once` repetidamente e inspecione dominios rejeitados e valuations persistidos.
5. Faca backtest de mudancas de limiar antes de alteracoes live.
6. Ative modo live com `max_daily_registrations=1`.
7. Aumente gradualmente a exposicao de capital somente depois de validacao lucrativa.
