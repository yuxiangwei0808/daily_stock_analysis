"""The user's own moomoo indicators, NX and CD, recomputed from daily bars.

Both are MyLang (TDX-style) scripts saved in the moomoo account; the scripts come
from ``OpenQuoteContext.get_indicator_list`` and are reproduced line by line here.
MyLang semantics used: ``EMA`` seeded with the first value, ``REF`` with a fixed
or per-bar lag, ``BARSLAST`` (bars since the condition was last true, 0 on that
bar), ``LLV``/``HHV`` over a per-bar window including the current bar, and
``COUNT`` over a window. A value that does not exist yet (not enough history, or
a condition that never happened) is ``None`` and makes every comparison false.

- NX: a trend tunnel. ``A``/``B`` are EMA(high)/EMA(low) over 26 bars and
  ``A1``/``B1`` over 89 bars; above a tunnel is bullish, below it bearish.
- CD: MACD(12, 26, 9) divergence. ``buy`` is the script's "抄底" mark (DXDX,
  bullish divergence confirmed by the MACD line turning), ``sell`` its "卖出"
  mark (DBJGXC, bearish divergence confirmed).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

Series = List[Optional[float]]
Flags = List[bool]


def ema(values: Sequence[Optional[float]], n: int) -> Series:
    out: Series = []
    previous: Optional[float] = None
    for value in values:
        if value is None:
            out.append(previous)
            continue
        previous = float(value) if previous is None else (2 * float(value) + (n - 1) * previous) / (n + 1)
        out.append(previous)
    return out


def ref(values: Sequence[Any], lag: Any) -> List[Any]:
    """``REF(X, n)``: the value ``n`` bars back; ``n`` may be a per-bar series."""
    out: List[Any] = []
    for index in range(len(values)):
        step = lag[index] if isinstance(lag, (list, tuple)) else lag
        if step is None or index - int(step) < 0:
            out.append(None)
        else:
            out.append(values[index - int(step)])
    return out


def barslast(flags: Sequence[Any]) -> Series:
    out: Series = []
    last: Optional[int] = None
    for index, flag in enumerate(flags):
        if flag:
            last = index
        out.append(None if last is None else float(index - last))
    return out


def _window(values: Sequence[Optional[float]], length: Sequence[Optional[float]], pick) -> Series:
    out: Series = []
    for index in range(len(values)):
        span = length[index]
        if span is None:
            out.append(None)
            continue
        span = int(span)
        # LLV/HHV(X, 0) covers all history in MyLang; the scripts only pass N + 1 >= 1.
        start = 0 if span <= 0 else max(0, index - span + 1)
        window = [value for value in values[start:index + 1] if value is not None]
        out.append(pick(window) if window else None)
    return out


def llv(values, length) -> Series:
    return _window(values, length, min)


def hhv(values, length) -> Series:
    return _window(values, length, max)


def count(flags: Sequence[Any], n: int) -> List[int]:
    return [sum(1 for flag in flags[max(0, index - n + 1):index + 1] if flag) for index in range(len(flags))]


def _add(values, amount) -> Series:
    return [None if value is None else value + amount for value in values]


def _lt(a, b) -> bool:
    return a is not None and b is not None and a < b


def _gt(a, b) -> bool:
    return a is not None and b is not None and a > b


def _le(a, b) -> bool:
    return a is not None and b is not None and a <= b


def _ge(a, b) -> bool:
    return a is not None and b is not None and a >= b


def nx(bars: Sequence[Dict[str, Any]], n1: int = 26, n2: int = 89) -> Dict[str, Series]:
    highs = [float(bar["high"]) for bar in bars]
    lows = [float(bar["low"]) for bar in bars]
    return {"A": ema(highs, n1), "B": ema(lows, n1), "A1": ema(highs, n2), "B1": ema(lows, n2)}


def cd(bars: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """CD's lines and its two marks: ``buy`` (抄底, DXDX) and ``sell`` (卖出, DBJGXC)."""
    close = [float(bar["close"]) for bar in bars]
    size = len(close)
    ema12, ema26 = ema(close, 12), ema(close, 26)
    d = [a - b for a, b in zip(ema12, ema26)]  # DIF
    a = ema(d, 9)  # DEA
    m = [(x - y) * 2 for x, y in zip(d, a)]  # MACD bars
    m1 = ref(m, 1)
    n1 = barslast([_ge(p, 0) and _lt(c, 0) for p, c in zip(m1, m)])  # bars since MACD turned negative
    mm1 = barslast([_le(p, 0) and _gt(c, 0) for p, c in zip(m1, m)])  # bars since it turned positive
    n1p, mm1p = _add(n1, 1), _add(mm1, 1)

    cc1 = llv(close, n1p)
    cc2 = ref(cc1, mm1p)
    cc3 = ref(cc2, mm1p)
    difl1 = llv(d, n1p)
    difl2 = ref(difl1, mm1p)
    difl3 = ref(difl2, mm1p)
    ch1 = hhv(close, mm1p)
    ch2 = ref(ch1, n1p)
    ch3 = ref(ch2, n1p)
    difh1 = hhv(d, mm1p)
    difh2 = ref(difh1, n1p)
    difh3 = ref(difh2, n1p)

    aaa = [_lt(cc1[i], cc2[i]) and _gt(difl1[i], difl2[i]) and _lt(m1[i], 0) and d[i] < 0 for i in range(size)]
    bbb = [_lt(cc1[i], cc3[i]) and _lt(difl1[i], difl2[i]) and _gt(difl1[i], difl3[i]) and _lt(m1[i], 0) and d[i] < 0
           for i in range(size)]
    ccc = [(aaa[i] or bbb[i]) and d[i] < 0 for i in range(size)]
    ccc1, d1 = ref(ccc, 1), ref(d, 1)
    jjj = [bool(ccc1[i]) and d1[i] is not None and abs(d1[i]) >= abs(d[i]) * 1.01 for i in range(size)]
    jjj1 = ref(jjj, 1)
    buy = [jjj1[i] is not None and not jjj1[i] and jjj[i] for i in range(size)]  # DXDX: 抄底

    zjdbl = [_gt(ch1[i], ch2[i]) and _lt(difh1[i], difh2[i]) and _gt(m1[i], 0) and d[i] > 0 for i in range(size)]
    gxdbl = [_gt(ch1[i], ch3[i]) and _gt(difh1[i], difh2[i]) and _lt(difh1[i], difh3[i]) and _gt(m1[i], 0) and d[i] > 0
             for i in range(size)]
    dbbl = [(zjdbl[i] or gxdbl[i]) and d[i] > 0 for i in range(size)]
    dbbl1 = ref(dbbl, 1)
    dbjg = [bool(dbbl1[i]) and d1[i] is not None and d1[i] >= d[i] * 1.01 for i in range(size)]
    not_dbjg1 = ref([not flag for flag in dbjg], 1)
    sell = [bool(not_dbjg1[i]) and dbjg[i] for i in range(size)]  # DBJGXC: 卖出
    return {"DIF": d, "DEA": a, "MACD": m, "buy": buy, "sell": sell}
