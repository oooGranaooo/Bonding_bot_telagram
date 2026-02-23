from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path("config.yaml")

# dot-key → (section, field, cast, description)
EDITABLE_KEYS: dict[str, tuple[str, str, type, str]] = {
    "dip.threshold":                    ("dip",      "threshold",                    float, "ATH比下落閾値 (0〜1)"),
    "dip.min_time_after_grad":          ("dip",      "min_time_after_grad",          float, "卒業後判定開始 (分)"),
    "dip.cooldown_minutes":             ("dip",      "cooldown_minutes",             int,   "通知クールダウン (分)"),
    "dip.price_change_window_seconds":  ("dip",      "price_change_window_seconds",  int,   "価格変動率ウィンドウ (秒, 0=無効)"),
    "dip.price_change_min_rate":        ("dip",      "price_change_min_rate",        float, "価格変動率閾値 (正の値で設定, 例: 0.1=10%。上昇・下落どちらも絶対値で判定)"),
    "tracking.poll_interval":   ("tracking", "poll_interval",       int,   "価格チェック間隔 (秒)"),
    "tracking.max_duration":    ("tracking", "max_duration",        int,   "追跡最大時間 (秒)"),
    "tracking.max_tokens":      ("tracking", "max_tokens",          int,   "同時追跡上限数"),
    "tracking.exit_mcap_usd":   ("tracking", "exit_mcap_usd",       float, "追跡終了時価総額 (USD)"),
    "filter.min_liquidity_usd": ("filter",   "min_liquidity_usd",   float, "最低流動性 (USD)"),
    "filter.min_market_cap":    ("filter",   "min_market_cap",      float, "最低時価総額 (USD)"),
    "filter.min_age_minutes":   ("filter",   "min_age_minutes",     float, "ローンチ後最低経過時間 (分)"),
}


