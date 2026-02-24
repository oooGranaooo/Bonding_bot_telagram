from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Coroutine, Any

import aiohttp

from .config import Config
from .models import GraduatedToken
from .solana_rpc import get_holder_rates

logger = logging.getLogger(__name__)

DEXSCREENER_URL = "https://api.dexscreener.com/token-pairs/v1/solana/{address}"
# 300 req/min = 5 req/sec → Semaphore で同時リクエスト数を制限
MAX_CONCURRENT = 5
# pump.fun 卒業銘柄が存在する DEX（Raydium 移行 + PumpSwap AMM）
_PUMPFUN_DEX_IDS = frozenset({"raydium", "pumpswap"})


class DexTracker:
    def __init__(
        self,
        queue: asyncio.Queue,
        config: Config,
        on_dip: Callable[[GraduatedToken], Coroutine[Any, Any, None]],
        on_start: Callable[[GraduatedToken], Coroutine[Any, Any, None]] | None = None,
    ):
        self._queue = queue
        self._config = config
        self._on_dip = on_dip
        self._on_start = on_start
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._active_tasks: dict[str, asyncio.Task] = {}

    def _effective_poll(
        self,
        token: GraduatedToken,
        fast: float,
        slow: float,
        exit_mcap: float,
        dip_threshold: float,
    ) -> float:
        """現在の状態に応じてポーリング間隔を返す。
        fast: 急変動期・終了基準近接時
        slow: 安定期
        """
        if slow <= 0 or slow <= fast or not token.price_history:
            return fast

        # Migration直後: slow*3 秒分の履歴がなければ急変動期とみなす
        oldest_ts = token.price_history[0][0]
        if (datetime.utcnow() - oldest_ts).total_seconds() < slow * 3:
            return fast

        # 急変動期: 直近 slow*2 秒間の変動率が 3% 超
        change = token.price_change_rate(slow * 2)
        if change is None or abs(change) > 0.03:
            return fast

        # 追跡終了基準に近い: mcap が exit_mcap の 3倍以内
        if token.market_cap is not None and exit_mcap > 0 and token.market_cap < exit_mcap * 3:
            return fast

        # 押し目閾値の 80% 以上: 通知が近い可能性
        dip = token.dip_from_ath()
        if dip is not None and dip >= dip_threshold * 0.8:
            return fast

        return slow

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
            if len(self._active_tasks) >= self._config.get("tracking", "max_tokens", 5):
                logger.info(
                    "追跡上限(%d)に達したためスキップ: %s",
                    self._max_tracks, token.symbol,
                )
                self._queue.task_done()
                continue
            task = asyncio.create_task(
                self._check_and_track(token), name=f"track-{token.address[:8]}"
            )
            self._active_tasks[token.address] = task
            task.add_done_callback(
                lambda t, addr=token.address: self._active_tasks.pop(addr, None)
            )
            self._queue.task_done()

    async def _check_and_track(self, token: GraduatedToken) -> None:
        """ホルダーフィルターチェック → 通過後に通知 & 追跡開始。"""
        cfg_filter = self._config.data["filter"]
        max_dev_rate: float = cfg_filter.get("max_dev_holding_rate", 0)
        max_top_rate: float = cfg_filter.get("max_top_holding_rate", 0)
        top_n: int = cfg_filter.get("top_holder_count", 10)

        if max_dev_rate > 0 or max_top_rate > 0:
            async with aiohttp.ClientSession() as check_session:
                dev_rate, top_rate = await get_holder_rates(
                    check_session, token.address, token.dev_wallet, top_n
                )
            if max_dev_rate > 0 and dev_rate is not None and dev_rate > max_dev_rate:
                logger.info(
                    "dev保有率が高すぎるため追跡スキップ: %s (%.1f%% > %.1f%%)",
                    token.symbol, dev_rate * 100, max_dev_rate * 100,
                )
                return
            if max_top_rate > 0 and top_rate is not None and top_rate > max_top_rate:
                logger.info(
                    "上位%dホルダー集中率が高すぎるため追跡スキップ: %s (%.1f%% > %.1f%%)",
                    top_n, token.symbol, top_rate * 100, max_top_rate * 100,
                )
                return

        # フィルター通過 → 追跡開始通知 → 追跡
        if self._on_start:
            await self._on_start(token)
        await self._track(token)

    async def _track(self, token: GraduatedToken) -> None:
        cfg_tracking = self._config.data["tracking"]
        max_duration: int = cfg_tracking["max_duration"]

        deadline = token.graduation_time + timedelta(seconds=max_duration)
        logger.info("追跡開始: %s (%s)", token.symbol, token.address)

        async with aiohttp.ClientSession() as session:
            while datetime.utcnow() < deadline:
                # ループごとに最新の設定を読み込む（/set で即時反映）
                cfg_dip = self._config.data["dip"]
                cfg_tracking = self._config.data["tracking"]
                cfg_filter = self._config.data["filter"]

                poll_interval: float = cfg_tracking["poll_interval"]
                poll_interval_slow: float = cfg_tracking.get("poll_interval_slow", 0)
                exit_mcap: float = cfg_tracking["exit_mcap_usd"]
                dip_threshold: float = cfg_dip["threshold"]
                min_time: int = cfg_dip["min_time_after_grad"]
                cooldown: int = cfg_dip["cooldown_minutes"]
                price_change_window: float = cfg_dip.get("price_change_window_seconds", 0)
                price_change_window_end: float = cfg_dip.get("price_change_window_end_seconds", 0)
                price_change_min_rate: float = cfg_dip.get("price_change_min_rate", 0)
                min_liquidity: float = cfg_filter["min_liquidity_usd"]
                min_mcap: float = cfg_filter["min_market_cap"]

                price_data = await self._fetch_price(session, token.address)
                if price_data is not None:
                    price, liquidity, mcap, pair_created_at = price_data

                    # 時価総額が閾値を下回ったら追跡終了
                    if mcap > 0 and mcap < exit_mcap:
                        logger.info(
                            "時価総額が閾値を下回ったため追跡終了: %s ($%.0f < $%.0f)",
                            token.symbol, mcap, exit_mcap,
                        )
                        break

                    # 初回フェッチ: DexScreener の pairCreatedAt でローンチ経過時間チェック
                    min_age_minutes: float = cfg_filter.get("min_age_minutes", 0)
                    if (
                        token.initial_price is None
                        and min_age_minutes > 0
                        and pair_created_at is not None
                    ):
                        elapsed_minutes = (datetime.utcnow() - pair_created_at).total_seconds() / 60
                        if elapsed_minutes < min_age_minutes:
                            logger.info(
                                "ローンチから%.1f分未満のためトラッキング終了: %s (%.1f分)",
                                min_age_minutes, token.symbol, elapsed_minutes,
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

                        token.record_price(price)
                        token.update_ath(price)

                        # ATHティアが設定されていれば dip_threshold を上書き
                        if token.ath is not None:
                            ath_threshold = self._config.get_threshold_for_ath(token.ath)
                            if ath_threshold is not None:
                                dip_threshold = ath_threshold

                        # 時価総額ティアが設定されていれば優先、なければグローバル設定を使用
                        tier = self._config.get_tier_for_mcap(mcap)
                        if tier is not None:
                            eff_window = tier.get("price_change_window_seconds", price_change_window)
                            eff_window_end = tier.get("price_change_window_end_seconds", price_change_window_end)
                            eff_min_rate = tier.get("price_change_min_rate", price_change_min_rate)
                        else:
                            eff_window = price_change_window
                            eff_window_end = price_change_window_end
                            eff_min_rate = price_change_min_rate

                        # 押し目判定
                        dip = token.dip_from_ath()
                        mins_since_grad = token.minutes_since_graduation()

                        if dip is not None and mins_since_grad >= min_time:
                            if dip >= dip_threshold:
                                # 価格変動率チェック（window > 0 のときのみ）
                                change_rate = token.price_change_rate(eff_window, eff_window_end)
                                if eff_window > 0 and eff_min_rate > 0:
                                    if change_rate is None or change_rate >= 0 or abs(change_rate) < eff_min_rate:
                                        logger.debug(
                                            "%s: 価格変動率不足 %.1f%% < %.1f%% (窓=%d〜%d秒前, mcap=$%.0f%s)",
                                            token.symbol,
                                            (change_rate * 100) if change_rate is not None else 0,
                                            eff_min_rate * 100,
                                            eff_window,
                                            eff_window_end,
                                            mcap,
                                            " [ティア適用]" if tier is not None else "",
                                        )
                                        await asyncio.sleep(poll_interval)
                                        continue

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
                                        "押し目検知: %s ATH比-%.1f%% / %d〜%d秒前平均比%.1f%%%s",
                                        token.symbol, dip * 100,
                                        eff_window, eff_window_end,
                                        (change_rate * 100) if change_rate is not None else 0,
                                        " [ティア適用]" if tier is not None else "",
                                    )
                                    asyncio.create_task(self._on_dip(token))

                                    max_notifications: int = cfg_tracking.get("max_notifications", 0)
                                    if max_notifications > 0 and token.notification_count >= max_notifications:
                                        logger.info(
                                            "通知上限(%d回)に達したため追跡終了: %s",
                                            max_notifications, token.symbol,
                                        )
                                        return
                        elif dip is not None:
                            logger.debug(
                                "%s: 卒業後%.1f分 < %.1f分 (待機中)",
                                token.symbol, mins_since_grad, min_time,
                            )

                eff_poll = self._effective_poll(
                    token, poll_interval, poll_interval_slow, exit_mcap, dip_threshold
                )
                await asyncio.sleep(eff_poll)

        logger.info("追跡終了: %s (%s)", token.symbol, token.address)

    async def _fetch_price(
        self, session: aiohttp.ClientSession, address: str
    ) -> tuple[float, float, float, datetime | None] | None:
        url = DEXSCREENER_URL.format(address=address)
        data = None
        retry_wait = 0
        async with self._semaphore:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 429:
                        retry_wait = int(resp.headers.get("Retry-After", 5))
                        logger.warning("DexScreener レート制限 — %d秒後リトライ: %s", retry_wait, address)
                    elif resp.status != 200:
                        logger.warning("DexScreener HTTP %d: %s", resp.status, address)
                    else:
                        data = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("DexScreener取得失敗: %s — %s", address, e)
                return None

        if retry_wait > 0:
            await asyncio.sleep(retry_wait)
            return None
        if data is None:
            return None

        pairs = data if isinstance(data, list) else data.get("pairs", [])
        if not pairs:
            logger.debug("ペアなし: %s", address)
            return None

        # pump.fun 関連 DEX のペアのみ対象
        pairs = [p for p in pairs if p.get("dexId") in _PUMPFUN_DEX_IDS]
        if not pairs:
            logger.debug("pump.fun ペアなし: %s", address)
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
            created_ms = pair.get("pairCreatedAt")
            pair_created_at = datetime.utcfromtimestamp(created_ms / 1000) if created_ms else None
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("パースエラー: %s — %s", address, e)
            return None

        return price, liquidity, mcap, pair_created_at
