# US watchlist and market monitoring

The existing scheduler generates saved watchlist analyses and a US market report.
The Reports page groups those saved analyses by their recorded market session.
The US market report adds a deterministic session scan to both LLM output and
fallback reports, using the same data in the LLM prompt and saved structured payload.
Your configured generation backend (including local Codex) still writes the analysis.
The market scan itself requires no LLM API key. If generation times out, exits
unsuccessfully or returns empty output after a usable scan was collected, the
report retains the scan and an explicitly labelled deterministic summary. The
failure remains in diagnostics and the saved scan warnings. Configuration, login
and approval errors still surface instead of being hidden by this fallback.

## Local configuration

```dotenv
TZ=America/New_York
MARKET_REVIEW_REGION=us
MARKET_REVIEW_ENABLED=true
SCREENING_ENABLED=true
SCREENING_US_UNIVERSE=auto
SCHEDULE_ENABLED=true
SCHEDULE_TIMES=08:45,10:00,12:00,14:00,15:30,16:30,19:30
SCHEDULE_RUN_IMMEDIATELY=false
TRADING_DAY_CHECK_ENABLED=true
AGENT_EVENT_MONITOR_ENABLED=true
AGENT_EVENT_MONITOR_INTERVAL_MINUTES=5
```

Set `STOCK_LIST` to your watchlist. Start the process with `TZ=America/New_York`
already in its environment; the scheduler uses process-local wall time. This
keeps the schedule on New York time through daylight-saving changes. The local
`local/run-us.sh` launcher, when present, loads `.env` before re-executing Python.
For a standard checkout, start with `TZ=America/New_York python main.py --serve-only`.
Restart an existing server after installing Python code/configuration changes.
Do not start a second scheduler alongside the server's scheduler.

| New York time | Purpose |
|---|---|
| 08:45 | Premarket watchlist and discovery |
| 10:00, 12:00, 14:00, 15:30 | Intraday updates |
| 16:30, 19:30 | After-hours updates |

