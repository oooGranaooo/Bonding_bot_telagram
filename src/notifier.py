import asyncio
import logging
import re
from typing import Callable

from telegram import Bot, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from .config import Config
from .models import GraduatedToken

# Solanaアドレス：base58文字 43〜44文字
_SOLANA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

logger = logging.getLogger(__name__)


def _short_addr(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}"


def _fmt_price(price: float) -> str:
    if price < 0.000001:
        return f"${price:.2e}"
    if price < 0.01:
        return f"${price:.8f}"
    return f"${price:.4f}"


def _fmt_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


class Notifier:
    def __init__(self, bot_token: str, chat_id: str, config: Config):
        self._bot = Bot(token=bot_token)
        self._chat_id = chat_id
        self._config = config

    async def send_dip_alert(self, token: GraduatedToken) -> None:
        dip = token.dip_from_ath()
        if dip is None:
            return

        mins = int(token.minutes_since_graduation())
        dex_url = f"https://dexscreener.com/solana/{token.address}"

        price_change_window: float = self._config.get("dip", "price_change_window_seconds", 0)
        change_rate = token.price_change_rate(price_change_window) if price_change_window > 0 else None
        if change_rate is not None:
            sign = "+" if change_rate >= 0 else ""
            change_line = f"📊 {int(price_change_window)}秒変動率: <b>{sign}{change_rate * 100:.1f}%</b>\n"
        else:
            change_line = ""

        text = (
            "🎓 <b>卒業銘柄 押し目アラート！</b>\n"
            "\n"
            f"🪙 <b>{token.symbol}</b>  {token.name}\n"
            f"📍 <code>{_short_addr(token.address)}</code>\n"
            "\n"
            f"📉 ATH比: <b>-{dip * 100:.1f}%</b>\n"
            f"{change_line}"
            f"💰 現在価格: {_fmt_price(token.current_price)}\n"
            f"📈 ATH: {_fmt_price(token.ath)}\n"
            f"💧 流動性: {_fmt_usd(token.liquidity_usd)}\n"
            f"⏱ 卒業から: {mins}分\n"
            "\n"
            f'🔗 <a href="{dex_url}">DexScreener</a>'
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 CAをコピー", copy_text=CopyTextButton(text=token.address))]
        ])

        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
            logger.info("通知送信: %s", token.symbol)
        except Exception as e:
            logger.error("通知失敗: %s — %s", token.symbol, e)

    async def listen_commands(
        self,
        on_stop_ca: Callable[[str], bool],
        get_active: Callable[[], list[str]],
        poll_timeout: int = 30,
    ) -> None:
        """Telegramのメッセージを監視し、コマンドを処理する。

        /list          : 追跡中CA一覧を表示
        /stop <CA>     : 指定CAの追跡を停止
        <CA> (直接入力): 同上
        """
        offset: int | None = None
        bot_username = (await self._bot.get_me()).username
        _HELP_TEXT = (
            "🤖 <b>コマンド一覧</b>\n"
            "\n"
            "/help               — このヘルプを表示\n"
            "/list               — 追跡中のCA一覧を表示\n"
            "/config             — 現在の設定を表示\n"
            "/set &lt;key&gt; &lt;value&gt;  — 設定を変更\n"
            "/stop <code>&lt;CA&gt;</code>         — 指定CAの追跡を停止\n"
            "<code>&lt;CA&gt;</code> 直接入力         — 同上（/stop 省略可）"
        )
        logger.info("Telegramコマンド受信ループ開始")
        while True:
            try:
                updates = await self._bot.get_updates(
                    offset=offset,
                    timeout=poll_timeout,
                    allowed_updates=["message"],
                )
                for update in updates:
                    offset = update.update_id + 1
                    msg = getattr(update, "message", None)
                    if msg is None:
                        continue
                    text = (msg.text or "").strip()

                    # /help コマンド
                    if text in ("/help", f"/help@{bot_username}"):
                        try:
                            await self._bot.send_message(
                                chat_id=msg.chat.id,
                                text=_HELP_TEXT,
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as e:
                            logger.error("返信送信失敗: %s", e)
                        continue

                    # /list コマンド
                    if text in ("/list", f"/list@{bot_username}"):
                        addrs = get_active()
                        if addrs:
                            lines = "\n".join(
                                f"{i+1}. <code>{a}</code>" for i, a in enumerate(addrs)
                            )
                            reply = f"📋 <b>追跡中CA ({len(addrs)}件)</b>\n\n{lines}"
                        else:
                            reply = "📋 現在追跡中のCAはありません。"
                        try:
                            await self._bot.send_message(
                                chat_id=msg.chat.id,
                                text=reply,
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as e:
                            logger.error("返信送信失敗: %s", e)
                        continue

                    # /config コマンド
                    if text in ("/config", f"/config@{bot_username}"):
                        reply = self._config.format_all()
                        try:
                            await self._bot.send_message(
                                chat_id=msg.chat.id,
                                text=reply,
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as e:
                            logger.error("返信送信失敗: %s", e)
                        continue

                    # /set <key> <value> コマンド
                    set_prefix = f"/set@{bot_username} "
                    if text.startswith("/set ") or text.startswith(set_prefix):
                        body = text[len(set_prefix):] if text.startswith(set_prefix) else text[5:]
                        parts = body.strip().split(maxsplit=1)
                        if len(parts) != 2:
                            reply = "⚠️ 使い方: <code>/set &lt;key&gt; &lt;value&gt;</code>\n例: <code>/set dip.threshold 0.25</code>"
                        else:
                            dot_key, value_str = parts
                            ok, reply = self._config.set(dot_key, value_str)
                            if ok:
                                logger.info("設定変更: %s = %s", dot_key, value_str)
                        try:
                            await self._bot.send_message(
                                chat_id=msg.chat.id,
                                text=reply,
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as e:
                            logger.error("返信送信失敗: %s", e)
                        continue

                    # /stop <CA> または CA 単体に対応
                    if text.startswith("/stop "):
                        text = text[6:].strip()
                    if _SOLANA_RE.match(text):
                        stopped = on_stop_ca(text)
                        if stopped:
                            reply = f"✅ 追跡停止: <code>{text}</code>"
                        else:
                            reply = f"⚠️ 追跡中のCAが見つかりません: <code>{text}</code>"
                        try:
                            await self._bot.send_message(
                                chat_id=msg.chat.id,
                                text=reply,
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as e:
                            logger.error("返信送信失敗: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("get_updates エラー: %s — 5秒後にリトライ", e)
                await asyncio.sleep(5)

    async def send_test_message(self) -> None:
        text = (
            "✅ <b>卒業ボット起動</b>\n"
            "\n"
            + self._config.format_all()
        )
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            logger.info("起動メッセージ送信完了")
        except Exception as e:
            logger.error("起動メッセージ失敗: %s", e)
            raise
