# Trading research methodology

How a trading idea becomes something DSA shows you, and how it is graded. The idea can come
from a video, a model answer, a report or your own rule. DSA is research only: it never places,
changes or cancels a broker order. The backtest engines and their results are in
[strategy-backtest.md](strategy-backtest.md); this page is the protocol they follow.

The short version: write the rules down before you see any result. Compare every trade with
twins that differ only in the idea being tested. Prove the engine shows nothing on random data.
Require the result in two separate periods, after costs. Then let it run forward in shadow mode
before it becomes guidance.

## 1. Stages

| Stage | What it produces | Moves on when |
|---|---|---|
| 1. Idea | The source and its claim in one sentence | The claim can be tested at all |
| 2. Rules | Entry, stop, target, time exit, size, with every threshold a number | A computer could follow them without judgment |
| 3. Pre-registration | A frozen file (section 3) | Written before any result on real data |
| 4. Engine check | The rules and their twins run on random data | The difference is about zero (section 5) |
| 5. Backtest | Results per period with controls and costs | Always reported, whatever the result |
| 6. Verdict | `pass`, `no edge shown` or `rejected` (section 6) | `pass` goes on to stage 7 |
| 7. Forward (shadow) | Live levels graded in shadow mode, nothing sent | Enough finished positions (section 9) |
| 8. Guidance | Shown on the dashboard or in Discord, labelled with its grade | Re-graded as results come in |

Going further, to a paper account, orders approved one at a time, or automatic orders, is a
separate decision for the user. Each step needs graded forward results. It is not part of
research.

## 2. From an idea to rules

- **Sources.** Work from full transcripts, not titles or descriptions. Look at the charts in
  a video, not only the words. Treat a demo account, delayed data or boxes drawn on past bars
  as an illustration, not evidence. A source shows the examples it chose.
- **Split the method into parts:** setup, entry, stop, target, time exit, size. Test the
  parts separately where possible. For example, "don't trade in the middle" can be tested on
  your own fills without testing the full strategy.
- **Make it computable.** Every threshold is a number. A decision uses only bars completed
  before it, so there is no look-ahead. Define what a touch, a break and a fill mean.
- **When the source is vague,** take values from its own examples and write down why, before
  any result. Never choose a value because it tested well.
- **Match the horizon** to how the idea is meant to be held and how positions are actually
  held; the trade journal shows the second.

## 3. Pre-registration: the frozen file

Write one file before running anything on real data. It contains:

- the question in one sentence;
- the data: source, bar size, session, adjustment, dates, the two periods, and the universe,
  fixed in advance;
- the rules (section 2), the fill assumptions and the costs (section 7);
- the controls (section 5) and the pass bar (section 6);
- secondary checks, named in advance: they are reported but can't be used to pick the main
  result;
- how example charts will be chosen: at random, with the seed in the file.

Changes are allowed only before any real-data result. Write each one as a dated addendum with
its reason; finding a bias on random data is a good reason. After real results, any change
needs a new frozen file, and the old result is still reported. Count every variant you tried
and report them all.

## 4. Data

| Source | Bars | History | Use |
|---|---|---|---|
| moomoo OpenD `request_history_kline` | 1–60 min, daily | 15-min back to 2018-10 | Intraday backtests. 1,000 stocks per 30 days of history quota; `time_key` is the bar's end |
| Yahoo | 5/15-min | 60 days | Screening and rejection only: one regime |
| Yahoo | hourly | about 2 years | Coarse intraday checks |
| Yahoo | daily | many years | Swing backtests |

- Use the regular session unless the idea needs extended hours. Use split-adjusted prices.
- **Survivorship:** a universe of today's stocks favours past winners. Use point-in-time
  membership where it exists. Otherwise use controls that cancel the bias, such as same-stock
  or same-date twins, and judge by the difference, not the raw return.
- **Two periods** that don't overlap, each long enough for many months. A result that holds in
  only one period is not a finding.

## 5. Controls

**Twins.** Every trade gets twins that match it in everything except the idea under test. The
match covers the direction, the stop and target distances, the exits, the costs and the entry
mechanics. The idea decides what else must match:

| Idea tested | Twins differ only in |
|---|---|
| Stock selection (which stock today) | The stock: other stocks, same date |
| Timing or price level (when and where to enter) | The time: same stock, same or nearby sessions |

Lessons, each found by the random-data check:

- Twins on random days of the same stock faked edges with t up to 9: a signal picks a year by
  the stock's own path, and random days of that year inherit it.
- Strategy entries by limit order at a box edge against twins entered at the bar open faked
  +0.03 to +0.24 R. Twins now enter by a limit order at the same distance from the previous
  close.

**Random-data check (before real data).** Run the rules and their twins on shuffled bars,
where each bar keeps its gap and shape but the order is random, or on synthetic prices. The
difference must be about zero. What remains is the method's noise floor.

**Placebo levels** for ideas built on price levels: the same zones shifted to random prices.
A real effect must sit clearly outside the placebo spread.

**Absolute check.** Beating the twins is not enough. The rule must also make money after costs
to be usable.

## 6. Statistics and verdicts

- **Unit:** per trade, R (multiples of the stop distance) and % return. For options use % of
  premium, and report the median and a trimmed mean, because one large trade moves the average.
