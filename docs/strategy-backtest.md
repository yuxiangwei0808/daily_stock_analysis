# Backtesting the system's trade plans

`scripts/backtest_trade_plans.py` replays the **deterministic** rules behind Trade
opportunities and breakout alerts on daily bars, with the live parameters
unchanged (nothing is tuned on the results):

```bash
python scripts/backtest_trade_plans.py --start 2021-10-01 --split 2024-01-01 --out reports/backtests
python scripts/backtest_trade_plans.py --ambiguous nearest   # robustness: alternative same-day fill
```

It writes `trade_plans_<date>_<fill>.json/.md` (the `reports/` folder is not committed).

## What is tested

| Plan | Rule (see `src/services/trade_desk/backtest/swing.py`) |
|---|---|
| `swing_trend` | `trend.score_bars` strength ≥ 70, not stretched; long or short |
| `swing_trend_regime` | the same, only with the market (SPY vs MA50) |
| `breakout` | the live breakout alert on daily closes (0.15 ATR past the 20-day high/low, right side of MA50, 1.3× volume, 7-day cooldown) |
| `pullback_long`, `fade_short` | alternatives fixed before their results were seen: an intact uptrend back at MA20; long where `swing_trend` says short |

Every trade enters at the next open with the levels the alerts print — stop 1.5
ATR, target 3 ATR, exit at the close after 15 sessions — and pays 5 bps per side.

## How to read it

- **Same-date twin** (the control since 2026-09-29): each trade is re-entered on
  the same signal date in three other member stocks, same direction and exits
  (an event-study control). A plan has an edge only if it beats these twins.
- **Same-ticker twin** (used until 2026-09-29, still printed): random days of the
  same stock, year and direction. It is biased: signals select on the stock's own
  path (a dip-buy fires in a year that fell, a trend-long in a year that rose), and
  random days of that year inherit it. On random walks it shows fake edges of
  ±0.3–2.7% with t up to 9, so its verdicts are withdrawn.
- **Random-walk control**: the plans and their twins on synthetic prices with no
  edge. Plans should match their same-date twins; they do within about ±0.3%
  (short rules and ATR exits keep a residual of up to −0.5%), which is the
  method's noise floor.
- **Periods**: results before and after `--split` are reported separately; a
  finding that holds in only one is not a finding.
- **Universe**: S&P 500 membership is rebuilt point-in-time from Wikipedia's
  [historical components](https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500);
  removed stocks are included when Yahoo still has their bars (the report shows
  the coverage). Missing delisted names bias results upward, most of all for
  strategies that buy losers. The ETF universe has no survivorship issue.
- **Options** are re-priced with Black–Scholes on realised volatility × 1.15 and
  3% of premium lost per leg and side — there are no historical option quotes.
- **Not tested**: the model review of trade ideas (it cannot be replayed without
  look-ahead). Its value can only be measured going forward.

## Findings (rerun 2026-09-29 with same-date twins; 557 of 613 S&P names)

Rule average vs same-date twins, S&P 500, 2021-10 → 2026-09 (both periods agree
unless noted):

| Rule | Rule avg | Twin avg | Difference |
|---|---|---|---|
| `swing_trend` long | +0.02% | +0.10% | −0.08% |
| `swing_trend` short | −0.96% | −0.76% | −0.20% |
| `breakout` long / short | +0.14% / −0.84% | +0.19% / −0.74% | −0.05% / −0.10% |
| `pullback_long` | −0.07% | −0.08% | 0.00% |
| `fade_short` (a long after drops) | +0.61% | +0.40% | +0.21% (+0.21% in each period) |

- None of the rules picks better or worse stocks than random ones on the same
  day, within the ±0.3% noise floor. Shorts lose because shorting lost money in
  this period (same-date random shorts lost −0.76% per trade too), not because the
  rule picks bad shorts.
- The earlier verdicts (longs 0.5% worse than random, shorts 0.5–1.6% worse,
  `fade_short` +0.8%) came from the biased same-ticker twin and are withdrawn.
