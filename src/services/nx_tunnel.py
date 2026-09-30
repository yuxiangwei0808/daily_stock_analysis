"""NX tunnel context for stock reports (the user's own moomoo indicator; descriptive only).

NX (``moomoo_indicators.nx``) draws two tunnels: EMA(high)/EMA(low) over 26 bars
(fast) and over 89 bars (slow). Reports show where the price sits relative to
them and the tunnel edges as reference levels. The backtest in
``docs/strategy-backtest.md`` found no edge in NX crossings on their own, so the
tunnel never changes a report's score or action; the prompt says so.

Bars come from Yahoo (two years, so the 89-bar EMA has settled to the values on
the moomoo chart) and are cached briefly per ticker. US tickers only.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600
_cache: Dict[str, tuple] = {}
_lock = threading.Lock()


def describe(bars: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Tunnel levels and the price's position on the last bar; None without enough history."""
    from src.services.trade_desk.moomoo_indicators import NX_MIN_BARS, nx, tunnel_state, tunnel_structure
    if len(bars) < NX_MIN_BARS:
        return None
    lines = nx(bars)
    close = float(bars[-1]["close"])
    fast = {"top": lines["A"][-1], "bottom": lines["B"][-1]}
    slow = {"top": lines["A1"][-1], "bottom": lines["B1"][-1]}
    fast["state"] = tunnel_state(close, fast["top"], fast["bottom"])
    slow["state"] = tunnel_state(close, slow["top"], slow["bottom"])
    previous_close = float(bars[-2]["close"])
    previous_fast = tunnel_state(previous_close, lines["A"][-2], lines["B"][-2])
    previous_slow = tunnel_state(previous_close, lines["A1"][-2], lines["B1"][-2])
    structure = tunnel_structure(lines)
    changes = []
    if fast["state"] != previous_fast:
        changes.append(f"fast:{previous_fast}->{fast['state']}")
    if slow["state"] != previous_slow:
        changes.append(f"slow:{previous_slow}->{slow['state']}")
    return {
        "as_of": str(bars[-1]["date"])[:10], "close": round(close, 4),
        "fast": {key: (round(value, 4) if isinstance(value, float) else value) for key, value in fast.items()},
        "slow": {key: (round(value, 4) if isinstance(value, float) else value) for key, value in slow.items()},
        "structure": structure, "changes_today": changes,
        "to_fast_bottom_pct": round((close / fast["bottom"] - 1) * 100, 2),
        "to_slow_bottom_pct": round((close / slow["bottom"] - 1) * 100, 2),
    }


def for_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """NX context for a US ticker from two years of daily bars; None when unavailable."""
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return None
    now = time.time()
    with _lock:
        cached = _cache.get(symbol)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
    try:
        from src.services.trade_desk.trend import download_bars
        bars = (download_bars([symbol], period="2y") or {}).get(symbol.replace(".", "-"))
        result = describe(bars or [])
    except Exception as exc:  # a missing tunnel never blocks a report
        logger.info("NX tunnel unavailable for %s: %s", symbol, type(exc).__name__)
        result = None
    with _lock:
        _cache[symbol] = (now, result)
    return result


_STATE_ZH = {"above": "上方", "inside": "通道内", "below": "下方"}
_STATE_EN = {"above": "above", "inside": "inside", "below": "below"}
_STRUCTURE_ZH = {"fast_above_slow": "快通道在慢通道之上（多头结构）", "fast_below_slow": "快通道在慢通道之下（空头结构）",
                 "overlapping": "快慢通道交织"}
_STRUCTURE_EN = {"fast_above_slow": "fast tunnel above the slow one (bullish structure)",
                 "fast_below_slow": "fast tunnel below the slow one (bearish structure)",
                 "overlapping": "tunnels overlapping"}


def summary_line(nx: Dict[str, Any], language: str = "zh") -> str:
    """One report line, e.g. "Price 34.27: below the fast tunnel (35.10–36.02), inside the slow one (...)"."""
    fast, slow = nx["fast"], nx["slow"]
    if language == "en":
        states, structures = _STATE_EN, _STRUCTURE_EN
        text = (f"Price {nx['close']:.2f}: {states[fast['state']]} the fast tunnel "
                f"({fast['bottom']:.2f}–{fast['top']:.2f}), {states[slow['state']]} the slow tunnel "
                f"({slow['bottom']:.2f}–{slow['top']:.2f}); {structures[nx['structure']]}; "
                f"{nx['to_fast_bottom_pct']:+.1f}% from the fast tunnel's lower edge")
        if nx["changes_today"]:
            text += "; changed today: " + ", ".join(nx["changes_today"])
        return text
    states, structures = _STATE_ZH, _STRUCTURE_ZH
    text = (f"现价 {nx['close']:.2f}：位于快通道（{fast['bottom']:.2f}–{fast['top']:.2f}）{states[fast['state']]}，"
            f"慢通道（{slow['bottom']:.2f}–{slow['top']:.2f}）{states[slow['state']]}；{structures[nx['structure']]}；"
            f"距快通道下沿 {nx['to_fast_bottom_pct']:+.1f}%")
    if nx["changes_today"]:
        text += "；今日位置变化：" + "，".join(nx["changes_today"])
    return text


def prompt_section(nx: Optional[Dict[str, Any]]) -> str:
    """Prompt block for the report model; empty without NX data."""
    if not nx:
        return ""
    return f"""
### NX 通道（用户在 moomoo 上使用的自定义指标，仅作图表背景）
- {summary_line(nx, "zh")}
- 快通道 = EMA(最高价,26)/EMA(最低价,26)；慢通道 = EMA(最高价,89)/EMA(最低价,89)；数据截至 {nx['as_of']}。
> 这是用户看图的参考框架，回测显示 NX 通道突破本身没有可验证的优势。可用它描述价格位置，并把快通道下沿、慢通道边沿作为止损/失效参考位；不得仅凭 NX 调整评分或买卖结论。
"""


def attach(result: Any, nx: Optional[Dict[str, Any]]) -> None:
    """Store NX on the result's dashboard (data_perspective.nx_tunnel), outside the model's answer."""
    if not nx or result is None:
        return
    dashboard = getattr(result, "dashboard", None)
    if not isinstance(dashboard, dict):
        dashboard = {}
        result.dashboard = dashboard
    perspective = dashboard.get("data_perspective")
    if not isinstance(perspective, dict):
        perspective = {}
        dashboard["data_perspective"] = perspective
    perspective["nx_tunnel"] = nx


def report_lines(perspective: Any, language: str = "zh") -> List[str]:
    """The NX line for a report's data section; empty without NX data."""
    nx = perspective.get("nx_tunnel") if isinstance(perspective, dict) else None
    if not isinstance(nx, dict):
        return []
    try:
        english = language not in ("zh", "zh-CN", "zh_CN")
        label = "NX tunnel" if english else "NX 通道"
        return [f"**{label}**: {summary_line(nx, 'en' if english else 'zh')}", ""]
    except (KeyError, TypeError, ValueError):
        return []
