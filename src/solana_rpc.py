from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


async def _rpc(
    session: aiohttp.ClientSession, method: str, params: list
) -> dict | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with session.post(
            SOLANA_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                logger.warning("Solana RPC %s HTTP %d", method, resp.status)
                return None
            data = await resp.json()
            if "error" in data:
                logger.warning("Solana RPC %s エラー: %s", method, data["error"])
                return None
            return data
    except Exception as e:
        logger.warning("Solana RPC %s 例外: %s", method, e)
        return None


async def get_holder_rates(
    session: aiohttp.ClientSession,
    mint: str,
    dev_wallet: str | None,
    top_n: int = 10,
) -> tuple[Optional[float], Optional[float]]:
    """(dev保有率, 上位top_nホルダー保有率) を返す。取得失敗時は None。"""

    # 総供給量を取得
    supply_data = await _rpc(session, "getTokenSupply", [mint])
    if supply_data is None:
        return None, None
    try:
        total = float(supply_data["result"]["value"]["amount"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if total == 0:
        return None, None

    # dev保有率
    dev_rate: Optional[float] = None
    if dev_wallet:
        accounts_data = await _rpc(
            session,
            "getTokenAccountsByOwner",
            [dev_wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        if accounts_data is not None:
            try:
                dev_amount = sum(
                    float(
                        a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
                    )
                    for a in accounts_data["result"]["value"]
                )
                dev_rate = dev_amount / total
            except (KeyError, TypeError, ValueError):
                pass

    # 上位N保有率
    top_rate: Optional[float] = None
    top_data = await _rpc(session, "getTokenLargestAccounts", [mint])
    if top_data is not None:
        try:
            accounts = top_data["result"]["value"][:top_n]
            top_amount = sum(float(a["amount"]) for a in accounts)
            top_rate = top_amount / total
        except (KeyError, TypeError, ValueError):
            pass

    return dev_rate, top_rate