- ATM calls/puts and debit spreads on the trend trades average −6% to −14% of
  premium with a median near −40% (absolute results, unaffected by the baseline).
- `fade_short`'s +0.21% is consistent but small, defined after an earlier look,
  and flattered by survivorship; forward tracking decides.

## Your moomoo indicators NX and CD (`scripts/backtest_indicators.py`)

NX and CD are the user's own MyLang scripts, read from the moomoo account with
`get_indicator_list` and rebuilt in `src/services/trade_desk/moomoo_indicators.py`.
NX matches moomoo's own calculation (`request_indicator_calc_async`) exactly on 10
tickers over two years; moomoo returns CD's 抄底/卖出 text marks as zeros, so CD was
checked against synthetic divergences and the report lists recent marks to compare
with the chart. Rules (fixed before the run), entry at the next open, three exits
(1.5/3 ATR over 15 sessions, a 10-session hold, the indicator's own exit up to 60
sessions): CD 抄底 long, the same only above NX's slow tunnel, CD 卖出 short, NX
fast-tunnel breakout long above the slow tunnel, and the mirror short.

Findings (2026-09-29, same-date twins, t clustered by signal month):

- CD 抄底: +0.2–0.3% vs same-date stocks (t 0.7–1.3): no edge shown.
- CD 抄底 above NX's slow tunnel: +0.5–1.0% in both periods with the short
  exits (t 1.4–1.8, 480 trades): suggestive, not established; worth tracking.
- CD 卖出: shorting it loses; as a "reduce" signal the stock lags other stocks by
  about 0.1% over ten sessions: negligible.
- NX tunnel entries (long or short): about zero vs same-date stocks.
- With the biased same-ticker twin CD 抄底 had looked like +1–4% (t up to 4.7) and
  NX longs −0.5%; the random-walk control reproduced both, which exposed the bias.

## Day trading (`scripts/backtest_day_trades.py`)

```bash
python scripts/backtest_day_trades.py --out reports/backtests
```

Engine: `src/services/trade_desk/backtest/intraday.py`. Every trade is flat by the
close; rules were fixed before the run:

| Rule | Entry | Exit |
|---|---|---|
| `orb30` | first 5-min close beyond the 09:30–10:00 range, next open | other side of the range, else the close |
| `gap_go` / `gap_fade` | open ≥ 2% from the prior close; 09:35 open with / against the gap | close |
| `move_follow` / `move_fade` | day change first crossing ±3% from 09:45 (the pulse alert); next open | close |
| `breakout_day` | the live breakout alert (20-day level ± 0.15 ATR, MA50 side, pace ≥ 1.3, two bars) | close |
| `momentum_last30` | direction of prior close → 10:00 (hourly: first hour); 15:30 open | close |

- **Twins**: the same ticker, side, entry time of day and exit rule (an ORB stop at
  the same distance) on five random *other* days. Same-day random entries would
  leak the signal day's path into the baseline.
- **Significance**: one rule-minus-twin number per signal day; the t-statistic is
  over days, because the same-day trades of 100 stocks are not independent.
- **Data**: Yahoo keeps 5-minute bars for 60 days (S&P 100, the watchlist and 25
  ETFs incl. TQQQ/SQQQ/SOXL/SOXS) and hourly bars for about two years (the
  momentum rule only). 5 bps per side; real fills on fast moves are worse.

### Findings (run of 2026-09-27; 5-min 2026-07-02 → 09-25, 141 tickers)

These twins are other days of the same stock (one session each), not the same-date
control above; the path bias is smaller over a single session but was not measured.


- Nothing beats its twin with a day-clustered |t| ≥ 2 except in the losing direction.
  The round trip's 0.1% is larger than almost every gross effect (±0.03–0.25%).
- `breakout_day` on S&P 100 names: −0.54% net per trade, 0.44% worse than its
  twin (t −3.0). An intraday breakout alert is a poor entry for a same-day trade.
