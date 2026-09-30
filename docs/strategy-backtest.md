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