- **Clusters:** trades that overlap are not independent. Use month clusters for multi-day
  trades and day clusters for same-day trades, and compute t over the clusters. With few
  clusters, use the t-distribution's critical value (`idea_tracker.t_needed`): 12.7 for 2
  months, about 2.2 for 12.
- **Verdicts:**
  - `pass`: beats its twins in both periods with clustered t at or above the critical value
    (about 2), makes money after costs in both, and the random-data check is not significant.
  - `rejected`: loses to its twins with t ≤ −2 in both periods, or loses money after costs in
    both periods.
  - `no edge shown`: anything else, including "suggestive". It is not a reason to act.
- **Sample size:** report n. Fewer than 30 per group is an anecdote; comparing two groups needs
  about 50 in each.
- **Many tests:** at t = 2, about 1 variant in 20 passes by chance in one period. Requiring
  both periods is the main guard.

## 7. Execution and costs

- **Stocks:** 5 bps per side as the baseline.
  - A bar that opens past the stop fills at its open.
  - A stop and a target touched in the same bar count as the stop.
  - In the entry bar only the stop counts.
- **Options:** there are no historical quotes. Re-price with Black-Scholes on realised volatility
  × 1.15, losing 3% of premium per leg and side. Alternatively, test on the underlying first and
  check spreads separately. Options magnify costs.
- **Short holds:** the round trip costs about 0.1%, larger than most measured same-day effects
  (0.03–0.25%). A same-day rule must clear costs before anything else.

## 8. Risk and sizing

- **Per position:** the loss at its stop as % of the account, plus a gap allowance, since a
  stock can open past its stop.
- **Portfolio:** the total loss if every stop is hit. Correlated positions hit their stops on
  the same day: in one recent session most of a book of growth stocks hit its stops at once,
  several through opening gaps, although each position risked under 1% at its stop. Show the
  total and a gap scenario, not only per-position figures.
- **Limits** (risk per trade, total at stops, drawdown) are the user's choice. The system
  reports them and does not advise.
- **Backtests** size every trade at 1 R so results are comparable. Portfolio curves use equal
  risk per trade (`backtest/swing.py:portfolio`).

## 9. Forward testing (shadow mode)

- A rule that passes becomes a level in shadow mode, graded next to the existing levels: your
  alerts, the daily review and the ATR rule, one row per trigger.
- **Grading:** what selling at the trigger would have returned, against holding 10 more
  sessions. The verdict needs at least 30 finished positions (`MIN_FINISHED`) and a
  month-clustered t at the critical value.
- Ideas that can't be replayed without look-ahead, such as the model's review, go straight to
  forward tracking with a fixed rule.
- When forward results disagree with a backtest after enough positions, the forward results win.

## 10. Reporting

Every result states:

- the question and a link to its frozen file;
- the data and the universe;
- a table per period with the controls;
- the verdict word;
- what was not tested, and the risks;
- where the scripts and outputs are.

Example charts are drawn at random with the seed from the frozen file; no hand-picked
examples. Fix every random seed, including the twins', so a rerun gives the same numbers. One-off research scripts stay outside the repo until they become a maintained
backtest in `scripts/`.

## 11. Register of tested ideas

| Idea | Test | Verdict | Details |
|---|---|---|---|
| Swing trend, breakout, pullback (daily) | S&P 500, 2021-10 → 2026-09, same-date twins | No edge shown (within ±0.3%) | [strategy-backtest.md](strategy-backtest.md) |
| `fade_short` (long after drops) | Same | No edge shown: +0.21% in each period, small, defined after a look | Same |
| moomoo NX and CD indicators | Same, three exits | No edge shown; CD 抄底 above NX's slow tunnel suggestive (t 1.4–1.8) | Same |
| Same-day rules (ORB, gaps, big moves, momentum) | 5-min, 60 days; hourly, 2 years | No edge shown; `breakout_day` rejected (t −3.0) | Same |
| Model review of trade ideas | Forward, 2026-09 → 11 | No measurable value (\|t\| ≤ 1) | 2026-11-03 reflection (not yet in docs) |
| Printed stop and target on ideas | Forward, same | Better than no stop (−2.5% vs −6.4% per idea) | Same |
| Video zones: entries at a zone vs in the middle | Own option fills, 37 days | No edge shown: middle −12% vs zone +5%, n = 14–18, medians not significant | 2026-11-05 review |
| Video box trades: break and retest toward the base | moomoo 15-min, 49 stocks, 2020-11 → 2026-11, 1,851 trades | Rejected: −0.06 / −0.03 R per trade after costs, 0.09–0.10 R worse than random timing (t −2.2 / −2.9) | 2026-11-05 run |
| Video box trades: fade at a fresh box | Same, 6,515 trades | No edge shown: −0.07 / +0.01 R, 0.03 R worse than random timing (t −1.8 / −0.9) | Same |
| Video boxes confirmed by a weekly value-area edge | Same trades; VAH/VAL of the 2 prior weeks within the box | No edge shown in all 8 variants: about half of all boxes line up by chance, and those did no better | Same |
| Tidier boxes (small, no drift) | Same, with and without the value-area filter | No edge shown | Same |

## 12. Don't

- Tune after seeing results, then call the result out-of-sample.
- Judge a method by a demo, a hindsight drawing, a title or one good example.
- Use a twin that differs in more than the idea: entry mechanics, date or stock.
- Assume fills at the stop, or leave out costs and gaps.
- Trust one period, one regime or 60 days of data.
- Treat "suggestive" as "works".