- `orb30`: −0.17% net and win rate 41% (ETFs 35%), slightly below its twin.
- Gaps: fading was slightly better than going with the gap (+0.04% vs −0.24% net),
  but the difference to the twins is noise (t ≤ 0.8).
- Big moves: following vs fading reverses between S&P 100 names and the watchlist;
  neither is significant.
- Intraday momentum on two years of hourly bars (≈90k trades): gross effect 0.00%.
- Sixty days are one regime; these numbers cannot confirm a rule, only reject the
  ones that already lose on costs.

## Box trades from a trading video (`scripts/backtest_boxes.py`)

```bash
python scripts/backtest_boxes.py --out reports/backtests            # downloads missing stocks from OpenD
python scripts/backtest_boxes.py --tickers SPY,QQQ --offline          # cached bars only
```

The rules are in `src/services/trade_desk/boxes.py`, which the live chart
([live-chart-plan.md](live-chart-plan.md), `GET /chart/{ticker}`) shares. Fills, twins and the controls are in
`src/services/trade_desk/backtest/boxes.py`. Bars are moomoo 15-minute bars (forward-adjusted,
regular session, back to 2018-10), cached in `reports/backtests/bars_15m`. Each newly downloaded
stock uses one moomoo history allowance for 30 days; cached stocks use none. The rules were fixed
before any result, following [trading-methodology.md](trading-methodology.md).

