"""Backtest the plans the system produces, on daily bars, with the live parameters unchanged.

Plans tested (``PLANS``):

- ``swing_trend``: the deterministic layer of Trade opportunities — strength
  >= 70 and not stretched (``trend.score_bars``), long or short. The model
  review cannot be replayed historically, so this measures the candidates it
  receives.
- ``swing_trend_regime``: the same, only with the market (SPY vs MA50).
- ``breakout``: the live breakout alert — close beyond the prior 20-day high/low
  by 0.15 ATR on the right side of MA50, volume >= 1.3x the 50-day average;
  one alert per ticker and direction per 7 days.

Every plan enters at the next session's open, stops at 1.5 ATR, targets
3 ATR and exits at the close after ``MAX_DAYS`` sessions — the levels the
alerts print. A day touching both stop and target counts as the stop
(conservative); a gap through the stop fills at the open. Costs:
``COST_BPS`` per side. Short borrow fees and dividends are ignored.

``random_baseline`` gives each plan's trades random entry days on the same
tickers and direction with the same exits: a plan only has an edge if it beats
its own random twin. Option variants re-price the same trades with
Black–Scholes on realised volatility (no historical option quotes exist here).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import trend

STOP_ATR, TARGET_ATR, MAX_DAYS = 1.5, 3.0, 15
COST_BPS = 5.0
WINDOW = 130  # bars the live scan sees (six months)
BREAKOUT_MARGIN_ATR, BREAKOUT_PACE, BREAKOUT_COOLDOWN = 0.15, 1.3, 7
# A day touching stop and target: "stop" (conservative) or "nearest" (whichever is closer to the open).
AMBIGUOUS = "stop"
# Alternatives fixed before looking at their results (judged per period against random twins):
#  pullback_long - an established uptrend (price and MA20 above a rising MA50) back at MA20;
#  fade_short    - long where swing_trend says short (tests short-term reversal).
PLANS = ("swing_trend", "swing_trend_regime", "breakout", "pullback_long", "fade_short")


@dataclass
class Trade:
    plan: str
    ticker: str
    direction: str
    signal_date: str
    entry_date: str
    entry: float
    stop: float
    target: float
    exit_date: str = ""
    exit: float = 0.0
    reason: str = ""
    days: int = 0
    strength: int = 0
    atr: float = 0.0

    @property
    def sign(self) -> int:
        return 1 if self.direction == "long" else -1

    @property
    def return_pct(self) -> float:
        """Net of costs on both sides."""
        gross = self.sign * (self.exit / self.entry - 1) * 100
        return gross - 2 * COST_BPS / 100

    @property
    def r_multiple(self) -> float:
        risk = abs(self.entry - self.stop)
        return (self.return_pct / 100 * self.entry) / risk if risk else 0.0


def simulate(bars: Sequence[Dict[str, Any]], signal_index: int, direction: str, atr: float, *,
             plan: str, ticker: str, strength: int = 0, max_days: int = MAX_DAYS,
             stop_atr: float = STOP_ATR, target_atr: float = TARGET_ATR) -> Optional[Trade]:
    """One trade entered at the open after ``signal_index``; None when no next bar exists."""
    entry_index = signal_index + 1
    if entry_index >= len(bars) or atr <= 0:
        return None
    sign = 1 if direction == "long" else -1
    entry = float(bars[entry_index]["open"])
    if not entry or entry <= 0:
        return None
    stop, target = entry - sign * stop_atr * atr, entry + sign * target_atr * atr
    trade = Trade(plan, ticker, direction, bars[signal_index]["date"], bars[entry_index]["date"], entry,
                  stop, target, strength=strength, atr=atr)
    last = min(len(bars) - 1, entry_index + max_days - 1)
    for index in range(entry_index, last + 1):
        bar = bars[index]
        opened, high, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
        adverse = low <= stop if sign > 0 else high >= stop
        favourable = high >= target if sign > 0 else low <= target
        if adverse and favourable and AMBIGUOUS == "nearest" and abs(opened - target) < abs(opened - stop):
            adverse = False
        if adverse:
            gapped = (opened <= stop) if sign > 0 else (opened >= stop)
            trade.exit, trade.reason = (opened if gapped and index > entry_index else stop), "stop"
        elif favourable:
            gapped = (opened >= target) if sign > 0 else (opened <= target)
            trade.exit, trade.reason = (opened if gapped and index > entry_index else target), "target"
        if trade.reason:
            trade.exit_date, trade.days = bar["date"], index - entry_index + 1
            return trade
    trade.exit, trade.reason = float(bars[last]["close"]), "time" if last == entry_index + max_days - 1 else "end"
    trade.exit_date, trade.days = bars[last]["date"], last - entry_index + 1
    return trade


# -- signals ---------------------------------------------------------------------------
def _above_ma50(bars: Sequence[Dict[str, Any]], index: int) -> Optional[bool]:
    if index < 50:
        return None
    closes = [float(bar["close"]) for bar in bars[index - 49:index + 1]]
    return float(bars[index]["close"]) > sum(closes) / 50


def trend_signals(bars: Sequence[Dict[str, Any]], *, start: int = WINDOW, strong_only: bool = True) -> Iterable[tuple]:
    """(index, setup) for sessions whose trailing window is a strong, unstretched trend (or every scored one)."""
    for index in range(max(start, trend.MIN_BARS), len(bars) - 1):
        setup = trend.score_bars(bars[index + 1 - WINDOW if index + 1 > WINDOW else 0:index + 1])
        if setup and (not strong_only or (setup["strength"] >= trend.STRONG and not setup["extended"])):
            yield index, setup


def is_pullback(setup: Dict[str, Any]) -> bool:
    """Uptrend intact (price and MA20 above a rising MA50) with price back near MA20."""
    return (setup["ma20"] > setup["ma50"] and setup["price"] > setup["ma50"] and setup["ma50_slope_pct"] > 0
            and setup["price"] <= setup["ma20"] + 0.25 * setup["atr"])


def breakout_signals(bars: Sequence[Dict[str, Any]], *, start: int = 60) -> Iterable[tuple]:
    """(index, direction, atr) for the live breakout rule evaluated on daily closes."""
    last_alert: Dict[str, int] = {}
    for index in range(max(start, 55), len(bars) - 1):
        window = bars[index - 50:index]  # prior sessions only
        highs = [float(bar["high"]) for bar in window[-20:]]
        lows = [float(bar["low"]) for bar in window[-20:]]
        closes = [float(bar["close"]) for bar in window]
        volumes = [float(bar.get("volume") or 0) for bar in window]
        ranges = [max(float(b["high"]) - float(b["low"]), abs(float(b["high"]) - float(a["close"])),
                      abs(float(b["low"]) - float(a["close"]))) for a, b in zip(window[-15:-1], window[-14:])]
        atr = sum(ranges) / len(ranges) if ranges else 0
        avg_volume = sum(volumes) / len(volumes) if volumes else 0
        bar = bars[index]
        close, volume, ma50 = float(bar["close"]), float(bar.get("volume") or 0), sum(closes) / len(closes)
        if not atr or not avg_volume or volume / avg_volume < BREAKOUT_PACE:
            continue
        for direction, hit in (("long", close > max(highs) + BREAKOUT_MARGIN_ATR * atr and close > ma50),
                               ("short", close < min(lows) - BREAKOUT_MARGIN_ATR * atr and close < ma50)):
            if hit and index - last_alert.get(direction, -99) >= BREAKOUT_COOLDOWN:
                last_alert[direction] = index
                yield index, direction, atr


def run_plans(bars_by_ticker: Dict[str, List[Dict[str, Any]]], *, member: Callable[[str, str], bool],
              market: Optional[List[Dict[str, Any]]] = None, start_date: str = "", end_date: str = "",
              progress: Optional[Callable[[str], None]] = None) -> List[Trade]:
    """All plan trades; ``member(ticker, date)`` limits signals to the point-in-time universe."""
    market_up = {}
    if market:
        for index in range(len(market)):
            flag = _above_ma50(market, index)
            if flag is not None:
                market_up[market[index]["date"]] = flag
    trades: List[Trade] = []
    for count, (ticker, bars) in enumerate(bars_by_ticker.items()):
        if progress and count % 50 == 0:
            progress(f"{count}/{len(bars_by_ticker)} tickers")
        busy = {plan: -1 for plan in PLANS}
        for index, setup in trend_signals(bars, strong_only=False):
            day = bars[index]["date"]
            if not (start_date <= day <= end_date if end_date else start_date <= day) or not member(ticker, day):
                continue
            strong = setup["strength"] >= trend.STRONG and not setup["extended"]
            wanted = []
            if strong:
                wanted += [("swing_trend", setup["direction"]), ("swing_trend_regime", setup["direction"])]
                if setup["direction"] == "short":
                    wanted.append(("fade_short", "long"))
            if is_pullback(setup):
                wanted.append(("pullback_long", "long"))
            for plan, direction in wanted:
                if index <= busy[plan]:
                    continue  # one open trade per ticker and plan
                if plan == "swing_trend_regime":
                    up = market_up.get(day)
                    if up is None or up != (setup["direction"] == "long"):
                        continue
                trade = simulate(bars, index, direction, setup["atr"], plan=plan, ticker=ticker,
                                 strength=setup["strength"])
                if trade:
                    trades.append(trade)
                    busy[plan] = index + trade.days
        for index, direction, atr in breakout_signals(bars):
            day = bars[index]["date"]
            if not (start_date <= day <= end_date if end_date else start_date <= day) or not member(ticker, day):
                continue
            if index <= busy["breakout"]:
                continue
            trade = simulate(bars, index, direction, atr, plan="breakout", ticker=ticker)
            if trade:
                trades.append(trade)
                busy["breakout"] = index + trade.days
    return trades


def random_baseline(trades: Sequence[Trade], bars_by_ticker: Dict[str, List[Dict[str, Any]]], *,
                    member: Callable[[str, str], bool], seed: int = 7, draws: int = 3) -> List[Trade]:
    """Each trade re-entered on random member days of the same ticker and direction, same exits."""
    rng = random.Random(seed)
    twins: List[Trade] = []
    for trade in trades:
        bars = bars_by_ticker[trade.ticker]
        year = trade.signal_date[:4]
        pool = [i for i in range(WINDOW, len(bars) - 1)
                if bars[i]["date"][:4] == year and member(trade.ticker, bars[i]["date"])]
        for _ in range(draws):
            if not pool:
                break
            index = rng.choice(pool)
            window = bars[max(0, index - 14):index + 1]
            ranges = [max(float(b["high"]) - float(b["low"]), abs(float(b["high"]) - float(a["close"])),
                          abs(float(b["low"]) - float(a["close"]))) for a, b in zip(window, window[1:])]
            atr = sum(ranges) / len(ranges) if ranges else 0
            twin = simulate(bars, index, trade.direction, atr, plan=f"random:{trade.plan}", ticker=trade.ticker)
            if twin:
                twins.append(twin)
    return twins


# -- statistics --------------------------------------------------------------------------
def summarize(trades: Sequence[Trade]) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0}
    returns = [t.return_pct for t in trades]
    rs = [t.r_multiple for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [-r for r in returns if r <= 0]
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) if len(returns) > 1 else 0.0
    return {
        "trades": len(trades), "win_rate": len(wins) / len(trades) * 100,
        "avg_return_pct": mean, "median_return_pct": sorted(returns)[len(returns) // 2],
        "t_stat": mean / (sd / math.sqrt(len(returns))) if sd else 0.0,
        "avg_r": sum(rs) / len(rs), "profit_factor": (sum(wins) / sum(losses)) if losses and sum(losses) else None,
        "avg_days": sum(t.days for t in trades) / len(trades),
        "exits": {reason: sum(1 for t in trades if t.reason == reason) for reason in ("target", "stop", "time", "end")},
    }


def portfolio(trades: Sequence[Trade], *, risk_pct: float = 1.0, max_open: int = 10,
              start_equity: float = 100_000.0) -> Dict[str, Any]:
    """Equal-risk portfolio: each trade risks ``risk_pct`` of equity at entry, at most ``max_open`` at once,
    strongest signals first on crowded days. Returns an equity curve by exit date and headline figures."""
    by_entry: Dict[str, List[Trade]] = {}
    for trade in trades:
        by_entry.setdefault(trade.entry_date, []).append(trade)
    events = sorted(set(by_entry) | {t.exit_date for t in trades})
    equity, open_trades, curve, taken = start_equity, [], [], 0
    for day in events:
        still = []
        for trade, size in open_trades:
            if trade.exit_date == day:
                equity += size * trade.return_pct / 100
            else:
                still.append((trade, size))
        open_trades = still
        for trade in sorted(by_entry.get(day, []), key=lambda t: -t.strength):
            if len(open_trades) >= max_open:
                break
            stop_pct = abs(trade.entry - trade.stop) / trade.entry
            size = min(equity * risk_pct / 100 / stop_pct, equity / max_open * 2) if stop_pct else 0
            open_trades.append((trade, size))
            taken += 1
        curve.append((day, equity))
    peak, max_dd = start_equity, 0.0
    for _, value in curve:
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / peak * 100)
    years = max((date.fromisoformat(curve[-1][0]) - date.fromisoformat(curve[0][0])).days / 365.25, 1e-9) if curve else 1
    final = curve[-1][1] if curve else start_equity
    return {"taken": taken, "final_equity": final, "cagr_pct": ((final / start_equity) ** (1 / years) - 1) * 100,
            "max_drawdown_pct": max_dd, "curve": curve}


# -- options (Black–Scholes on realised volatility) -------------------------------------------
def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot: float, strike: float, years: float, vol: float, right: str, rate: float = 0.04) -> float:
    if years <= 0 or vol <= 0:
        return max(0.0, spot - strike) if right == "call" else max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    if right == "call":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * years) * _norm_cdf(d2)
    return strike * math.exp(-rate * years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def realised_vol(bars: Sequence[Dict[str, Any]], index: int, days: int = 20) -> float:
    closes = [float(bar["close"]) for bar in bars[max(0, index - days):index + 1]]
    logs = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(logs) < 5:
        return 0.0
    mean = sum(logs) / len(logs)
    return math.sqrt(sum((x - mean) ** 2 for x in logs) / (len(logs) - 1)) * math.sqrt(252)


OPTION_DTE = 35  # calendar days, the swing horizon plus room
IV_MARKUP = 1.15  # implied vol usually trades above realised
SPREAD_COST = 0.03  # 3% of premium lost to bid/ask per side


def option_variants(trade: Trade, bars: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Return on premium (%) for the same trade held as an at-the-money option, or as a debit
    spread from the money to the 3-ATR target (35 days to expiry, IV = 1.15x realised vol,
    3% of premium lost to the bid/ask per leg and side)."""
    index_in = next(i for i, bar in enumerate(bars) if bar["date"] == trade.entry_date)
    index_out = next(i for i, bar in enumerate(bars) if bar["date"] == trade.exit_date)
    vol_in = realised_vol(bars, index_in - 1) * IV_MARKUP
    vol_out = realised_vol(bars, index_out) * IV_MARKUP or vol_in
    if vol_in <= 0:
        return {}
    right = "call" if trade.direction == "long" else "put"
    held = (date.fromisoformat(trade.exit_date) - date.fromisoformat(trade.entry_date)).days
    t_in, t_out = OPTION_DTE / 365, max(OPTION_DTE - held, 0) / 365
    spot_in, spot_out = trade.entry, trade.exit
    near = round(spot_in)
    far = near + trade.sign * round(max(1.0, TARGET_ATR * trade.atr))
    single_in = bs_price(spot_in, near, t_in, vol_in, right)
    single_out = bs_price(spot_out, near, t_out, vol_out, right)
    spread_in = single_in - bs_price(spot_in, far, t_in, vol_in, right)
    spread_out = single_out - bs_price(spot_out, far, t_out, vol_out, right)
    result = {}
    if single_in > 0:
        result["atm_option"] = ((single_out * (1 - SPREAD_COST)) / (single_in * (1 + SPREAD_COST)) - 1) * 100
    if spread_in > 0:
        result["debit_spread"] = ((max(spread_out, 0) * (1 - 2 * SPREAD_COST)) / (spread_in * (1 + 2 * SPREAD_COST)) - 1) * 100
    return result


