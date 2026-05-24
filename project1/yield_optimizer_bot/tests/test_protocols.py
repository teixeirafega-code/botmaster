from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from web3 import Web3

from app.protocols.aave import AaveProtocol
from app.protocols.beefy import BeefyProtocol
from app.protocols.compound import CompoundProtocol
from app.protocols.curve import CurveProtocol
from app.protocols.stargate import StargateProtocol


@pytest.mark.asyncio
async def test_aave_fetch_apy_reads_liquidity_rate():
    reserve_data = (0, 0, 0, 0, 0, 50_000_000_000_000_000_000_000_000, 0, 0, 0, 0, 0, 0)
    contract = SimpleNamespace(
        functions=SimpleNamespace(
            getReserveData=lambda _asset: SimpleNamespace(call=MagicMock(return_value=reserve_data))
        )
    )
    w3 = MagicMock()
    w3.eth.contract.return_value = contract

    protocol = AaveProtocol(chain="Polygon", w3=w3, wallet_address="0x1111111111111111111111111111111111111111")
    quote = await protocol.fetch_apy("USDC")

    assert quote.protocol == "aave"
    assert quote.asset_symbol == "USDC"
    assert quote.raw_apy > 0
    assert quote.details["liquidity_rate_ray"] == reserve_data[5]


def test_aave_build_plans_encode_pool_calls():
    protocol = AaveProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    deposit_tx = protocol.build_deposit_plan("USDC", 1_000_000)
    withdraw_tx = protocol.build_withdraw_plan("USDC", 1_000_000)

    assert deposit_tx["to"] == protocol._POOL_ADDRESS
    assert withdraw_tx["to"] == protocol._POOL_ADDRESS
    assert deposit_tx["data"].startswith("0x")
    assert withdraw_tx["data"].startswith("0x")


@pytest.mark.asyncio
async def test_compound_fetch_apy_uses_utilization_and_supply_rate():
    contract = SimpleNamespace(
        functions=SimpleNamespace(
            getUtilization=lambda: SimpleNamespace(call=MagicMock(return_value=800_000_000_000_000_000)),
            getSupplyRate=lambda _util: SimpleNamespace(call=MagicMock(return_value=951_293_759)),
        )
    )
    w3 = MagicMock()
    w3.eth.contract.return_value = contract

    protocol = CompoundProtocol(chain="Polygon", w3=w3, wallet_address="0x1111111111111111111111111111111111111111")
    quote = await protocol.fetch_apy("USDT")

    assert quote.protocol == "compound"
    assert quote.asset_symbol == "USDT"
    assert quote.raw_apy > 0
    assert quote.details["utilization"] == 800_000_000_000_000_000


def test_compound_build_plans_encode_comet_calls():
    protocol = CompoundProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    deposit_tx = protocol.build_deposit_plan("USDT", 2_000_000)
    withdraw_tx = protocol.build_withdraw_plan("USDT", 2_000_000)

    assert deposit_tx["to"] == protocol._MARKETS["USDT"]["comet_address"]
    assert withdraw_tx["to"] == protocol._MARKETS["USDT"]["comet_address"]
    assert deposit_tx["data"].startswith("0x")
    assert withdraw_tx["data"].startswith("0x")


@pytest.mark.asyncio
async def test_curve_fetch_apy_combines_pool_and_volume_payloads():
    protocol = CurveProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    pools_payload = {
        "data": {
            "poolData": [
                {
                    "id": "1",
                    "address": "0x445FE580eF8d70FF569aB36e80c647af338db351",
                    "name": "Curve.fi amDAI/amUSDC/amUSDT",
                    "lpTokenAddress": "0xE7a24EF0C5e95Ffb0f6684b813A78F2a3AD7D171",
                    "gaugeAddress": "0x20759f567bb3ecdb55c817c9a1d13076ab215edc",
                    "usdTotal": 123456.78,
                }
            ]
        }
    }
    volume_payload = {
        "data": {
            "pools": [
                {
                    "address": "0x445FE580eF8d70FF569aB36e80c647af338db351",
                    "latestDailyApy": 4.2,
                }
            ]
        }
    }

    async def fake_get_json(url: str):
        return pools_payload if "getPools" in url else volume_payload

    protocol._get_json = fake_get_json  # type: ignore[method-assign]
    quote = await protocol.fetch_apy("USDC")

    assert quote.protocol == "curve"
    assert quote.asset_symbol == "USDC"
    assert quote.raw_apy == pytest.approx(0.042)
    assert quote.details["pool_address"] == protocol._POOL_ADDRESS


