from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.protocols.base_protocol import PositionSnapshot
from app.services.paper_portfolio import PaperPortfolioLedger
from app.services.position_indexer import PortfolioSnapshot


@dataclass(frozen=True)
class PaperReconciliationResult:
    snapshot: PortfolioSnapshot
    cache_updated: bool
    inconsistencies: list[str] = field(default_factory=list)


class PaperReconciliationService:
    def __init__(self, ledger: PaperPortfolioLedger, wallet_address: str, chain: str):
        self.ledger = ledger
        self.wallet_address = wallet_address
        self.chain = chain

    async def reconcile(self, asset_symbols: list[str]) -> PaperReconciliationResult:
        positions: dict[str, dict[str, PositionSnapshot]] = {}
        totals_by_asset: dict[str, int] = {}
        dominant_protocol_by_asset: dict[str, str | None] = {}
        for asset_symbol in asset_symbols:
            totals_by_asset[asset_symbol] = self.ledger.total_balance(asset_symbol)
            dominant_protocol_by_asset[asset_symbol] = self.ledger.dominant_protocol(asset_symbol)
            wallet_balance = self.ledger.get_wallet_balance(asset_symbol)
            for protocol, protocol_positions in self.ledger.state.protocol_positions.items():
                amount = int(protocol_positions.get(asset_symbol).amount_wei) if asset_symbol in protocol_positions else 0
                positions.setdefault(protocol, {})[asset_symbol] = PositionSnapshot(
                    protocol=protocol,
                    chain=self.chain,
                    asset_symbol=asset_symbol,
                    wallet_balance_wei=wallet_balance,
                    supplied_balance_wei=amount,
                    withdrawable_balance_wei=amount,
                    allowance_wei=2**256 - 1,
                    shares_balance_wei=amount,
                    details={"paper_trading": True},
                )
        snapshot = PortfolioSnapshot(
            wallet_address=self.wallet_address,
            created_at=time.time(),
            positions=positions,
            totals_by_asset=totals_by_asset,
            dominant_protocol_by_asset=dominant_protocol_by_asset,
            details={"paper_trading": True},
        )
        return PaperReconciliationResult(snapshot=snapshot, cache_updated=False, inconsistencies=[])
