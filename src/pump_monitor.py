import asyncio
import json
import logging
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosedError, WebSocketException

from .config import Config
from .models import GraduatedToken

logger = logging.getLogger(__name__)

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
RECONNECT_BASE = 2
RECONNECT_MAX = 60


class PumpMonitor:
    def __init__(self, queue: asyncio.Queue, config: Config):
        self._queue = queue
        self._config = config

    async def run(self) -> None:
        backoff = RECONNECT_BASE
        while True:
            try:
                await self._connect()
                backoff = RECONNECT_BASE
            except (ConnectionClosedError, WebSocketException, OSError) as e:
                logger.warning("WebSocket切断: %s — %d秒後に再接続", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("予期しないエラー: %s — %d秒後に再接続", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    async def _connect(self) -> None:
        logger.info("pumpportal.fun に接続中…")
        async with websockets.connect(PUMPPORTAL_WS) as ws:
            logger.info("WebSocket接続完了")
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            logger.info("subscribeMigration 送信完了")
            async for raw in ws:
                await self._handle(raw)

    async def _handle(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("JSONパース失敗: %s", raw[:200])
            return

        # pumpportal の Migration イベント形式
        mint = data.get("mint") or data.get("address") or data.get("tokenAddress")
        if not mint:
            logger.debug("mint なし: %s", data)
            return
        logger.debug("migrationイベント raw keys: %s", list(data.keys()))

        # ローンチから min_age_minutes 未満のトークンはスキップ
        min_age: float = self._config.get("filter", "min_age_minutes", 3)
        ts = data.get("timestamp")
        if ts is not None and min_age > 0:
            # ミリ秒の場合は秒に変換
            if ts > 1e12:
                ts = ts / 1000
            launch_time = datetime.utcfromtimestamp(ts)
            elapsed_minutes = (datetime.utcnow() - launch_time).total_seconds() / 60
            if elapsed_minutes < min_age:
                logger.debug(
                    "ローンチから%.1f分未満のためスキップ: %s (%.1f分)", min_age, mint, elapsed_minutes
                )
                return

        symbol = data.get("symbol", "UNKNOWN")
        name = data.get("name", symbol)
        dev_wallet = data.get("traderPublicKey")

        token = GraduatedToken(
            address=mint,
            symbol=symbol,
            name=name,
            graduation_time=datetime.utcnow(),
            dev_wallet=dev_wallet,
        )
        logger.info("卒業検知: %s (%s) — %s", symbol, name, mint)
        await self._queue.put(token)
