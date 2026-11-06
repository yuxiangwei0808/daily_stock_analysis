"""The moomoo indicator script (scripts/moomoo_box_indicator.txt) draws the boxes of boxes.py."""
import math
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from src.services.trade_desk import boxes as bx
from tests.test_boxes import QUIET, SIDEWAYS, _bars, _impulse_day

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "moomoo_box_indicator.txt"


class Seq(list):
    """moomoo's Sequence as far as the script uses it: indexing, assignment and ``==`` per bar."""

    def __eq__(self, other):
        return Seq(value == other for value in self)

    __hash__ = None


def _run(bars, inputs):
    plots, sticks = {}, {}
    days = [int(str(d)[8:10]) for d in bars.start.astype("datetime64[D]")]
    fdatetime = types.ModuleType("fdatetime")
    fdatetime.day = lambda: Seq(days)
    color = types.SimpleNamespace(hex=lambda value: value)
    scope = {
        "__name__": "__main__", "indicator": lambda *args: None, "input_parameter": lambda name, default: inputs.get(name, default),
        "open": lambda: Seq(bars.o.tolist()), "high": lambda: Seq(bars.h.tolist()), "low": lambda: Seq(bars.l.tolist()),
        "close": lambda: Seq(bars.c.tolist()), "Color": color,
        "plot": lambda name, seq, color=None: plots.__setitem__(name, list(seq)),
        "plot_stickline": lambda name, cond, a, b, **kw: sticks.__setitem__(name, list(cond)),
    }
    previous = sys.modules.get("fdatetime")
    sys.modules["fdatetime"] = fdatetime
    try:
        exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), scope)
    finally:
        if previous is None:
            sys.modules.pop("fdatetime", None)
        else:
            sys.modules["fdatetime"] = previous
    return plots, sticks


def _expected(bars, params):
    want = {name: [math.nan] * len(bars) for name in ("RANGE_TOP", "RANGE_BOTTOM", "BASE_TOP", "BASE_BOTTOM")}
    for s in bx.setups(bars, params):
        for name, box, a in (("RANGE", s.range_box, s.i + 1), ("BASE", s.base_box, s.i - params.box_bars)):
            if box is not None:
                for k in range(a, s.last + 1):
                    want[f"{name}_BOTTOM"][k], want[f"{name}_TOP"][k] = box
    return want


def _same(a, b):
    return all((math.isnan(x) and math.isnan(y)) or x == pytest.approx(y) for x, y in zip(a, b)) and len(a) == len(b)


VARIANTS = [({}, bx.Params()),
            ({"Impulse x T (1.0 or 0.6)": 0.6, "Tidy boxes (1 = yes)": 1}, bx.Params(impulse_t=0.6, tidy=True))]


@pytest.mark.parametrize("inputs,params", VARIANTS)
def test_the_moomoo_script_draws_the_boxes_of_the_tested_rules(inputs, params):
    rng = np.random.default_rng(4)
    walk = [QUIET] * 11
    price = 100.0
    for _ in range(14):  # random sessions with jumps, so several setups overlap and some boxes are too tall
        day = []
        for _ in range(26):
            move = rng.normal(0, 0.4) + (rng.choice([0, 0, 0, 0, 0, 0, 0, 0, 3.0, -3.0]))
            o, c = price, price + move
            day.append((o, max(o, c) + abs(rng.normal(0, 0.2)), min(o, c) - abs(rng.normal(0, 0.2)), c))
            price = c
        walk.append(day)
    bars = _bars(walk + [_impulse_day(SIDEWAYS, [])] + [QUIET] * 3)
    assert sum(1 for _ in bx.setups(bars, params)) >= 3
    plots, sticks = _run(bars, inputs)
    for name, values in _expected(bars, params).items():
        assert _same(plots[name], values), name
    imp = np.abs(bars.chg) >= params.impulse_t * bars.T
    imp[np.isnan(bars.T)] = False
    assert sticks["IMPULSE"] == [bool(flag) for flag in imp]
