from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


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
