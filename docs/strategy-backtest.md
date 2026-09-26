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

| Plan | Rule (see `src/services/trade_desk/strategy_backtest.py`) |
|---|---|
| `swing_trend` | `trend.score_bars` strength ≥ 70, not stretched; long or short |
| `swing_trend_regime` | the same, only with the market (SPY vs MA50) |
| `breakout` | the live breakout alert on daily closes (0.15 ATR past the 20-day high/low, right side of MA50, 1.3× volume, 7-day cooldown) |
| `pullback_long`, `fade_short` | alternatives fixed before their results were seen: an intact uptrend back at MA20; long where `swing_trend` says short |

Every trade enters at the next open with the levels the alerts print — stop 1.5
ATR, target 3 ATR, exit at the close after 15 sessions — and pays 5 bps per side.

## How to read it

- **Random twin**: each trade is re-entered on random days of the same ticker,
  year and direction with the same exits. A plan has an edge only if it beats
  its twin; the market's drift and the fill rules affect both alike.
- **Random-walk control**: the plans on synthetic prices with no edge. It loses
  about −0.25% per trade — 0.1% costs plus the conservative "stop first when a
  day touches both" fill. A clear profit here would reveal look-ahead.
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

## Findings (run of 2026-09-26, 557 of 613 S&P names, 53 of 91 removed names with data)

- Long trend and breakout entries underperform their random twins by about
  0.5% per trade in both periods (e.g. S&P `swing_trend` long +0.07% vs +0.64%
  after 2024); ETFs are close to their twins.
- Short trend and breakdown entries are clearly harmful: 0.5–1.6% per trade
  worse than random shorts in every period and universe (t between −3 and −15).
- ATM calls/puts and debit spreads on the same trades average −6% to −14% of
  premium with a median near −40%.
- `pullback_long` shows no edge. `fade_short` beats its twin by ~0.8% per trade in
  both periods, both universes and both fill rules (short-term reversal), but
  its equal-risk portfolio still drew down 45% in 2022 because signals cluster
  in selloffs, survivorship flatters it, and it was defined after seeing the
  short side lose — a hypothesis for forward tracking, not a strategy.
