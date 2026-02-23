from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Coroutine, Any

import aiohttp

from .models import GraduatedToken

logger = logging.getLogger(__name__)

DEXSCREENER_URL = "https://api.dexscreener.com/token-pairs/v1/solana/{address}"
# 300 req/min = 5 req/sec → Semaphore で同時リクエスト数を制限
MAX_CONCURRENT = 5


class DexTracker:
    def __init__(
        self,
        queue: asyncio.Queue,
        config: dict,
        on_dip: Callable[[GraduatedToken], Coroutine[Any, Any, None]],
    ):
        self._queue = queue
        self._config = config
        self._on_dip = on_dip
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._max_tracks: int = config.get("tracking", {}).get("max_tokens", 5)

    def stop_tracking(self, address: str) -> bool:
        """指定アドレスの追跡タスクをキャンセルする。成功時 True を返す。"""
        task = self._active_tasks.get(address)
        if task is None:
            return False
        task.cancel()
        logger.info("手動追跡停止: %s", address)
        return True

    def active_addresses(self) -> list[str]:
        return list(self._active_tasks.keys())

    async def run(self) -> None:
        while True:
            token: GraduatedToken = await self._queue.get()
            if token.address in self._active_tasks:
                logger.info("既に追跡中: %s", token.address)
                self._queue.task_done()
                continue
            if len(self._active_tasks) >= self._max_tracks:
                logger.info(
                    "追跡上限(%d)に達したためスキップ: %s",
                    self._max_tracks, token.symbol,
                )
                self._queue.task_done()
                continue
            task = asyncio.create_task(
                self._track(token), name=f"track-{token.address[:8]}"
            )
            self._active_tasks[token.address] = task
            task.add_done_callback(
                lambda t, addr=token.address: self._active_tasks.pop(addr, None)
            )
            self._queue.task_done()

    async def _track(self, token: GraduatedToken) -> None:
        cfg_dip = self._config["dip"]
        cfg_tracking = self._config["tracking"]
        cfg_filter = self._config["filter"]

        poll_interval: int = cfg_tracking["poll_interval"]
        max_duration: int = cfg_tracking["max_duration"]
        exit_mcap: float = cfg_tracking["exit_mcap_usd"]
        dip_threshold: float = cfg_dip["threshold"]
        min_time: int = cfg_dip["min_time_after_grad"]
        cooldown: int = cfg_dip["cooldown_minutes"]
        min_liquidity: float = cfg_filter["min_liquidity_usd"]
        min_mcap: float = cfg_filter["min_market_cap"]

        deadline = token.graduation_time + timedelta(seconds=max_duration)
        logger.info("追跡開始: %s (%s)", token.symbol, token.address)

        async with aiohttp.ClientSession() as session:
            while datetime.utcnow() < deadline:
                price_data = await self._fetch_price(session, token.address)
                if price_data is not None:
                    price, liquidity, mcap = price_data

                    # 時価総額が閾値を下回ったら追跡終了
                    if mcap > 0 and mcap < exit_mcap:
                        logger.info(
                            "時価総額が閾値を下回ったため追跡終了: %s ($%.0f < $%.0f)",
                            token.symbol, mcap, exit_mcap,
                        )
                        break

                    # フィルター
                    if liquidity < min_liquidity:
                        logger.debug(
                            "%s: 流動性不足 $%.0f < $%.0f",
                            token.symbol, liquidity, min_liquidity,
                        )
                    elif mcap < min_mcap:
                        logger.debug(
                            "%s: 時価総額不足 $%.0f < $%.0f",
                            token.symbol, mcap, min_mcap,
                        )
                    else:
                        token.current_price = price
                        token.liquidity_usd = liquidity
                        token.market_cap = mcap

                        if token.initial_price is None:
                            token.initial_price = price

                        token.update_ath(price)

                        # 押し目判定
                        dip = token.dip_from_ath()
                        mins_since_grad = token.minutes_since_graduation()

                        if dip is not None and mins_since_grad >= min_time:
                            if dip >= dip_threshold:
                                # クールダウンチェック
                                in_cooldown = token.last_notified and (
                                    datetime.utcnow() - token.last_notified
                                    < timedelta(minutes=cooldown)
                                )
                                if in_cooldown:
                                    logger.debug("%s: クールダウン中", token.symbol)
                                else:
                                    token.last_notified = datetime.utcnow()
                                    token.notification_count += 1
                                    logger.info(
                                        "押し目検知: %s ATH比-%.1f%%", token.symbol, dip * 100
                                    )
                                    await self._on_dip(token)
                        elif dip is not None:
                            logger.debug(
                                "%s: 卒業後%.1f分 < %.1f分 (待機中)",
                                token.symbol, mins_since_grad, min_time,
                            )

                await asyncio.sleep(poll_interval)

        logger.info("追跡終了: %s (%s)", token.symbol, token.address)

    async def _fetch_price(
        self, session: aiohttp.ClientSession, address: str
    ) -> tuple[float, float, float] | None:
        url = DEXSCREENER_URL.format(address=address)
        async with self._semaphore:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning("DexScreener HTTP %d: %s", resp.status, address)
                        return None
                    data = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("DexScreener取得失敗: %s — %s", address, e)
                return None

        pairs = data if isinstance(data, list) else data.get("pairs", [])
        if not pairs:
            logger.debug("ペアなし: %s", address)
            return None

        # 流動性が最大のペアを採用
        pair = max(
            pairs,
            key=lambda p: (p.get("liquidity") or {}).get("usd") or 0,
        )

        try:
            price = float(pair["priceUsd"])
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
            mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("パースエラー: %s — %s", address, e)
            return None

        return price, liquidity, mcap
