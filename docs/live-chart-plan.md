# Plan: live chart with boxes, levels and graded suggestions

Status: phases 1 to 3 done (boxes module, `scripts/backtest_boxes.py`, `GET /chart/{ticker}`,
the Chart tab); later phases not built.
Owner decision 2026-11-05: build it on the dashboard with KLineChart.
A moomoo indicator script is a small add-on at the end.

## What it is for

You pick a stock, or take one from your holdings, the watchlist or today's trade ideas. The
dashboard shows a live chart with what the system sees: boxes, value-area lines, prior-day
levels, your stops, targets and alerts, and your indicators. It flags setups as they form and
shows each flag's test grade. You decide and place every order yourself. DSA never sends,
changes or cancels an order.

Every flag, and every trade you mark from the chart, is recorded and graded automatically. This
follows [trading-methodology.md](trading-methodology.md): a flag becomes a suggestion only after
it passes a backtest and the forward grading. Until then it is labelled as context, with its
result, for example "box retest: rejected in backtest (t −2.9)". This is also how your own chart
reading gets a fair, forward test.

## What already exists

| Piece | Where | Reused for |
|---|---|---|
| moomoo live feed: quote, order-book and fill push on their own connections | `trade_desk/live_feed.py` | Live prices and the connection pattern |
| Bars from OpenD (`get_cur_kline`, subscribed) | `MoomooProvider.kline` | The chart's history |
| Position plans: stop, target, source | `position_plans.py` | Stop and target lines, risk |
| Your alert rules | `holdings.py` | Alert lines |
| NX tunnels and CD marks, rebuilt from your moomoo scripts | `moomoo_indicators.py` | Indicators on the chart |
| 20-day low and other levels | `levels.py`, `monitor.price_levels` | Lines |
| Shadow grading: month-clustered t, `t_needed`, a results table per trigger | `shadow_exits.py`, `idea_tracker.py` | Grading flags and your marks |
| Event stream to the page (SSE) | `GET /api/v1/trade-desk/events`, `useTaskStream` | The pattern for the chart stream |
| Boxes, value areas, twins and exits | `trade_desk/boxes.py`, `trade_desk/backtest/boxes.py` | Boxes, levels and flag grading |

## Choices

- **KLineChart 10.1** (`klinecharts`, Apache-2.0), not TradingView Lightweight Charts. It has
  built-in drawing tools, so you can draw your own boxes and lines on the same chart, and
  custom drawings ("overlays") for the system's boxes and levels.
- **15-minute bars by default**, as in the video, with 1- and 5-minute views. A flag uses
  completed bars only, the same no-look-ahead rule as the backtest.
- **One module for boxes and levels, used by both the backtest and the live chart.** The live
  chart then draws exactly the rule that was tested.
- **Charts are pulled, not pushed to Discord.** No Discord alerts for flags until a flag type
  passes.

## Phases

Each phase ships on its own with tests. Phase 0 is a precondition.

0. **The shadow-mode update is committed** (2026-11-06). It runs once the server is restarted.
1. **Boxes and levels in the repo**: `src/services/trade_desk/boxes.py`
   - Port from the research scripts: impulse, range and base boxes, both entries, the
     tidy-box option, the weekly value area, and the limit-entry twins.
   - Add `scripts/backtest_boxes.py`, which reproduces the 2026-11-05 results on the saved
     bars (same seeds), and document it in [strategy-backtest.md](strategy-backtest.md).
   - Tests: fixed synthetic bars with known boxes, no look-ahead, and fills at gaps.
2. **Chart snapshot API**: `GET /api/v1/trade-desk/chart/{ticker}?interval=15m` (done,
   `trade_desk/chart.py`).
   - Returns bars (from `get_cur_kline`, up to 1,000), plus overlays (boxes, VAH, VAL,
     prior-day high, low and close, plan stop and target, alerts, NX) and the flags.
   - Flags are `armed`, `watching`, `open` or `closed`; `boxes.pending` gives the first two
     with the same rules, and a test checks that an armed flag is the trade the next bar fills.
   - It reads bars on its own moomoo connection, so it cannot disturb the advice quotes or
     shadow mode. It keeps the newest 5 tickers' subscriptions and releases the rest at the
     next request; phase 4 adds the timed idle release.
   - Right after a new subscription OpenD returns only its latest 60 bars; the rest load within
     about a second, so the read is repeated for up to three seconds. Checked on OpenD on
     2026-11-05: 1,000 bars at every interval in 1.3–2.2 s, regular session only.
   - Same login as the other Trade Desk routes. Tests use a fake provider.
3. **Chart tab on the Trade Desk page** (done, `ChartPanel.tsx`, `chartOverlays.ts`).
   - KLineChart with candles, volume and NX, and a picker for any stock or for your holdings,
     watchlist and trade ideas.
   - The overlays: a labelled box, a labelled level line with its price on the axis, and a
     mark for each trade taken. Short labels on the chart; the side panel lists full names.
   - A side panel lists the flags with their grades; selecting one draws its entry, stop and
     target. It refreshes every minute until phase 4 streams.
   - Checked with a scratch web build and screenshots on real SPY bars, light and dark, desktop
     and phone width, before anything is built into `static/`.
4. **Live updates.**
   - Subscribe quote, 1-minute and 15-minute bar push for open charts only. Each moomoo
     subscription counts against the account's limit, so cap open charts at about 5 and
     release idle ones after 10 minutes.
   - Stream updates to the page over a separate in-memory SSE route, kept out of the events
     table.
   - Keep the two fixes shadow mode needed: no per-push pandas tables, and the SDK's file log
     at INFO.
   - Measure push delay and CPU in market hours, as for shadow mode.
5. **Flags, your marks, and grading.**
   - Each flag (box retest, fade at a box) is stored with entry, stop, target and direction
     when it triggers.
   - You can mark "I'd go long / short here" with your own stop and target, or keep a box you
     drew.
   - Grading:
     - The backtest's exit rules, against limit-entry twins at random times.
     - One table per source: system flags by kind, and your marks.
     - A verdict after 30 finished trades, using the month-clustered t at its critical value.
6. **Risk panel.** Loss at each stop as a percentage of the account, the total if every stop is
   hit, and a gap scenario. Percentages only.
7. **moomoo add-on.**
   - A custom indicator script that draws the boxes on moomoo's own charts. You paste it into
     the app and check it, since moomoo's drawing functions are not documented in the API.
   - Optionally, moomoo price alerts at stops or box edges through `set_price_reminder`. This
     changes your account's settings, so only with your OK each time.

## Risks and limits

- **The method is not shown to work.** Box entries did not beat random timing over six
  years, and the value-area check did not help. The chart is a map, and its flags carry that
  result until forward grades say otherwise.
- **Subscription limits:** each open chart uses about three subscriptions, so too many open
  charts could crowd out the shadow feed. Hence the cap and the idle release.
- **One server and SQLite on network storage:** grading writes are small and batched, and
  live bars stay in memory.
- **Order flow is not included.** The footprint and delta the video uses need tick data we
  don't keep.
- **Deployment:** the live page needs a web build into `static/` and a server restart. Both
  remain your call.

## Done when

- The chart shows live bars within about a second of moomoo for 5 open charts, with the
  boxes and levels matching the backtest module.
- Flags and your marks are graded in one table.
- The risk panel matches the holdings page.
