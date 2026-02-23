from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# 価格履歴の最大保持時間（秒）
_MAX_HISTORY_SECONDS = 600


@dataclass
class GraduatedToken:
    address: str
    symbol: str
    name: str
    graduation_time: datetime

    initial_price: Optional[float] = None
    ath: Optional[float] = None
    ath_time: Optional[datetime] = None
    current_price: Optional[float] = None
    liquidity_usd: Optional[float] = None
    market_cap: Optional[float] = None

    last_notified: Optional[datetime] = None
    notification_count: int = 0

    # (timestamp, price) のリスト。record_price() で追記する
    price_history: list = field(default_factory=list)

    def minutes_since_graduation(self) -> float:
        return (datetime.utcnow() - self.graduation_time).total_seconds() / 60

    def dip_from_ath(self) -> Optional[float]:
        if self.ath and self.current_price and self.ath > 0:
            return (self.ath - self.current_price) / self.ath
        return None

    def update_ath(self, price: float) -> None:
        if self.ath is None or price > self.ath:
            self.ath = price
            self.ath_time = datetime.utcnow()

    def record_price(self, price: float) -> None:
        """現在価格を履歴に追記し、古いエントリを削除する。"""
        now = datetime.utcnow()
        self.price_history.append((now, price))
        cutoff = now - timedelta(seconds=_MAX_HISTORY_SECONDS)
        # 古いエントリを先頭から削除
        while self.price_history and self.price_history[0][0] < cutoff:
            self.price_history.pop(0)

    def price_change_rate(self, from_seconds: float, to_seconds: float = 0) -> Optional[float]:
        """[from_seconds秒前 〜 to_seconds秒前] の平均価格と現在価格の変動率 (正=上昇, 負=下落)。
        to_seconds=0 のとき現在時刻を終端とする。
        対象期間内に価格がない場合は None を返す。"""
        if not self.price_history or self.current_price is None or from_seconds <= 0:
            return None
        now = datetime.utcnow()
        cutoff_old = now - timedelta(seconds=from_seconds)
        cutoff_new = now - timedelta(seconds=to_seconds) if to_seconds > 0 else now
        prices = [p for ts, p in self.price_history if cutoff_old <= ts <= cutoff_new]
        if not prices:
            return None
        avg = sum(prices) / len(prices)
        if avg == 0:
            return None
        return (self.current_price - avg) / avg