@pytest.mark.asyncio
async def test_curve_fetch_apy_falls_back_to_pool_payload_when_volume_api_omits_apy():
    protocol = CurveProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    pools_payload = {
        "data": {
            "poolData": [
                {
                    "id": "1",
                    "address": "0x445FE580eF8d70FF569aB36e80c647af338db351",
                    "name": "Curve.fi amDAI/amUSDC/amUSDT",
                    "latestDailyApy": 3.6,
                }
            ]
        }
    }
    volume_payload = {"data": {"pools": [{"address": "0x445FE580eF8d70FF569aB36e80c647af338db351"}]}}

    async def fake_get_json(url: str):
        return pools_payload if "getPools" in url else volume_payload

    protocol._get_json = fake_get_json  # type: ignore[method-assign]
    quote = await protocol.fetch_apy("USDC")

    assert quote.protocol == "curve"
    assert quote.raw_apy == pytest.approx(0.036)


@pytest.mark.asyncio
async def test_curve_fetch_apy_reads_percent_fields_from_volume_api():
    protocol = CurveProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    pools_payload = {
        "data": {
            "poolData": [
                {
                    "id": "1",
                    "address": "0x445FE580eF8d70FF569aB36e80c647af338db351",
                    "name": "Curve.fi amDAI/amUSDC/amUSDT",
                }
            ]
        }
    }
    volume_payload = {
        "data": {
            "pools": [
                {
                    "address": "0x445FE580eF8d70FF569aB36e80c647af338db351",
                    "latestDailyApyPcent": 0,
                    "latestWeeklyApyPcent": 1.01,
                }
            ]
        }
    }

    async def fake_get_json(url: str):
        return pools_payload if "getPools" in url else volume_payload

    protocol._get_json = fake_get_json  # type: ignore[method-assign]
    quote = await protocol.fetch_apy("USDC")

    assert quote.protocol == "curve"
    assert quote.raw_apy == pytest.approx(0.0101)


@pytest.mark.asyncio
async def test_curve_discover_position_skips_pool_balance_lookup_when_no_lp_balance():
    token_contract = SimpleNamespace(
        functions=SimpleNamespace(
            balanceOf=lambda _user: SimpleNamespace(call=MagicMock(return_value=123)),
            allowance=lambda _user, _spender: SimpleNamespace(call=MagicMock(return_value=456)),
        )
    )
    lp_contract = SimpleNamespace(
        functions=SimpleNamespace(
            balanceOf=lambda _user: SimpleNamespace(call=MagicMock(return_value=0)),
            totalSupply=lambda: SimpleNamespace(call=MagicMock(return_value=1_000_000)),
        )
    )
    failing_pool = SimpleNamespace(
        functions=SimpleNamespace(
            balances=lambda _index: SimpleNamespace(call=MagicMock(side_effect=RuntimeError("should not be called")))
        )
    )
    w3 = MagicMock()
    w3.eth.contract.side_effect = [failing_pool, failing_pool, lp_contract, token_contract, token_contract]

    protocol = CurveProtocol(
        chain="Polygon",
        w3=w3,
        wallet_address="0x1111111111111111111111111111111111111111",
    )
    position = await protocol.discover_position("0x1111111111111111111111111111111111111111", "USDC")

    assert position.wallet_balance_wei == 123
    assert position.allowance_wei == 456
    assert position.supplied_balance_wei == 0
    assert position.withdrawable_balance_wei == 0


def test_curve_build_plans_encode_liquidity_calls():
    protocol = CurveProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    protocol.pool = SimpleNamespace(
        functions=SimpleNamespace(
            calc_token_amount=lambda _amounts, _is_deposit: SimpleNamespace(call=MagicMock(return_value=1_000_000)),
            add_liquidity=lambda _amounts, _min_mint, _use_underlying: SimpleNamespace(
                _encode_transaction_data=lambda: "0xdeadbeef"
            ),
            remove_liquidity_imbalance=lambda _amounts, _max_burn, _use_underlying: SimpleNamespace(
                _encode_transaction_data=lambda: "0xcafebabe"
            ),
        )
    )

    deposit_tx = protocol.build_deposit_plan("USDT", 3_000_000)
    withdraw_tx = protocol.build_withdraw_plan("USDT", 3_000_000)

    assert deposit_tx["data"] == "0xdeadbeef"
    assert withdraw_tx["data"] == "0xcafebabe"
    assert deposit_tx["to"] == protocol._POOL_ADDRESS
    assert withdraw_tx["to"] == protocol._POOL_ADDRESS