class Config:
    def __init__(self, path: str | Path = CONFIG_PATH) -> None:
        self._path = Path(path)
        self._data: dict = {}
        self.reload()

    def reload(self) -> None:
        with self._path.open(encoding="utf-8") as f:
            self._data = yaml.safe_load(f)

    @property
    def data(self) -> dict:
        return self._data

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self._data.get(section, {}).get(key, default)

    def set(self, dot_key: str, value_str: str) -> tuple[bool, str]:
        """dot_key (例: "dip.threshold") と文字列値を受け取り更新・保存する。
        Returns (success, message)."""
        if dot_key not in EDITABLE_KEYS:
            keys_list = "\n".join(f"  <code>{k}</code>" for k in EDITABLE_KEYS)
            return False, f"⚠️ 不明なキー: <code>{dot_key}</code>\n利用可能なキー:\n{keys_list}"

        section, field, cast, _ = EDITABLE_KEYS[dot_key]
        try:
            value = cast(value_str)
        except (ValueError, TypeError):
            return False, (
                f"⚠️ 型エラー: <code>{dot_key}</code> には "
                f"<b>{cast.__name__}</b> 型の値を指定してください"
            )

        old = self._data.get(section, {}).get(field, "?")
        self._data.setdefault(section, {})[field] = value
        self._save()
        return True, f"✅ <code>{dot_key}</code>: <b>{old}</b> → <b>{value}</b>"

    def _save(self) -> None:
        with self._path.open("w", encoding="utf-8") as f:
            yaml.dump(self._data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    def get_tier_for_mcap(self, mcap: float) -> dict | None:
        """時価総額に対応するティアを返す。マッチしなければ None。"""
        tiers = self._data.get("dip", {}).get("mcap_tiers", [])
        for tier in tiers:
            mcap_min = tier.get("mcap_min", 0)
            mcap_max = tier.get("mcap_max", float("inf"))
            if mcap_min <= mcap < mcap_max:
                return tier
        return None

    def add_mcap_tier(
        self, mcap_min: float, mcap_max: float, window_sec: int, min_rate: float
    ) -> tuple[bool, str]:
        """ティアを追加して mcap_min 昇順にソートして保存する。"""
        dip = self._data.setdefault("dip", {})
        tiers = dip.setdefault("mcap_tiers", [])
        tiers.append(
            {
                "mcap_min": mcap_min,
                "mcap_max": mcap_max,
                "price_change_window_seconds": window_sec,
                "price_change_min_rate": min_rate,
            }
        )
        tiers.sort(key=lambda t: t.get("mcap_min", 0))
        self._save()
        max_str = "∞" if mcap_max == float("inf") else f"${mcap_max:,.0f}"
        return True, (
            f"✅ ティア追加\n"
            f"  時価総額: ${mcap_min:,.0f} 〜 {max_str}\n"
            f"  変動率ウィンドウ: {window_sec}秒\n"
            f"  最低変動率: {min_rate * 100:.1f}%"
        )

    def remove_mcap_tier(self, index: int) -> tuple[bool, str]:
        """1-indexed でティアを削除して保存する。"""
        tiers = self._data.get("dip", {}).get("mcap_tiers", [])
        if not tiers:
            return False, "⚠️ ティアが登録されていません"
        if index < 1 or index > len(tiers):
            return False, f"⚠️ インデックスが範囲外: {index} (有効範囲: 1〜{len(tiers)})"
        tiers.pop(index - 1)
        self._save()
        return True, f"✅ {index}番目のティアを削除しました"

    def format_tiers(self) -> str:
        tiers = self._data.get("dip", {}).get("mcap_tiers", [])
        if not tiers:
            return (
                "📊 <b>時価総額別変動率ティア</b>\n"
                "\n"
                "未設定です。\n"
                "追加: <code>/volatier add &lt;min&gt; &lt;max&gt; &lt;秒&gt; &lt;変動率%&gt;</code>\n"
                "例: <code>/volatier add 10000 50000 5 10</code>"
            )
        lines = ["📊 <b>時価総額別変動率ティア</b>\n"]
        for i, tier in enumerate(tiers, 1):
            mcap_min = tier.get("mcap_min", 0)
            mcap_max = tier.get("mcap_max", float("inf"))
            window = tier.get("price_change_window_seconds", 0)
            rate = tier.get("price_change_min_rate", 0)
            max_str = "∞" if mcap_max == float("inf") else f"${mcap_max:,.0f}"
            lines.append(
                f"  {i}. ${mcap_min:,.0f}〜{max_str}"
                f" | {window}秒 | {rate * 100:.1f}%以上"
            )
        lines.append("\n追加: <code>/volatier add &lt;min&gt; &lt;max&gt; &lt;秒&gt; &lt;変動率%&gt;</code>")
        lines.append("例: <code>/volatier add 10000 50000 5 10</code>")
        lines.append("削除: <code>/volatier del &lt;番号&gt;</code>")
        return "\n".join(lines)

    def get_threshold_for_ath(self, ath: float) -> float | None:
        """ATH価格に対応する下落閾値を返す。マッチしなければ None。"""
        tiers = self._data.get("dip", {}).get("ath_tiers", [])
        for tier in tiers:
            ath_min = tier.get("ath_min", 0)
            ath_max = tier.get("ath_max", float("inf"))
            if ath_min <= ath < ath_max:
                return tier.get("threshold")
        return None

    def add_ath_tier(
        self, ath_min: float, ath_max: float, threshold: float
    ) -> tuple[bool, str]:
        """ATHティアを追加して ath_min 昇順にソートして保存する。"""
        dip = self._data.setdefault("dip", {})
        tiers = dip.setdefault("ath_tiers", [])
        tiers.append({"ath_min": ath_min, "ath_max": ath_max, "threshold": threshold})
        tiers.sort(key=lambda t: t.get("ath_min", 0))
        self._save()
        max_str = "∞" if ath_max == float("inf") else f"${ath_max:g}"
        return True, (
            f"✅ ATHティア追加\n"
            f"  ATH: ${ath_min:g} 〜 {max_str}\n"
            f"  下落閾値: {threshold * 100:.1f}%"
        )

    def remove_ath_tier(self, index: int) -> tuple[bool, str]:
        """1-indexed でATHティアを削除して保存する。"""
        tiers = self._data.get("dip", {}).get("ath_tiers", [])
        if not tiers:
            return False, "⚠️ ATHティアが登録されていません"
        if index < 1 or index > len(tiers):
            return False, f"⚠️ インデックスが範囲外: {index} (有効範囲: 1〜{len(tiers)})"
        tiers.pop(index - 1)
        self._save()
        return True, f"✅ {index}番目のATHティアを削除しました"

    def format_ath_tiers(self) -> str:
        tiers = self._data.get("dip", {}).get("ath_tiers", [])
        if not tiers:
            return (
                "📊 <b>ATH別下落閾値ティア</b>\n"
                "\n"
                "未設定です。\n"
                "追加: <code>/athtier add &lt;min&gt; &lt;max&gt; &lt;閾値%&gt;</code>\n"
                "例: <code>/athtier add 0 0.001 20</code>"
            )
        lines = ["📊 <b>ATH別下落閾値ティア</b>\n"]
        for i, tier in enumerate(tiers, 1):
            ath_min = tier.get("ath_min", 0)
            ath_max = tier.get("ath_max", float("inf"))
            threshold = tier.get("threshold", 0)
            max_str = "∞" if ath_max == float("inf") else f"${ath_max:g}"
            lines.append(
                f"  {i}. ATH: ${ath_min:g}〜{max_str}"
                f" | 閾値: {threshold * 100:.1f}%以上"
            )
        lines.append("\n追加: <code>/athtier add &lt;min&gt; &lt;max&gt; &lt;閾値%&gt;</code>")
        lines.append("例: <code>/athtier add 0 0.001 20</code>")
        lines.append("削除: <code>/athtier del &lt;番号&gt;</code>")
        return "\n".join(lines)

    def format_all(self) -> str:
        lines = ["⚙️ <b>現在の設定（グローバル）</b>"]
        lines.append("<i>※ ティア設定がある場合はそちらが優先されます</i>")
        current_section: str | None = None
        for dot_key, (section, field, _, desc) in EDITABLE_KEYS.items():
            if section != current_section:
                lines.append(f"\n<b>[{section}]</b>")
                current_section = section
            value = self._data.get(section, {}).get(field, "?")
            lines.append(f"  <code>{dot_key}</code> = <b>{value}</b>  <i>{desc}</i>")
        lines.append("\n変更: /set &lt;key&gt; &lt;value&gt;")
        lines.append("例: <code>/set dip.threshold 0.25</code>")
        return "\n".join(lines)
