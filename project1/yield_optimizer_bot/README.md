# Yield Optimizer Bot (DeFi Automation)

**Yield Optimizer Bot** é um sistema profissional de automação para monitorar APYs em tempo real e rebalancear saldos de **USDT/USDC** entre **Aave v3**, **Compound** e **Curve**, movendo fundos para o protocolo com maior rendimento líquido após **gas**, **slippage** e thresholds.

> Observação importante: este repositório fornece a arquitetura enterprise-grade (pronta para produção) e módulos principais (gas estimation, risk controls, schedulers, logging, engines). Para ficar 100% funcional em mainnet, você precisa preencher endereços de contratos/ABIs e rotas de swap para cada chain/protocolo (dependendo do seu strategy desejado).

---

## Visão geral da arquitetura
- **APScheduler**: jobs contínuos (monitor APYs, rebalance, healthcheck, heartbeat)
- **Arquitetura modular**: protocols (Aave/Compound/Curve), serviços (aggregator/portfolio/rebalance/risk), blockchain (Web3, wallet, gas, tx)
- **Segurança**: private key via `.env`, dry-run por padrão, emergency stop, validações e cooldown
- **Observabilidade**: logging rotativo + console

---

## Requisitos
- Python 3.12+
- Conta em node RPC (Infura/Alchemy/ou próprio)

---

## Setup local
### 1) Instale dependências
```bash
cd yield_optimizer_bot
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2) Configure `.env`
Copie:
```bash
copy .env.example .env
```
Preencha:
- `RPC_ETHEREUM_URL`
- `RPC_POLYGON_URL`
- `PRIVATE_KEY`

### 3) Configure `config.yaml`
Edite `yield_optimizer_bot/config.yaml`:
- `app.mode` (paper_trading ou production)
- `networks.rpc_urls` e valores reais (idealmente remover hardcodes)
- thresholds (cooldown, min_profit_usd, slippage)
- `protocols.enabled`

> Dica: garanta que o módulo de cada protocolo está apontando para os contratos corretos da sua versão/mercado.

### 4) Execução
```bash
python -m yield_optimizer_bot.app.main
```

---

## Modo Paper Trading
Por padrão, o `config.yaml` está com:
- `dry_run: true`
- `app.mode: paper_trading`

Isso mantém segurança máxima: decisões e simulações registradas sem broadcast real.

---

## Docker (VPS/Prod)
### Build
```bash
docker build -t yield-optimizer-bot -f yield_optimizer_bot/Dockerfile .
```

### Run com docker-compose
```bash
docker compose up -d --build
```

Logs ficam em:
- `yield_optimizer_bot/logs/bot.log`

---

## Deploy em VPS (Linux)
1. Copie o projeto.
2. Crie `.env` com secrets.
3. Ajuste `config.yaml` para seus RPCs.
4. Rode com docker-compose.

---

## Troubleshooting
- **Falha ao conectar RPC**: verifique URLs em `.env`/`config.yaml`.
- **Transaction falha em production**: valide ABI/endereços/rotas (swap) e limites de gas.
- **Bot não rebalanceia**: confira thresholds (min_apy_diff_bps, min_profit_usd), cooldown e risco de gas volatility.

---

## Segurança (Checklist)
- [ ] Private key via `.env` (nunca hardcoded)
- [ ] Dry-run ativo em testes
- [ ] Emergency stop configurado
- [ ] Cooldown entre rebalanceamentos
- [ ] Limites de gas e rate limit ativados