# -- point-in-time S&P 500 ------------------------------------------------------------------
def membership_from_changes(current: Iterable[str], changes: Iterable[Dict[str, str]]) -> Callable[[str, str], bool]:
    """``member(ticker, day)`` from today's list and dated changes {date, added, removed}, walked backwards."""
    spans: Dict[str, List[List[str]]] = {ticker: [["0000-00-00", "9999-99-99"]] for ticker in current}
    for change in sorted(changes, key=lambda item: item["date"], reverse=True):
        day, added, removed = change["date"], change.get("added"), change.get("removed")
        if added:
            spans.setdefault(added, [["0000-00-00", "9999-99-99"]])
            spans[added][-1][0] = day  # member from its addition date
        if removed:
            spans.setdefault(removed, []).append(["0000-00-00", day])  # member until removal
    spans = {ticker: [span for span in ranges if span[0] < span[1]] for ticker, ranges in spans.items()}

    def member(ticker: str, day: str) -> bool:
        return any(start <= day < end for start, end in spans.get(ticker, []))

    member.tickers = sorted(spans)  # type: ignore[attr-defined]
    return member


def random_walk_bars(count: int, days: int, *, seed: int = 11, daily_vol: float = 0.02,
                     start_price: float = 50.0) -> Dict[str, List[Dict[str, Any]]]:
    """Synthetic tickers with no edge to find (driftless log-normal walk, realistic ranges and volume).

    Running the plans here is a control: any consistent profit would reveal look-ahead or fill bugs.
    """
    rng = random.Random(seed)
    out = {}
    first = date(2020, 1, 1)
    for n in range(count):
        price, rows, day = start_price * math.exp(rng.gauss(0, 0.5)), [], first
        vol = daily_vol * math.exp(rng.gauss(0, 0.3))
        for _ in range(days):
            day += timedelta(days=1 if day.weekday() < 4 else 3)
            opened = price * math.exp(rng.gauss(0, vol * 0.3))
            close = opened * math.exp(rng.gauss(-vol * vol / 2, vol))
            high = max(opened, close) * math.exp(abs(rng.gauss(0, vol * 0.5)))
            low = min(opened, close) * math.exp(-abs(rng.gauss(0, vol * 0.5)))
            rows.append({"date": day.isoformat(), "open": opened, "high": high, "low": low, "close": close,
                         "volume": 2_000_000 * math.exp(rng.gauss(0, 0.4))})
            price = close
        out[f"RW{n:03d}"] = rows
    return out
