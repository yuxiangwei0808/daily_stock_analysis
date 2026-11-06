"""The register of trading ideas (docs/trading-methodology.md §11), written once.

The methodology doc's register tables are printed from here (``python -m
src.services.trade_desk.register``, checked by a test), and the chart's flag grades, the trade
ideas' labels and the Discord gate all read from here, so a verdict is never restated.

Verdicts are the doc's words (§6): ``pass``, ``no edge shown`` and ``rejected`` for a tested idea;
``tracked forward`` for a source graded on the Journal tab's scoreboard that has no verdict here yet;
``not tested`` for something shown before any test. Only ``pass`` lets an idea reach Discord (§1,
stage 8), and only an edit here promotes one, after its forward grading agrees.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

PASS, NO_EDGE, REJECTED, FORWARD, NOT_TESTED = "pass", "no edge shown", "rejected", "tracked forward", "not tested"
TESTED = (PASS, NO_EDGE, REJECTED)
BACKTEST_DOC = "[strategy-backtest.md](strategy-backtest.md)"


@dataclass(frozen=True)
class Idea:
    id: str
    name: str  # the doc's "Idea" column
    test: str
    verdict: str
    summary: str  # the doc's "Verdict" column; starts with the verdict
    details: str = ""


IDEAS: Tuple[Idea, ...] = (
    Idea("swing_trend", "Swing trend, breakout, pullback (daily)", "S&P 500, 2021-10 → 2026-09, same-date twins", NO_EDGE,
         "No edge shown (within ±0.3%)", BACKTEST_DOC),
    Idea("fade_short", "`fade_short` (long after drops)", "Same", NO_EDGE,
         "No edge shown: +0.21% in each period, small, defined after a look", "Same"),
    Idea("moomoo_nx_cd", "moomoo NX and CD indicators", "Same, three exits", NO_EDGE,
         "No edge shown; CD 抄底 above NX's slow tunnel suggestive (t 1.4–1.8)", "Same"),
    Idea("same_day_rules", "Same-day rules (ORB, gaps, big moves, momentum)", "5-min, 60 days; hourly, 2 years", NO_EDGE,
         "No edge shown; `breakout_day` rejected (t −3.0)", "Same"),
    Idea("model_review", "Model review of trade ideas", "Forward, 2026-09 → 11", NO_EDGE,
         "No edge shown: no measurable value (\\|t\\| ≤ 1)", "2026-11-03 reflection (not yet in docs)"),
    Idea("printed_stop", "Printed stop and target on ideas", "Forward, same", NO_EDGE,
         "No edge shown as a test (one regime); the stop lost less than no stop (−2.5% vs −6.4% per idea)", "Same"),
    Idea("video_zones", "Video zones: entries at a zone vs in the middle", "Own option fills, 37 days", NO_EDGE,
         "No edge shown: middle −12% vs zone +5%, n = 14–18, medians not significant", "2026-11-05 review"),
    Idea("box_break_retest", "Video box trades: break and retest toward the base",
         "moomoo 15-min, 49 stocks, 2020-11 → 2026-11, 1,851 trades", REJECTED,
         "Rejected: −0.06 / −0.03 R per trade after costs, 0.09–0.10 R worse than random timing (t −2.2 / −2.9)",
         BACKTEST_DOC),
    Idea("box_fade", "Video box trades: fade at a fresh box", "Same, 6,515 trades", NO_EDGE,
         "No edge shown: −0.07 / +0.01 R, 0.03 R worse than random timing (t −1.8 / −0.9)",
         "Same; `scripts/backtest_boxes.py`"),
    Idea("box_value_area", "Video boxes confirmed by a weekly value-area edge",
         "Same trades; VAH/VAL of the 2 prior weeks within the box", NO_EDGE,
         "No edge shown in all 8 variants: about half of all boxes line up by chance, and those did no better", "Same"),
    Idea("box_tidy", "Tidier boxes (small, no drift)", "Same, with and without the value-area filter", NO_EDGE,
         "No edge shown at 1.0 T; rejected at 0.6 T (loses in both periods)", "Same"),
    # Shown or tracked, without a verdict here.
    Idea("report_calls", "Stock report calls (buy, watch, sell)", "Forward from each day's first report", FORWARD,
         "Tracked forward against the rest of the watchlist", "Journal tab, What's working"),
    Idea("social_youtube", "Social scan and YouTube picks", "Forward, 5/10/20 sessions", FORWARD,
         "Tracked forward against same-day stocks", "Journal tab, What's working"),
    Idea("prior_day_levels", "Prior day high, low and close", "Shown on the chart", NOT_TESTED,
         "Not tested as levels on their own", "[trade-desk.md](trade-desk.md#chart)"),
    Idea("value_area_levels", "Value-area high and low of the two prior weeks", "Shown on the chart", NOT_TESTED,
         "Not tested as levels on their own (only as a box filter, above)", "[trade-desk.md](trade-desk.md#chart)"),
    Idea("premarket_levels", "Pre-market high and low", "Shown by the trading assistant", NOT_TESTED,
         "Not tested", "[trading-assistant-plan.md](trading-assistant-plan.md)"),
    Idea("opening_range", "Opening range (first 5 and 15 minutes)", "Shown by the trading assistant", NOT_TESTED,
         "Not tested here (the 60-day same-day test of ORB showed no edge, above)", "Same"),
    Idea("vwap", "VWAP from 1-minute bars", "Shown by the trading assistant", NOT_TESTED, "Not tested", "Same"),
)
_BY_ID: Dict[str, Idea] = {idea.id: idea for idea in IDEAS}

# The chart's eight box variants (the 2026-11-05 backtest, docs/strategy-backtest.md): each trade
# against the same trade at random times; t by month in period A (2020-11 to 2023-11) and B (to 2026-11).
BOX_VARIANTS: Dict[Tuple[str, float, bool], Tuple[str, float, float]] = {
    ("break_retest", 1.0, False): (REJECTED, -2.24, -2.90),
    ("fade", 1.0, False): (NO_EDGE, -1.78, -0.90),
    ("break_retest", 0.6, False): (REJECTED, -4.60, -3.57),
    ("fade", 0.6, False): (REJECTED, -2.35, -1.10),
    ("break_retest", 1.0, True): (NO_EDGE, -0.04, -0.11),
    ("fade", 1.0, True): (NO_EDGE, -1.47, 0.26),
    ("break_retest", 0.6, True): (REJECTED, -0.52, -0.65),
    ("fade", 0.6, True): (REJECTED, -0.69, -0.49),
}
BOX_SOURCE = "docs/strategy-backtest.md, run of 2026-11-05"

# What each Trade Desk idea message rests on.
SCAN_RULES = ("swing_trend", "model_review")  # the trend scan and its model review, and their options follow-ups
BREAKOUT_RULES = ("swing_trend",)


def get(idea_id: str) -> Idea:
    return _BY_ID[idea_id]


def passed(idea_id: str) -> bool:
    idea = _BY_ID.get(idea_id)
    return idea is not None and idea.verdict == PASS


def discord_ok(rules: Optional[Iterable[str]]) -> bool:
    """An idea reaches Discord only when every rule it rests on has passed; one naming none never does."""
    rules = list(rules or [])
    return bool(rules) and all(passed(rule) for rule in rules)


def label(idea_id: str) -> str:
    """One line for a page: the idea and its verdict, without the doc's table escapes."""
    idea = _BY_ID[idea_id]
    text = f"{idea.name}: {idea.summary[0].lower()}{idea.summary[1:]}"
    return text.replace("\\|", "|").replace("`", "")


def box_grade(kind: str, impulse_t: float, tidy: bool) -> Tuple[str, float, float]:
    return BOX_VARIANTS[(kind, impulse_t, tidy)]


def markdown_tables() -> str:
    """The doc's two register tables: tested ideas, then what is tracked or shown without a verdict here."""
    def table(rows):
        lines = ["| Idea | Test | Verdict | Details |", "|---|---|---|---|"]
        return "\n".join(lines + [f"| {r.name} | {r.test} | {r.summary} | {r.details} |" for r in rows])
    tested = [idea for idea in IDEAS if idea.verdict in TESTED]
    other = [idea for idea in IDEAS if idea.verdict not in TESTED]
    return (table(tested) + "\n\nTracked forward or shown, without a verdict here (the scoreboard grades the forward "
            "sources):\n\n" + table(other))


if __name__ == "__main__":
    print(markdown_tables())
