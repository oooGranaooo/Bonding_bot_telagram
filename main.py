import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from src.config import Config
from src.dex_tracker import DexTracker
from src.notifier import Notifier
from src.pump_monitor import PumpMonitor

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    # 環境変数チェック
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.error(".env に TELEGRAM_BOT_TOKEN と TELEGRAM_CHAT_ID を設定してください")
        sys.exit(1)

    config = Config()

    notifier = Notifier(bot_token=bot_token, chat_id=chat_id, config=config)

    # 起動テスト通知（--no-test オプションでスキップ）
    if "--no-test" not in sys.argv:
        await notifier.send_test_message()

    queue: asyncio.Queue = asyncio.Queue()
    monitor = PumpMonitor(queue=queue, config=config)
    tracker = DexTracker(queue=queue, config=config, on_dip=notifier.send_dip_alert)

    logger.info("卒業ボット起動")
    await asyncio.gather(
        monitor.run(),
        tracker.run(),
        notifier.listen_commands(tracker.stop_tracking, tracker.active_addresses),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("ボット停止")
