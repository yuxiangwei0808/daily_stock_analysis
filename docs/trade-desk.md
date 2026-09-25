# Trade Desk

Trade Desk adds US stock/ETF options comparisons, follow-up discussion, selective
opportunity alerts, paper fills, and a manual-live journal at `/trade-desk`.
It never submits, unlocks, cancels, or modifies a broker order. Account value and
personal loss caps are not required. Allocation is optional, and quantities are
only estimated when capital requirements are known. Broker margin and strategy
permissions must be checked in the moomoo order preview.

## Setup

The feature is enabled by default. Existing reports, screening, and Futu holdings
imports retain their existing contracts. The Trade Desk quote integration uses the
optional **moomoo** SDK; it does not replace the globally pinned `futu-api` package.
For a native moomoo connection, install `moomoo-api` into the same Python
environment, run moomoo OpenD, and sign in locally. If only the existing pinned
Futu SDK is installed, diagnostics identify the compatibility path explicitly;
that path still requires an authenticated live quote test. Use the Trade Desk connection diagnostics to
verify SDK access, stock/OPRA quote rights, source times, and available quotas.
App quote permissions do not prove API quote access.

```dotenv
TRADE_DESK_ENABLED=true
# Optional overrides; otherwise the existing FUTU_OPEND_HOST/PORT are used.
# TRADE_DESK_OPEND_HOST=127.0.0.1
# TRADE_DESK_OPEND_PORT=11111
# Optional: RSA-encrypt the OpenD protocol (same key as OpenD's rsa_private_key)
# TRADE_DESK_OPEND_RSA_KEY_FILE=/path/to/opend_rsa.pem
# Public origin for links in Discord, e.g. https://your-private-dashboard.example
# TRADE_DESK_PUBLIC_URL=
```

OpenD accepts API connections from any local user on its listening address. On a
shared machine, set OpenD's `rsa_private_key` and `TRADE_DESK_OPEND_RSA_KEY_FILE` to
the same private key (readable only by you) so other users cannot use your logged-in
session; health reports `protocol_encrypted`. The existing Futu holdings import does
not use this key.

No gateway or paid feed is installed automatically. Missing SDK/login/permissions,
stale quotes, unsupported contracts, and unavailable session timing produce an
explicit unavailable state. Yahoo research quotes never fill missing live option
quotes. Replay is deterministic **synthetic example data**, not a historical
backtest, current market data, or evidence of a profitable strategy. Replay plans
cannot be recorded in the manual-live ledger.

Configure Discord through existing Settings (`DISCORD_WEBHOOK_URL` or the existing
bot-token/channel configuration). Enable proactive suggestions and Discord delivery
in Trade Desk preferences. New-idea delivery defaults to three per New York trading
day and a 60-minute symbol cooldown. Position and data-outage alerts have separate
deduplication and do not consume this idea limit. Delivery attempts are persisted;
failures retry at most three times. Proactive discovery, its model runs (at most one per 15-minute cycle) and idea delivery happen only during the regular session; pre-market and after-hours produce none. Without Discord configuration the in-app journal
remains available. `TRADE_DESK_PUBLIC_URL` should be the address accessible on your
phone; localhost links on another device will not reach this server.

## Using it

1. Open Trade Desk directly or use **Explore trade strategies** from a report or
   screening result. Choose live or clearly labeled replay data.
2. Enter a ticker, optional allocation, direction, horizon, and question. Advanced
   inputs include expiry/strategy filters, explicitly owned shares, broker-supplied
   margin per unit, and fee/rate/dividend assumptions. Rates and dividend yields
   are decimals (0.04 means 4%).
3. Compare exact option legs, entry debit/credit, capital, payoff, assignment
   exposure, model probabilities, scenarios, and evidence confidence. The default
   fee is an illustrative $0.65 per option contract per fill, not a moomoo fee quote;
   enter your actual fee assumption. Rate and dividend defaults are explicit zero
   assumptions, not fetched current yields.
4. Ask follow-up questions. Each answer is a new version linked to its own source
   quotes and assumptions. A model error leaves labeled numerical comparisons;
   it does not silently become an AI recommendation. Follow-ups retain revised
   inputs shared by the displayed alternatives, including allocation and direction,
   while earlier versions retain their original inputs.
5. Select a candidate and monitor its price levels. The plan saves the trigger,
   invalidation, target and exit levels exactly as displayed; clearing a suggested
   level removes it. A price alert only confirms that price condition; other entry
   conditions still require review.