The exchange calendar determines sessions, holidays and early closes; the above
clock times stay fixed, so a 14:00 report on an early-close day is an after-hours
report. After-hours ends at 20:00, or four hours after an early close (17:00 after a
13:00 close); the 19:30 run on an early-close day is therefore outside any session.
Scheduled reports are periodic snapshots, not a streaming terminal.
Existing Alerts rules are evaluated every five minutes when the event monitor
is enabled. Enabling the monitor does not create thresholds or notification channels;
configure watchlist price/percentage rules in Alerts. Stale US prices are skipped,
and provider quote timestamps are used for trigger deduplication (stored in the
server's local time, like trigger times). Stale-quote skipping applies to US quotes
only; A-share, Hong Kong and other markets keep their existing alert behavior.

## Data semantics

- The quote API retains `provider_timestamp`, `fetched_at`, `quote_session`,
  `is_stale`, `data_quality` and `currency`. `update_time` is provider time,
  not retrieval time; stock profiles mark stale or unverified US quotes partial.
  A US fallback quote without a provider timestamp or supported session is a
  reference price and cannot trigger price/percentage alerts.
- Yahoo and Longbridge US quotes use the same session contract: Longbridge's regular
  `timestamp` and its pre/post-market quotes map to the same regular/extended price
  and time fields. A quote with valid session metadata is fresh for up to 20 minutes;
  `REALTIME_CACHE_TTL` does not apply a second, shorter threshold to it.
- US index quotes (SPX, IXIC, DJI, ...) are fresh only during the regular session and
  only when Yahoo returns the index's regular-market time. Index alerts therefore
  require a timestamped index source; otherwise the index quote is a stale reference.
- Saved stock reports and notifications retain the quote's session, provider time,
  freshness, reference close and session move. The Web report shows the provenance
  beside its price. Missing or zero session changes never fall back to a daily
  candle's percentage change.
- US watchlist quotes choose the provider's matching premarket, regular or
  after-hours price and timestamp. Missing session data remains explicitly stale.
  A timestamp without a valid matching price cannot make a retained price fresh;
  its provider time and percentage change remain unavailable.
  Secondary providers may supplement valuation fields, but cannot fill premarket/
  after-hours volume ratio, turnover rate or amplitude with regular-session metrics.
- Premarket changes are relative to the prior regular close; regular changes use
  the prior close; after-hours changes are relative to the same day's regular close.
  In the session scan, when Yahoo's daily series omits the prior session, the prior
  close is that session's last regular five-minute bar, used only if the bar ends
  within 10 minutes of the session close (the same rule as the after-hours reference).
- Extended-hours reference closes must have their own timestamp near the expected
  regular close (including early closes and holidays). If that reference cannot be
  verified, the current price remains available but the change/percentage is unavailable.
- Reports and notification templates separate the session quote from a dated regular-session
  daily-bar section. Historical OHLC/volume are never presented as premarket or after-hours
  activity; an intraday daily bar is labelled incomplete at analysis time.
- When a fresh regular quote adds a new trading day, comparisons advance to the previous
  completed trading session (including weekends and holidays). The provider's previous close
  anchors intraday price changes; volume comparisons require the matching historical bar.
  Missing prior-session bars leave volume comparisons unavailable, and old daily volume
  ratios are not carried into the current session.
- Only verified, fresh regular-session quotes can augment regular daily candles.
  Missing timestamps, stale quotes and extended-hours quotes cannot create or
  overwrite today's regular daily candle. Stooq daily fallback prices remain
  available as historical reference and are marked stale for monitoring. This guard
  also applies to the final report's daily OHLC context; extended-hours prices remain
  in a separate session-labelled quote block with provider time and freshness.
  Yahoo daily history for watchlist analysis and optional screening enrichment
  includes the current regular candle only after the exchange's verified close,
  including early closes; it excludes the unfinished current candle before then.
- Yahoo index figures retain their `daily_bar_date` in prompts, report text and
  saved structured data. The report UI labels them as regular-session daily bars:
  a prior-session index move is not today's premarket move, and a current-day bar
  may still be incomplete.
- Each market scan captures one timestamp before resolving its universe, so a scan
  crossing the open or close keeps its session label and data aligned.
- The scanner uses five-minute bars with extended hours included. A bar older
  than 20 minutes, lacking a timezone, or from another session is excluded.
  Yahoo data may be delayed; this is a research feed, not a broker execution feed.
- Discovery lists stocks outside `STOCK_LIST` moving at least +2% or -2%, with
  price at least $5 and estimated session dollar volume at least $1M. These are
  observed movers, not predictions or verified catalyst claims. Zero/missing
  session volume cannot pass the liquidity filter. Yahoo may provide extended-hours
  prices with zero reported volume. During premarket/after-hours, reports separately
  show up to ten outside-watchlist price movers (price at least $5, absolute move
  at least 2%) with **liquidity unverified**, ordered by absolute move. This list
  does not establish tradable liquidity or relax manual screening's $1M filter.
  Both reports and manual screening disclose missing/unverified session volume.
- Dollar volume is estimated from bar close times bar volume. Full-day relative
  volume is deliberately not substituted for time-of-day relative volume.
- Reports show advancers/decliners in the successfully sampled universe and the
  requested/available/excluded counts. This is not whole-exchange market breadth.
- Sector rankings use 11 explicitly labelled sector ETF proxies. They are not
  rankings computed from every constituent stock. Fresh data is required for each.
  When no sector proxy data is available (screening disabled, closed session or
  failed scan) the market review states that sector rankings are unavailable.
- Holidays, unsupported overnight hours, missing calendars, timeouts and provider
  failures produce an unavailable/partial explanation, not fabricated movers.
  If every scanned symbol is missing or stale, reports retain the requested and
  excluded counts and the provider coverage explanation, with empty mover lists.

## Bar interval and report frequency

The scanner currently uses a fixed five-minute interval. This is the granularity
of its input data, not how often it generates reports; `SCHEDULE_TIMES` controls
report frequency. Session percentage moves still compare against the relevant regular
close, not against the preceding five-minute bar. Five-minute bars are suitable for
the periodic research reports.
The last available bar close supplies each scan price, and bar volume contributes
to estimated session dollar volume. A different interval can therefore change
mover rankings, liquidity-filter membership and sector snapshots. Watchlist quotes
come from a separate quote request, and daily technical indicators use daily candles.

Yahoo also supports one-, two-, fifteen-, thirty- and sixty-minute intervals
([provider documentation](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)).
Finer bars do not eliminate provider delays or refresh saved reports automatically.
Changing the scanner interval would also require checking its freshness cutoff,
regular-close reference window and source labels; it is not an exposed setting.

## Universe and manual screening

`SCREENING_US_UNIVERSE=auto` tries explicit `SCREENING_US_TICKERS`, then the S&P 500,
then the bundled large-cap list. Use `env` with comma-separated tickers for your
own broader discovery universe, `sp500` to require S&P 500 lookup, or `default` for
the small bundled universe. This universe is independent of your watchlist.
A smaller fallback universe is visible in the report's requested symbol count.
Discovery returns share-class symbols in the application's dotted form (`BRK.B`),
so its Analyze action and watchlist exclusion use the same identity. Yahoo requests
translate those symbols to the provider's hyphenated form (`BRK-B`), including
fundamentals requests. US tickers such as `HKD` retain their US identity rather
than being interpreted as Hong Kong codes.
Realtime quotes retain the dotted application identity and US market classification,
so share-class stocks use the same session selection and freshness checks as other US stocks.

The Screening page now offers US stocks and compatible `us_gainers` /
`us_decliners` strategies. These score research candidates using momentum/reversal
and liquidity factors; they are not pure percentage-change leaderboards. The market
report's discovery lists separately sort the largest qualifying session moves.
Existing China strategies retain their own market scope.
Market selection and Run wait until the strategy list finishes loading, preventing
a late response from pairing US stocks with a China-only strategy.
Without an API model configured, screening falls back to deterministic ranking;
market/watchlist report generation continues to use the selected local Codex backend.
Daily technical enrichment is optional and distinct from the session snapshot.
Screening candidates retain optional `quote_session`, `provider_timestamp`,
`reference_price` and `is_stale` fields in results and saved history. US rows show
quote time in New York and identify the session and reference close for each move;
legacy results without those fields show time/session as unavailable.

## Verification and rollback

Regression tests cover premarket/regular/after-hours reference prices, early closes,
holidays, stale/missing quotes, partial coverage, watchlist exclusion, scanner
strategy execution, missing extended-hours volume, completed daily closes,
stale-alert suppression, report fallback and report-body retry.
Live provider availability and browser behavior still require deployment checks.

Set `SCHEDULE_ENABLED=false` and `AGENT_EVENT_MONITOR_ENABLED=false` to pause
background work; set `SCREENING_ENABLED=false` to disable scanning. Restart the
service when reverting Python changes. Preserve other local report/UI edits.
