# TODO — Yield Optimizer Bot

- [ ] Criar estrutura do projeto conforme blueprint
- [ ] Criar `requirements.txt`, `Dockerfile`, `docker-compose.yml`
- [ ] Criar `config.yaml` (com defaults seguros)
- [ ] Criar `.env.example`
- [ ] Implementar `app/utils/logger.py` (logging + rotating handlers)
- [ ] Implementar `app/utils/retry.py`
- [ ] Implementar `app/config/settings.py` (parser/validação)
- [ ] Implementar `app/blockchain/web3_client.py`
- [ ] Implementar `app/blockchain/wallet.py`
- [ ] Implementar `app/blockchain/gas_estimator.py` (EIP-1559 + congestion)
- [ ] Implementar `app/blockchain/transaction_manager.py` (dry-run, validação, envio)
- [ ] Implementar `app/protocols/base_protocol.py` (BaseProtocol abstrata)
- [ ] Implementar `app/protocols/aave.py`
- [ ] Implementar `app/protocols/compound.py`
- [ ] Implementar `app/protocols/curve.py`
- [ ] Implementar `app/services/apy_aggregator.py` (cache + retry + timeouts)
- [ ] Implementar `app/strategies/yield_strategy.py` (cálculo de lucro líquido, slippage, cooldown)
- [ ] Implementar `app/services/portfolio_manager.py` (state + holdings)
- [ ] Implementar `app/services/risk_manager.py` (rate limiting, volatilidade de gas, emergency stop)
- [ ] Implementar `app/services/rebalance_engine.py` (withdraw/swap/deposit)
- [ ] Implementar `app/scheduler.py` (APScheduler jobs + healthcheck + heartbeat)
- [ ] Implementar `app/main.py` (bootstrap)
- [ ] Criar `app/data/state.json` (seed)
- [ ] Criar testes unitários com mocks
- [ ] Atualizar `README.md` completo (instalação, .env, deploy VPS, Docker, troubleshooting)
- [ ] Rodar testes/lint básico (opcional) e garantir execução em paper trading