6. Simulate paper entry/exit, or execute manually in moomoo and record each actual
   fill, quantity, price, fees, and time. Record partial fills individually.

## Numerical meaning

Expiration payoff is calculated from the option/share legs, entry prices, and
fees. Bounded and unlimited results are explicit; JSON never represents infinity
as a number. Covered calls include the stock purchase unless shares were explicitly
supplied; existing shares still contribute economic exposure. Selected plans retain
the full explicitly supplied share quantity, and paper call quantities cannot
exceed that recorded coverage. Owned shares alone do not open a plan: it stays
*watching* until a fill is recorded, and it is *closed* once its option legs are
flat and the owned shares are back at the supplied quantity (or were delivered by
exercise/assignment). A paper close never sells explicitly owned shares.

Pre-expiry scenarios are theoretical American-option binomial estimates using
fractional remaining time. The probability field is a **model-implied probability**
under a stated IV-based lognormal, risk-neutral distribution and stated horizon.
It is not a calibrated forecast, an option delta, an LLM confidence score, or a
historical win rate. Contract timing uses the exchange calendar and explicit provider cutoff metadata.
For normal sessions, a finite list of late-close ETF classes uses the
[NYSE Arca published schedule](https://www.nyse.com/publicdocs/nyse/markets/arca-options/Options_Late_Close_ARCO.csv)
(reviewed September 22, 2026). Ambiguous ETF cutoffs and late-close ETF early-close
contracts remain unavailable without verified timing; no cutoff is guessed. Whether
an underlying is a fund comes from its OpenD market snapshot (fund/equity markers);
when the type is unknown, date-only contracts are treated as unavailable. A live
snapshot is source-verified only with a two-sided, non-crossed underlying bid/ask;
otherwise it is marked stale with `underlying_bid_ask_unavailable`. Contracts past
their cutoff are excluded and do not consume quote subscriptions.
Missing necessary inputs leave probability unavailable. Intraday exits
and expiration outcomes are separate. Volatility, jumps, spread, early assignment,
and execution can make realized outcomes differ substantially.

Low/medium/high confidence describes the strength of supporting evidence. Codex
can explain and select server-calculated candidates, but cannot overwrite their
payoff calculations or invent contract IDs. Live prices are checked again after
model generation; the displayed contracts themselves are re-priced (strikes are
never re-selected) when their quotes aged during the model call, and again when a
live plan is created from a comparison older than 30 seconds, so the plan records
current prices. A setup is stale only when the underlying moves more than 1%, a leg
lacks a fresh quote, or the position's entry cost changes by more than 5% of its
size (the larger of the entry debit/credit and the bounded maximum loss); a
one-cent tick on a cheap wing is not material. Only adverse changes count (a
higher debit or smaller credit); a cheaper entry just updates the numbers.
Candidates the model did not select carry no price-based reasoning and are simply
re-priced. A stale comparison offers **Run again**, which re-submits it as a new
version in the same conversation. Calculated candidates start at low evidence
confidence; verified quotes are a precondition, not evidence.

Live snapshots include up to 20 daily and 26 fifteen-minute underlying bars from
OpenD (times in ET; intraday times are bar end times; forming bars are marked
`partial`) as model context only. The option chain request covers only the nearest
expiries used, and an OpenD request timeout (common on a symbol's first request) is
retried on the same connection.

OpenD's market-snapshot `update_time` is the last **trade**, not the last quote, so
option bid/ask freshness comes from the order book. OpenD leaves the book's server
time empty until a subscribed book changes; a two-sided book held by this
connection's live ORDER_BOOK subscription is then treated as observed at request
time and the snapshot carries `order_book_time_unreported_observed_on_live_subscription`.
Option contracts use no QUOTE subscriptions (only order books), which keeps quota
use low. Same-day contracts of late-close ETFs (16:15 ET cutoff) are valid for
intraday comparisons; swing candidates always use a later expiry. Credit spreads
sell the out-of-the-money strike. Monitoring runs in
a separate worker and continues independently of the model. Follow-up calculations use the existing Codex owned-process runner, with cancellation and deadline cleanup; advice stores the actual tool calls and effective per-candidate requests.

When `TARGETED_GENERATION_BACKEND` is set (see the LLM configuration guide), automatic
proactive scans are reviewed by the routine `GENERATION_BACKEND` without tools, so
they do not spend the targeted model; they cannot recalculate alternatives. Manual
comparisons and follow-ups keep the Codex advisor, and each `SECOND_OPINION_BACKENDS`
model independently answers trade (with one supplied candidate) or wait on the same
calculated candidates. The advice stores this as `panel` (`opinions`, `agreement`:
`agree`/`split`/`unavailable`); agreement requires the same action and strategy.
The panel is advisory and never changes candidates, triggers or plan eligibility.

## Watchlist groups on Home

When OpenD is connected, `GET /api/v1/stocks/watchlist/groups` returns the
user's custom moomoo watchlist groups in app order (US codes without the
`US.` prefix; at most nine groups because OpenD allows ten watchlist requests
per 30 seconds; cached five minutes). Home shows them as tabs over the
existing `STOCK_LIST` rows, plus "Ungrouped". Groups only filter the view;
batch analysis still covers the whole watchlist.

`GET /api/v1/stocks/watchlist/quotes` quotes the US tickers in `STOCK_LIST`
with one batched OpenD market snapshot (cached 10 seconds). Each quote has the
last regular-session price and change versus the previous close; during
pre-market or after-hours, `extended` adds that session's price. Home polls it
every 15 seconds while the tab is visible.

## Market pulse (market-hours watch)

With `MARKET_PULSE_ENABLED=true`, the Trade Desk worker (leader only, regular
session only) watches the US tickers in `STOCK_LIST`:

- Every 60 seconds, one batched OpenD snapshot. A day change crossing a level
  in `MARKET_PULSE_MOVE_LEVELS` (default `3,5,8` %) emits `market_move` once
  per level per day, reporting only the highest level crossed; a move of
  `MARKET_PULSE_FAST_MOVE_PCT` (default 2 %) within 15 minutes emits a fast
  move at most every 30 minutes per stock. Move alerts cite the latest
  headline seen for the stock.
- Every 10 minutes, in the background, Google News for the next 11 tickers
  (the whole watchlist every ~30 minutes). New headlines from the last two
  hours are rated 0–3 for materiality by the routine generation backend in one
  call; only 3 ("major") emits `market_news`, at most 20 per day. If the model
  is unavailable, a keyword rule (earnings, guidance, M&A, regulators, halts,
  offerings…) decides. The first look at each ticker after a restart only
  records existing headlines.

Both event types use the worker's deduplicated, retried Discord delivery
(labelled "Market move" / "Market news") and never create plans or orders.

## Options ideas from stock reports

With `TRADE_DESK_REPORT_IDEAS=N` (default 0 = off), once a scheduled report
run finishes during the regular session (at least five fresh US reports, none in
the last three minutes), the worker submits comparisons for the N reports with
the strongest directional view (score furthest from 50; bullish ≥ 60, bearish
≤ 40, otherwise neutral) with the report as evidence. They run on the routine
model without tools. When all finish (or after 30 minutes), one `options_ideas`
Discord message lists, per stock, the leading candidate's legs, debit/credit,
max loss/gain, break-even and model probability, or "wait" with the reason.
Runs finishing after the close are skipped because option quotes are no longer
tradable. Ideas are research only; nothing is ordered.

## Your own trade plan and fresh news

A request may carry `plan_legs`: up to four legs, either objects
(`{side, right, quantity, strike, expiry}`) or lines such as
`buy 1 call 230 2026-10-16`, `sell 1 call 240 2026-10-16` or `buy 100 stock`.
The server subscribes those exact contracts (the provider accepts
`right:strike:YYYY-MM-DD` beside contract ids), prices them at the displayed
bid/ask with the same payoff, probability and scenario engine, and lists the
result first as "Your plan" (`strategy: custom`) beside up to three calculated
alternatives. Without an explicit expiry, the plan's expiry is used for the
alternatives too. If the plan cannot be priced (missing or one-sided quote,
mixed expiries, short stock) the advice stores `plan_error` and still compares
alternatives. Codex may price a trade described in the message by passing
`plan_legs` to the comparison tool, but only with strikes the user wrote.

With `FREE_NEWS_SOURCES` set, a user-requested comparison fetches recent
headlines (Google News RSS, Yahoo Finance, and Finnhub when `FINNHUB_API_KEY`
is set; cached 10 minutes) into the snapshot evidence as dated `news` items.
`about_ticker=false` marks general market stories from ticker feeds. Headlines
are context, never verified catalysts; automatic scans do not fetch them.

## Positions and operational behavior

Paper fills buy at ask and sell at bid with configured fees and displayed-size
checks. A net-debit limit is compared with the **whole simulated fill in USD**;
negative values express a minimum net credit. Simultaneous multi-leg availability
is an explicit simulation assumption, not a guaranteed broker combo fill.

Only fills change quantities. Alerts never create entries or close positions.
Paper plans cannot record broker exercise/assignment, so an expired paper position
is settled explicitly: **Settle expiry** closes its expired option legs at intrinsic
value for the underlying price you enter (share delivery is not simulated).
The paper net-debit limit is for the whole fill in USD (not per share) and excludes
fees.
Unknown marks produce unavailable P&L, not zero. Expiration and assignment require
broker reconciliation; record resulting option/share fills and reconciliation
notes. Record the exercised or assigned option leg first, followed by its matching
resulting share fill; unmatched directions and quantities are rejected. Fill order
is preserved even when the broker reports identical timestamps. Reconciliation
cannot clear missing or partially recorded share deliveries or remaining expired
option legs. A newly recorded exercise/assignment requires reconciliation even
when its broker timestamp predates an earlier acknowledgement.
Paper (live quotes), paper on synthetic replay, and manual-live outcomes are
reported separately and are unrelated to
the existing daily-stock backtest. A recorded win rate describes only the sample
of closed journal positions.

Each plan can pause/resume its alerts or, while still unfilled, be archived. The
independent worker uses a database lease (a process that gains the lease also
fails jobs left running by a crashed leader), releases resources at shutdown,
prioritizes existing positions, and bounds monitored plans/subscriptions. Unfilled
plans stop being monitored once their contracts expire. Live data-outage alerts are
raised only during the regular session, when option quotes are expected; outside it,
live monitoring pauses silently. SSE carries persisted event IDs; reconnecting
clients resume from `Last-Event-ID`. A new connection without a cursor starts at the
newest event (current state comes from the REST endpoints); `?after=0` requests a
full replay. Saved
advice and positions remain available through data/model outages. Held option
contracts take priority over nearby strikes within the quote limit; plans sharing
a stock and expiry share their required quotes. Discord links open the referenced
saved advice or position, including advice outside the latest list; message text
cannot mention `@everyone`, `@here` or users. Proactive discovery considers only
US tickers from the watchlist, and one failing symbol does not end the cycle. A
"volatile" view defaults to long straddles/strangles only; iron condors always use
distinct short strikes; candidates with no possible expiration profit are omitted.

## API and persistence

All `/api/v1/trade-desk/*` routes inherit the existing administrator session guard.
The API supports health, catalog, advice submission/status/cancellation, plans,
paper fills, manual fills, positions, journal, outcomes, preferences and SSE events.
`POST /advice` accepts `parent_advice_id` for a follow-up version. `POST
/plans/{id}/reconcile` records reconciliation notes after actual fills are entered. `POST
/plans/{id}/paper-settle` (`underlying_price`) settles expired paper option legs.
`GET /outcomes` returns `paper`, `paper_replay` and `manual_live` buckets.
OpenAPI at `/docs` describes request validation.

Additive `trade_desk_*` tables hold conversations, advice versions, plans, fills,
events, preferences and worker leases. Existing `DecisionSignal` records remain
research evidence rather than option orders or positions. Interrupted model jobs
are marked failed on worker restart so they can be explicitly retried.

## Verification and rollback

See [the validation record](trade-desk-validation.md) for local test results,
browser evidence, remaining repository-wide failures, and live-integration gaps.

Offline tests cover numerical fixtures, model assumptions, stale data, lifecycle,
authentication, cancellation, paper/live isolation, partial fills, replay, leases,
and delivery failure. Live OpenD/OPRA access and real Discord delivery require
separately configured external services and must not be claimed verified from
fixture tests.

Set `TRADE_DESK_ENABLED=false` and restart to stop the Trade Desk worker and new
advice. Preserve its tables when rolling back the feature; existing research and
holdings services continue independently. No automated broker execution exists.

## Reference contracts

- [Moomoo option-chain API](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-option-chain.html): chain membership is not a live bid/ask subscription.
- [Moomoo snapshot schema](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-market-snapshot.html): option IV is percentage-valued and converted explicitly into decimal volatility.
- [OIC technical information](https://www.optionseducation.org/referencelibrary/faq/technical-information): pricing assumptions and theoretical option values.
- [FINRA options guidance](https://www.finra.org/investors/investing/investment-products/options): assignment and option position obligations.
