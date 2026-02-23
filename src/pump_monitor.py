import asyncio
import json
import logging
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosedError, WebSocketException

from .models import GraduatedToken

logger = logging.getLogger(__name__)

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
RECONNECT_BASE = 2
RECONNECT_MAX = 60


class PumpMonitor:
    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

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

        symbol = data.get("symbol", "UNKNOWN")
        name = data.get("name", symbol)

        token = GraduatedToken(
            address=mint,
            symbol=symbol,
            name=name,
            graduation_time=datetime.utcnow(),
        )
        logger.info("卒業検知: %s (%s) — %s", symbol, name, mint)
        await self._queue.put(token)