| Trade | Entry | Stop | Target |
|---|---|---|---|
| `break_retest` (the video's worked example) | After a close outside the range box on the side away from the impulse: a limit at the box's near edge, against the impulse | Past the range's extreme wick, impulse bar included | The base box's near edge |
| `fade` | The first return to a box from outside: a limit at its edge | Past the box's own extreme wick | 2 × the stop distance |

- **Impulse:** one 15-minute bar, opening gap included, of at least 1.0 × T (main) or 0.6 × T.
  T is the median session range over the 10 prior sessions.
- **Boxes:** the bodies of the 8 bars after the impulse (range box) and before it (base box). A
  box counts when it is at most 0.75 × the impulse. The tidy variant requires at most 0.5 ×
  with no drift. A setup lives for 5 sessions.
- **Exits:** stop or target. Both in one bar count as the stop; in the entry bar only the stop
  counts. A gap past the stop fills at the open. Otherwise the trade closes after 5 sessions.
- **Cost:** 0.05% per round trip. That is half the 5 bps a side used above; more cost only
  makes the result worse.
- **Twins:** 20 per trade, on the same stock with the same direction, stop and target. Each
  enters by a limit at the same distance from the previous close as the real entry, on random
  bars of the same session (or the five sessions either side). Twins entered at the bar open
  instead faked +0.03 to +0.24 R on shuffled bars. Seeds are fixed, so a rerun gives the same
  numbers.
- **Value-area check:** a trade is confirmed when the value-area high or low of either of the
  two prior weeks falls within the traded box (wicks included, ± 0.1 T). The weekly volume
  profile is approximated from the 15-minute bars. The chance control moves those levels by
  1–3 T, 200 times.

### Findings (run of 2026-11-05; 49 stocks and ETFs, 2020-11 → 2026-11, period B from 2023-11)

The universe was the four index ETFs, 20 large caps across sectors, and the user's traded and
held tickers. R is per trade after costs; "edge" is R minus the twins' R; t is clustered by
month. All variants are shown.

| Trade, impulse | Period A: trades, R, edge (t) | Period B: trades, R, edge (t) | Verdict |
|---|---|---|---|
| `break_retest`, 1.0 T | 610, −0.062, −0.101 (−2.24) | 1,241, −0.030, −0.091 (−2.90) | Rejected |
| `fade`, 1.0 T | 2,244, −0.066, −0.029 (−1.78) | 4,271, +0.005, −0.031 (−0.90) | No edge shown |
| `break_retest`, 0.6 T | 1,251, −0.046, −0.134 (−4.60) | 1,933, −0.034, −0.104 (−3.57) | Rejected |
| `fade`, 0.6 T | 6,901, −0.062, −0.035 (−2.35) | 10,247, −0.046, −0.029 (−1.10) | Rejected (loses in both) |
| Tidy boxes, 1.0 T (`break_retest` / `fade`) | 142 / 848, R +0.013 / −0.117, edge −0.081 / −0.057 | 218 / 1,602, R −0.023 / +0.018, edge −0.051 / +0.012 | No edge shown |
| Tidy boxes, 0.6 T (`break_retest` / `fade`) | 231 / 2,180, R −0.041 / −0.087, edge −0.065 / −0.009 | 334 / 3,302, R −0.067 / −0.076, edge −0.072 / −0.019 | Rejected (lose in both) |

- Win rates were 32–38%. On shuffled bars `break_retest` gains +0.07 R on its twins (t 1.96),
  so its real shortfall is, if anything, understated.
- **Value area:** 41–54% of trades line up with a weekly value-area edge by chance alone. The
  confirmed-minus-unconfirmed gap changes sign between the periods in every variant, and no
  variant passes.
- **Not tested:** order flow (the video's footprint and delta need tick history), the trader's
  own choice of which boxes to trade, DAX futures, and options (with options the costs are higher).

## Which stocks move today: the in-play range test (`scripts/backtest_in_play.py`)

Frozen on 2026-11-06, before any run on real data ([trading-methodology.md](trading-methodology.md) §3).
Results are added after this part; the rules in it don't change.

- **Question:** does ranking stocks at 09:45 by how far they have already moved today, against their
  own normal, pick the stocks that move most over the rest of the session, more than the other
  stocks that day? Options pay for movement, so this is what a day list must do first (methodology §13).
- **Data:** the 49 cached stocks and ETFs of the box test (`reports/backtests/bars_15m`: SPY, QQQ, IWM,
  DIA, 20 large caps and the watchlist of 2026-11-05), moomoo 15-minute bars, forward-adjusted, regular
  session. Period A is 2020-11-05 to 2023-11-04, period B 2023-11-05 to 2026-11-05. A day counts when at
  least 20 stocks have the bars it needs.
- **Normal:** T, the median session range (high − low) of the 10 prior sessions, as in `boxes.py`.
- **Score at 09:45**, after the first 15-minute bar: S = (|open − prior close| + that bar's high − low) / T.
- **Picks:** the 5 stocks with the highest S each day.
- **Outcome:** the rest of the session's range / T: the highest high minus the lowest low of the bars
  from 09:45–10:00 to the close.
- **Twins:** every other stock that day, so picks and twins differ only in the stock. The edge of a day
  is the picks' mean outcome minus the others'.
- **Statistics and pass bar:** t over days (day clusters), in each period. It passes when the edge is
  above 0 with t ≥ 2 in both periods (section 6's critical value with hundreds of clusters). Section 6's
  absolute check, money after costs, doesn't apply: this picks what to watch, not a trade, and its
  verdict says so.
- **Random-data checks:** (1) the same days with the scores shuffled among each day's stocks, 200
  times: the edge must be about 0; (2) the whole procedure on random walks built from each stock's own
  15-minute moves in shuffled order: the edge must be about 0.
- **Secondary checks**, reported and never used to choose: top 3 instead of 5; the gap alone; the
  first bar's range alone; S times the first bar's volume over its 10-session median; up and down
  gaps apart; week clusters instead of days.
- **Seed:** 7 for every random draw.

**Addendum, 2026-11-06, before any real-data result.** The random-walk check on synthetic bars (the
engine's own test) "passed" the frozen design: +0.11 T a day, t 2.5 and 3.6. The score and the outcome
were both divided by T, so a stock-day whose T happened to be low got a high score and a high outcome
alike. The outcome is now divided by T2, the median session range of the sessions 11 to 20 before, which
shares no session with T. A day needs 20 prior sessions. Nothing else changes.