@pytest.mark.asyncio
async def test_beefy_fetch_apy_selects_polygon_usdc_vault():
    protocol = BeefyProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    apy_payload = {
        "aave-usdc": 0.037144355563767335,
        "compound-polygon-usdc": 0.0125,
    }
    vaults_payload = [
        {
            "id": "aave-usdc-eol",
            "network": "polygon",
            "tokenAddress": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
            "earnContractAddress": "0xE71f3C11D4535a7F8c5FB03FDA57899B2C9c721F",
            "status": "eol",
            "assets": ["pUSDCe"],
            "name": "USDC.e",
        },
        {
            "id": "compound-polygon-usdc",
            "network": "polygon",
            "tokenAddress": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
            "earnContractAddress": "0x86F371838A321F92237DaD7b8DA5c76d2c084934",
            "status": "eol",
            "assets": ["pUSDCe"],
            "name": "USDC.e",
        },
    ]

    async def fake_get_json(url: str):
        return apy_payload if "apy" in url else vaults_payload

    protocol._get_json = fake_get_json  # type: ignore[method-assign]
    quote = await protocol.fetch_apy("USDC")

    assert quote.protocol == "beefy"
    assert quote.asset_symbol == "USDC"
    assert quote.raw_apy == pytest.approx(0.037144355563767335)
    assert quote.details["vault_id"] == "aave-usdc-eol"


def test_beefy_build_plans_encode_vault_calls():
    protocol = BeefyProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    deposit_tx = protocol.build_deposit_plan("USDC", 1_000_000)
    withdraw_tx = protocol.build_withdraw_plan("USDC", 1_000_000)

    assert deposit_tx["to"] == Web3.to_checksum_address("0xE71f3C11D4535a7F8c5FB03FDA57899B2C9c721F")
    assert withdraw_tx["to"] == Web3.to_checksum_address("0xE71f3C11D4535a7F8c5FB03FDA57899B2C9c721F")
    assert deposit_tx["data"].startswith("0x")
    assert withdraw_tx["data"].startswith("0x")


@pytest.mark.asyncio
async def test_stargate_fetch_apy_reads_usdc_pool_from_payload():
    protocol = StargateProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    payload = {
        "pools": [
            {
                "poolId": 1,
                "token": "USDC",
                "tokenAddress": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                "poolAddress": "0x1205f31718499dBf1fCa446663B532Ef87481fe1",
                "apr": 4.25,
            }
        ]
    }

    async def fake_get_json(_url: str):
        return payload

    protocol._get_json = fake_get_json  # type: ignore[method-assign]
    quote = await protocol.fetch_apy("USDC")

    assert quote.protocol == "stargate"
    assert quote.asset_symbol == "USDC"
    assert quote.raw_apy == pytest.approx(0.0425)
    assert quote.details["pool_address"] == protocol._POOL_ADDRESS


@pytest.mark.asyncio
async def test_stargate_fetch_apy_falls_back_to_zero_when_endpoint_fails():
    protocol = StargateProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    async def failing_get_json(_url: str):
        raise RuntimeError("404")

    protocol._get_json = failing_get_json  # type: ignore[method-assign]
    quote = await protocol.fetch_apy("USDC")

    assert quote.protocol == "stargate"
    assert quote.raw_apy == 0.0
    assert quote.details["source"] == "fallback_zero"


def test_stargate_build_plans_encode_router_calls():
    protocol = StargateProtocol(
        chain="Polygon",
        w3=Web3(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )

    deposit_tx = protocol.build_deposit_plan("USDC", 2_000_000)
    withdraw_tx = protocol.build_withdraw_plan("USDC", 2_000_000)

    assert deposit_tx["to"] == protocol._ROUTER_ADDRESS
    assert withdraw_tx["to"] == protocol._ROUTER_ADDRESS
    assert deposit_tx["data"].startswith("0x")
    assert withdraw_tx["data"].startswith("0x")
