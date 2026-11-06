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
bot-token/channel configuration), then enable Discord delivery in Trade Desk
preferences. New trade ideas come from the scheduled "Swing trade opportunities"
scan below (the earlier 15-minute proactive options discovery was removed: it
spent a model call per cycle and, in practice, always concluded "wait").
Delivery attempts are persisted; failures retry at most three times. Long
messages are sent in parts split at blank lines (an idea is never cut in half);
a retry sends only the parts that failed. Without Discord configuration the in-app journal
remains available. `TRADE_DESK_PUBLIC_URL` should be the address accessible on your
phone; localhost links on another device will not reach this server.

## Using it

The **Ask about a stock** tab has three parts. The ask panel at the top folds away
like a dropdown (it opens by itself when there is no history, when a link brings a
ticker, or from the sidebar's **Ask**, which opens this tab while the desk is on; the
original stock chat stays at `/chat` and still opens from a report's AI follow-up button); its quick row is the ticker (suggestions: what you hold, then what you asked
recently), your question (Ctrl/Cmd+Enter sends) and **Ask**; under it, the ticker's latest active report
verdict (action, score, stop/target, date and the reason; from the AI-signal store), with data mode, direction,
horizon and **Trade** beside it and everything else under **More options**. Strategies default to
**Auto**: the desk compares the four that fit the chosen direction and what to trade (the panel names
them; `GET /catalog` returns them as `defaults`, and per Trade choice as `defaults_by_instrument`),
plus covered calls or a protective put on shares you hold; **Pick my own** lists every strategy
grouped by family, shares first.

**Trade** is Auto (shares and options), Shares or Options. Auto puts the share trade that fits
the view first — buy shares for a bullish view, short shares for a bearish one, both when there is
no view — then option structures, four in all; neutral and volatility views are option structures
only. **Horizon** is optional: Auto (default), Intraday (out by the close), Interday (1–5 trading
days), Swing (1–6 weeks) or Position (1–6 months). Left on Auto, a horizon or what to trade named
plainly in your question is used ("day trade", "this week", "a few weeks", "long-term", "LEAPS";
"shares", "options", "calls"); two horizons at once stay Auto, and a choice on the form always wins.
Under Auto, share trades are swings and option structures alternate between intraday and swing.

A **share trade** (`long_stock` / `short_stock`) needs only the stock's quote and its recent
range, so it is compared even when no option is quoted (before the open, or a stock without
options). Its stop is a number of daily ranges from the entry by horizon (0.5 intraday, 1
interday, 1.5 swing — the ideas' rule — and 3 position) and its target is twice that risk; the
daily range is the ATR(14) of the snapshot's daily bars, else 1.2× the implied daily move from the
at-the-money options, else 2% of the price. The time stop closes it after 1, 5, 20 or 63 sessions.
It shows the shares (your allocation's worth, else 100), the capital (full cost; a short shows Reg T
50% initial margin, borrow fees excluded), the dollars lost at the stop and made at the target,
the chance of a profit at the time stop (lognormal, implied volatility when quoted, else realized)
and the chance the target comes before the stop for a driftless walk (about a third for a 2R
target). A short warns about margin, borrow and unbounded loss; a day trade about the
pattern-day-trader rule. A plan from a share trade is monitored on its stop, target and time stop
unless the model set others, and paper fills buy or short the shares at the ask or bid. Sending folds the
panel so the answer is in view. **Your questions** lists your requests grouped by
stock (newest group first, a filter once there are more than three); the answer to the
selected one sits beside it: your position if attached, the desk's verdict and any second
opinions, then the compared strategies side by side (cost, max gain and loss, breakeven,
chance of profit, expiry; a star marks the desk's pick). The pick, or the first strategy,
opens below with the desk's reasoning and levels, the key numbers, the legs, the payoff
chart and its plan, the model's technical details folded away; caveats that apply to every
strategy are listed once. The NX tunnel and social/YouTube background fold away under the
answer, with the follow-up box at its end. In **Broker
holdings**, **Ask** on a position opens the panel on that ticker with your position
attached. The list shows current requests; **Archive** lists requests
whose options have expired, that were marked stale, that found nothing (after
their day) or that are over a week old. Nothing is deleted: archived requests stay
in `trade_desk_advice` for the track record and backtests (`GET /advice?scope=archive`)
until you delete them. The bin icon on a request, or **Delete all archived** in the
archive, removes requests permanently after a confirmation; queued/running requests
(cancel first) and requests a monitored plan was created from are kept. Journal
entries stay as the log, plus an `advice_deleted` entry.
**Your position.** With broker holdings set up, typing a ticker you hold shows
"Use my position" (on by default; live data only). The request then carries your
shares, open option legs (quantity, unit cost, current mid, P&L on cost, trading days
left) and your alerts for that ticker, read-only from the last sync with fresh quotes.
Owned shares prefill the share count; with direction "auto" and no strategies of your own,
the comparison becomes a protective put and (with 100+ shares) covered calls beside a bull and a bear
debit spread, instead of the generic directional set. A single
held option position (no plan or expiry of your own) becomes the `custom` candidate,
priced from the current mid as "holding from here", and the model answers for it first:
hold, take profit or close, reduce, hedge or roll (a roll shows as closing it plus one of
the other candidates; it is not priced as one trade). Account totals and cash are not
sent. Only requests you make get the position; automatic follow-ups, which can reach
Discord, never do. The answer shows a "Your position" panel with what was used.

**Your NX tunnel.** Live answers on US tickers include your NX indicator on daily
bars (fast tunnel = EMA of highs/lows over 26 bars, slow = 89; see
`src/services/nx_tunnel.py`): the tunnel edges and where the price sits. The model is
told it is your chart framework with no tested edge on its own — it may use the edges
as trigger, invalidation and stop levels and mention them, but not pick or reject a
trade because of NX. The answer shows a "Your NX tunnel" panel. Replay requests skip it.

When no contracts can be compared, the answer says why (no quotable options, or
none passing the filters, with the provider's notes) instead of a generic message. Live
quotes taken outside 9:30 AM–4:00 PM ET say so instead: US options trade only in the
regular session, so before the open or after the close the broker has no option bid/ask
to price ("ask again after the open"). Your own question still gets an answer: the
routine model (with its fallback chain) writes a short view from the ticker's price, news,
position and NX evidence — the move you ask about, the levels that matter, what would
change the view and what to check once options can be priced — marked "The desk's view (no
priced strategies)" (`llm_status` `view_only`). Automatic follow-ups do not.

The desk's routine model calls (the daily ideas, news ratings, alert wording, routine
reviews, YouTube picks) use the configured fallback chain (`GENERATION_FALLBACK_BACKEND`,
e.g. `codex_cli,litellm`), so a rate-limited Claude Code answers through Codex and then the
default models; second opinions stay with the backend they name.

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

Each candidate's **Expiry payoff** panel reads the exact P/L at any price: the
server sends the payoff's corner points (0, each strike, far tail samples) and the
page interpolates between them, which is exact because the expiry payoff is linear
between strikes. It shows each breakeven with its distance from the current price,
where the maximum gain and loss hold (e.g. "at or below 35.00", or both tails of an
iron condor), and the P/L if the option expired at today's price. The chart shades
profit green and loss red and marks the current price, breakevens and strikes;
hovering reads P/L in dollars and as a percentage of the debit paid (or, for a
credit, of the maximum loss). A table lists the P/L at moves of ±5/10/20% from the
current price and at each breakeven and strike (the percentage column is hidden on
phones).

Because short-term options are usually closed before expiry, each candidate also carries
`payoff.curves`: the P/L if closed at the quote time ("now") and halfway to expiry, on a
price grid, valued with the binomial (CRR) model at each leg's current IV held fixed, and
`payoff.theta_per_day`, the value change over the next day at today's price. The chart
draws them as dashed and dotted lines, the tooltip and a "Close now" table column read
them, and a chip shows the daily time decay. They are estimates: implied volatility moves.
The "now" curve at today's price is roughly the cost of getting in and out at once (the
spread and fees). Answers made before this change have no curves and show the expiry view.

Pre-expiry scenarios are theoretical American-option binomial estimates using
fractional remaining time. The probability field is a **model-implied probability**
under a stated IV-based lognormal, risk-neutral distribution and stated horizon.
It is not a calibrated forecast, an option delta, an LLM confidence score, or a
historical win rate. Contract timing uses the exchange calendar and explicit provider cutoff metadata.
For normal sessions, a finite list of late-close ETF classes uses the
[NYSE Arca published schedule](https://www.nyse.com/publicdocs/nyse/markets/arca-options/Options_Late_Close_ARCO.csv)
(reviewed September 22, 2026) and closes at 16:15 ET; late-close ETF contracts on an
early-close date remain unavailable without verified timing. Every other contract,
including ETFs off that list (SOXS, TQQQ, SQQQ, ARKK …), stops trading at the session
close like equity options; for a fund or an underlying of unknown type the snapshot
carries `expiry_cutoff_assumed_1600_et`. (Until 2026-09-28 these ETFs were dropped as
"ambiguous", which left their chains empty.) A live
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
intraday comparisons; longer horizons always use a later expiry. Options pick the expiry that
fits the horizon: the nearest for intraday, the one nearest 3 days within 10 for interday, nearest
30 days within 14–60 for swing and nearest 90 days within 45–200 for position (else the nearest
beyond the window, else the closest quoted). Swing and position expiries are usually beyond the
nearest four the snapshot fetches, so the desk asks OpenD for that expiry's chain as well and
splits the quote budget between the two nearest expiries and it. Credit spreads
sell the out-of-the-money strike. Monitoring runs in
a separate worker and continues independently of the model. Follow-up calculations use the existing Codex owned-process runner, with cancellation and deadline cleanup; advice stores the actual tool calls and effective per-candidate requests.

When `TARGETED_GENERATION_BACKEND` is set (see the LLM configuration guide), automatic
follow-ups (options comparisons for trade ideas) are reviewed by the routine `GENERATION_BACKEND` without tools, so
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

## System status

The **Status** page (sidebar; `GET /api/v1/trade-desk/status`) lists every component with a
state — working, needs attention, failing, or off — and a one-line detail, refreshing every
30 seconds: the running code version (and a warning when newer code is on disk, i.e. a
restart is needed), moomoo OpenD quotes, scheduled reports (next run, last success or, after a
restart, when the last scheduled brief went out, last error, and how many stocks the last run analyzed out of how many it was given: a run that sent
its brief with some stocks missing needs attention, and with fewer than half analyzed it counts as
failing so the watchdog posts it), the alert monitor, plan monitoring, broker holdings sync, market pulse, trade
opportunities, breakout watch, idea tracker, YouTube picks (including a caption rate limit),
social scan and Discord delivery. Work a component hands to a background thread (the opportunity
scan, breakout levels, holdings tasks) reports its last failure too. OpenD reports no quote-rights
fields, so a connection logged in to the quote server reads as working (a denied right, a logged-out
quote server or an exhausted quota reads as failing). The OpenD check runs on its own
thread and reads as failing after 5 seconds (the moomoo SDK otherwise retries a lost connection
forever). The worker's watchdog (`trade_desk/status.py`, on its own thread so a monitor stuck on
OpenD is still reported) posts `system_status` messages to the digest category: a summary two minutes after each start ("✅ Server started · <version> · all
systems working", or the components that need attention), and a component still failing five
minutes later, once per component per day for as long as it lasts.

## Discord channels, categories and batching

Trade Desk messages fall into four categories (`trade_desk/discord_routes.py`):

| Category | Messages |
| --- | --- |
| `holdings` | Holding alerts, your price alerts, daily portfolio summary, plan triggers |
| `market` | Big moves and material news |
| `ideas` | Trade opportunities, options ideas, breakouts |
| `digest` | Social scan, weekly track record, system status |

`TRADE_DESK_DISCORD_WEBHOOKS=holdings=<url>,market=<url>,ideas=<url>,digest=<url>` (any
subset) sends a category to its own channel; the others use `DISCORD_WEBHOOK_URL` or the bot.
The Journal tab's settings switch each category off or on (`discord_categories` in the
preferences; a missing key means on). Moves and news on names you do not hold come in bursts
at the open, so they are sent together once the oldest has waited 15 minutes, as one "📊 Market
moves & news" message; alerts on names you hold, or when holdings are unknown, still go out
at once. A batch is one Discord message: what does not fit waits for the next one, so a retry
never repeats part of a message. A category with no webhook of its own and no main Discord setting
is skipped. The scheduled stock reports are not Trade Desk messages and keep the main channel.

Alerts about something you hold (holding alerts, your own alerts, and big moves or major news
on a held ticker) are sent as a card so they stand apart from market and idea messages: one
line for the notification ("💼 **NVDA** · Trend break · your position"), then a card with a
coloured bar (red: a loss, trend break, assignment risk or a move against you; green: a
profit or a move your way; amber: expiry and earnings; blue: your alerts and news), the
"💼 YOUR POSITION" header, the event, your position, **What to do**, the latest daily report
verdict and your alerts on it. A card is one Discord message; alerts stored before this change
are shown as cards too.

## Asking from Discord (/ask)

With your own Discord bot you can ask Trade Desk from your phone: `/ask ticker:NVDA
question:covered call for next week?` (`trade_desk/discord_ask.py`). The bot connects out to
Discord, so the server needs no public URL. Setup:

1. In the Discord Developer Portal create an application, add a bot, copy its token into
   `DISCORD_BOT_TOKEN`, and invite it to your server with the `bot` and `applications.commands`
   scopes (no privileged intents are needed).
2. Set `TRADE_DESK_DISCORD_ASK_USERS` to your Discord user id (Settings → Advanced → Developer
   Mode, then right-click your name → Copy User ID); several ids are comma-separated. Each
   question costs a model call, so nobody else can ask.
3. Restart the server; the command appears in your server within a minute.

The bot replies "Asked Trade Desk about NVDA…" at once and posts the verdict, a shortened
explanation and a link (`TRADE_DESK_PUBLIC_URL`) when the answer is ready (up to 15 minutes).
Questions from Discord run as live questions without your broker position, so the reply never
carries holdings. They appear under Your questions on the page like any other.

## Market pulse (market-hours watch)

With `MARKET_PULSE_ENABLED=true`, the Trade Desk worker (leader only, regular
session only) watches the US tickers in `STOCK_LIST`:

- Every minute, from the worker's single OpenD snapshot that also serves the
  breakout watch and holdings alerts. A day change crossing a level in
  `MARKET_PULSE_MOVE_LEVELS` (default `3,5,8` %, scaled for 2x/3x funds) emits
  `market_move` once per level per day, reporting only the highest level
  crossed and never again as the move fades. With broker holdings configured,
  every level applies to what you hold and watchlist names you do not hold alert
  from 5 %. (The former 15-minute "fast move" alert was removed: at the open it
  mostly produced contradictory pairs.) Move alerts cite the latest headline
  seen for the stock.
- Every 10 minutes, in the background, Google News for the next 11 tickers
  (the whole watchlist every ~30 minutes). New headlines from the last two
  hours are rated 0–3 for materiality by the routine generation backend in one
  call; only 3 ("major") emits `market_news`, at most 20 per day. Several outlets
  carrying one story make one alert per stock per sweep (the first headline, with
  "+N more headlines"), and that stock's next major headlines wait an hour. If the model
  is unavailable, a keyword rule (earnings, guidance, M&A, regulators, halts,
  offerings…) decides. The first look at each ticker after a restart only
  records existing headlines.

Both event types use the worker's deduplicated, retried Discord delivery
(labelled "Market move" / "Market news") and never create plans or orders.

## Swing trade opportunities

> Methodology note ([trading-methodology.md](trading-methodology.md), register in
> `trade_desk/register.py`): the trend and breakout rules below showed no edge over random
> entries in 2021–2026, and the model review on top showed no measurable value forward. So the
> scan's messages, their options follow-ups and the breakout alerts are recorded and tracked as
> below but go to Discord only once every rule they rest on has passed (stage 8; each message names
> its `rules`). Until then they show on the dashboard: **Ask** tab → **Trade ideas** (`GET /ideas`),
> the last 7 days with entry, stop, target and result so far, each labelled with its rules'
> verdicts; the ones the review rejected on request, and a chart button per stock.

With `TRADE_OPPORTUNITIES_ENABLED=true` (default off) the worker looks for
strong trends to go long or short over days to weeks. Nothing is ordered.

- **When:** after each scheduled stock-report run whose first report lands within
  an hour of a `SCHEDULE_TIMES` slot (one-off analyses and the market-review row
  never start a scan; the last published run and today's announced ideas are
  kept in `trade_desk_settings` so restarts neither rescan nor resend; the
  message's dedup key is the slot) — at least five fresh US
  reports, none in the last three minutes), including the after-close run.
- **Universe:** the watchlist plus `SCREENING_US_UNIVERSE` (S&P 500 by default,
  loaded once a day), about 520 names, from one batched yfinance download of six
  months of daily bars (~10–40 s).
- **Rules** (`trend.py`, 0–100 per direction, the stronger one kept):
  MA20/MA50 alignment 30, MA slopes 20, a close past the prior 20-day high/low 25
  (within 2 %: 12), 20-day momentum 15, volume vs. the 50-day average 10 (an
  unfinished session's volume is scaled by a typical intraday profile); more
  than 3 ATR past MA20 costs 10. Price ≥ $5 and ≥ $20M average daily dollar
  volume are required. Strength ≥ 70 is a strong trend. ATR levels: stop 1.5 ATR,
  targets 3 and 4.5 ATR.
- **Review:** up to `2 × TRADE_OPPORTUNITIES_MAX` (at least 6) setups at strength
  ≥ 70 — stretched ones (more than 3 ATR past MA20) are left out as chasing, half
  the slots are kept for watchlist/held names, and ties rank by volume and
  ATR-scaled momentum; watchlist names also qualify at strength ≥ 55 when their
  report score is ≥ 65 (long) or ≤ 35 (short) — go to the routine generation
  backend in one call with 20 recent bars, up to five headlines from the free
  news sources, the report summary, the SPY/QQQ regime and full names from OpenD.
  It keeps the rule direction or rejects, and gives a conviction
  (low/medium/high), entry, stop, targets, horizon, thesis, risks and
  invalidation. Stops or targets on the wrong side of the price fall back to the
  ATR levels; a malformed row drops only that idea. The message shows the first
  target's reward/risk ("R:R") and a "Risks:" line; ideas whose first target is
  smaller than the risk to the stop (R:R < 1) are dropped. The log lists the
  reviewed candidates (watchlist names marked `*`).
- **Earnings:** each reviewed candidate's next earnings date comes from Yahoo's
  calendar (`earnings.py`, cached per day; funds have none; for an unconfirmed
  window the earliest day). The model sees it. Earnings within 14 days (inside
  the hold) cap conviction at medium, so no options follow-up, and the idea shows
  "⚠️ Earnings … inside the hold: gap risk; size down or exit before"; within 30
  days a plain "Earnings …" line is shown. Options comparisons are told the date
  so expiries after it are weighed for IV crush. Breakout alerts carry the same
  note.
- **Delivery:** medium/high ideas (at most `TRADE_OPPORTUNITIES_MAX`) are sent as
  one `trade_opportunities` Discord message with labelled ways to act. Long: buy
  shares. Short: sell or trim if held; short shares (margin and borrow needed,
  loss unbounded); an inverse ETF for broad index/sector ETFs (e.g. SPY → SH,
  QQQ → PSQ). High conviction adds calls/call spread or puts/put spread; during
  the regular session a Trade Desk options comparison (swing horizon, routine
  model) runs for each new high-conviction name and one `options_ideas` message
  follows. An idea already sent today with the same direction and conviction
  is listed only under "Still on"; a run with nothing new sends nothing.
- **Live breakouts:** in the regular session, after the first 15 minutes, the
  watchlist and scan names at strength ≥ 60 within 2 % of their 20-day high/low
  are quoted every minute through OpenD. A price above the prior 20-day high and
  MA50 (or below the low and MA50) at ≥ 1.3× normal volume pace emits one
  `breakout` alert per stock, direction and day, with ATR stop and targets.
  Names outside the watchlist are capped at five alerts a day (watchlist names
  are checked first). A break must hold on two consecutive minute checks, needs
  2× pace before 10:30 (early snapshots can include pre-market volume), and a
  ticker/direction alerts at most once a week; funds on the same index or stock
  (SPY/VOO/SH…, QQQ/TQQQ/SQQQ…, SOXL/SOXS…, TSLL with TSLA) alert once per group
  and day. The price must
  clear the level by 0.15 ATR (no one-cent pokes); when today's trend review has
  an idea in the same direction its stop and targets are shown instead of ATR
  levels, and a move against the review says so. Notes reset each session; a
  failed level download is retried after five minutes. Volume pace follows the
  session's real length on early-close days.
- **Leveraged/inverse ETFs** (detected from the name, e.g. "Bear 3X", "UltraPro
  Short"): long ideas say "short-term, small size"; bearish ones only say sell or
  trim if held (never short the fund); no options follow-up, and breakout alerts
  show the stop without ATR targets.

## Forward track record of ideas and breakouts

Because the model review cannot be backtested, every scan's reviewed candidates
are followed forward (`idea_tracker.py`, table `trade_desk_tracked_ideas`): the
ideas that were sent (verdict `high` / `medium`, with their printed stop and first
target) and the candidates that were not (`rejected` — by the review, by R:R < 1
or by the message limit — with the rule's ATR levels), plus every breakout alert.
Entry is the alert price; from the next session daily bars settle each record like
the backtest (stop first when a day touches both, gaps fill at the open, otherwise
the close after 15 sessions; 5 bps per side), with SPY over matching closing-price windows for the
benchmark (shorts are compared with the market's move in their favour).

After 16:30 New York time each trading day the worker settles open records; on the
week's last trading day it posts "📒 Idea track record" (last 90 days per group).
`GET /track-record?days=` and the Journal tab show the same. Nothing is traded.

**How the summary grades.** A raw average of closed records misleads three ways, so:

- *Each idea counts once.* A scan relists a live idea every slot; a repeat of the same ticker,
  direction and family (approved / rejected / breakout) within 15 sessions of its first record is the
  same idea, whatever conviction it was relisted at.
- *Only finished ideas count.* An idea enters the averages with all 15 sessions behind it. Stops close
  within days and winners take weeks, so counting closed records alone over-weights the losers. The
  rest are shown as "running", with their average so far.
- *Same-day stocks are the yardstick, not SPY.* In a rotation the index and the average stock can move
  in opposite directions. After settling, the job measures what the average universe stock
  (`SCREENING_US_UNIVERSE`) did from each tracked signal day's close: under the ideas' rule (1.5 ATR stop,
  3 ATR target, 15 sessions, same settlement and costs) in both directions, and close to close over
  5/10/20 sessions. This takes a year of daily bars, downloaded only when a signal day reaches a new
  horizon, and is stored in the `idea_peer_baselines` setting. A day not measured yet (download
  failed, fewer than 200 stocks) is left out, not compared with something else. `avg_vs_spy_pct` stays in the API.

"Review value" compares approved with rejected finished ideas of the same day and direction, month-clustered,
once there are 10 such comparisons.

Each record also carries your NX tunnel state at the signal (`nx`), filled by the
same after-close job (older open records get it on the next run): the tunnels come
from the daily bars before the signal day, read at the alert price. The slow (89)
tunnel sets the alignment: a long above it or a short below it *agrees*, the
opposite is *against*, inside it is *neutral*. The weekly summary and the Journal
card split finished ideas and breakouts by alignment; with under 20 finished records
they say it is too early to judge.

A split or dividend adjusts the whole downloaded price history, so a price stored at the signal no
longer matches later bars: the signal day's close is kept as `ref_close` at the first settle and later
settles scale the stored levels by how that close moved (social and YouTube records read their entry
close from the bars every time). A ticker whose prices stop while SPY's go on for five sessions (a
halt, a delisting) closes at its last price (`reason: halted`) instead of staying open.

The stock reports' own calls are tracked the same way (`kind: verdict`): one per US
stock and day (the first report of the day, at its price), grouped as bullish
(buy/add/hold), watch, or bearish (reduce/sell/avoid). They settle on the close 5 and
10 sessions later and are split by the NX slow tunnel at the report
(above / inside / below). The after-close job reads the last 45 days of report
history, so reports made before this existed are included. The weekly summary adds
"Report calls, 10 sessions later vs same-day stocks" and the Journal card a table of the same.
Geared funds are left out of these comparisons: a 3x fund's move is not comparable to a stock's.

Each call also keeps `model_call`, the model's own buy/hold/sell before any guard, read from its stored
answer (older records are filled in once). The summary shows those calls against the rest of the
watchlist that day, which tests the call apart from how the watchlist did overall, and the scoreboard splits
the model's Buys into those shown as Buy and those a guard softened, so the guard's effect can be measured.

## Social media scan and YouTube picks

Two optional, free references (`src/services/social_scan.py`, `src/services/youtube_picks.py`).
Neither changes a report's score or action, a Trade Desk candidate, or an alert: the
prompts say so, and both are tracked forward to test them before anyone leans on them.

**Social scan** (`SOCIAL_SCAN_ENABLED=true`, US tickers). Sources, keyless and cached
15 minutes; a source that fails is skipped for 2 minutes and named in the report:

| Source | What it gives |
| --- | --- |
| ApeWisdom | Mentions across the stock subreddits over 24 hours, with the count and rank a day earlier |
| Stocktwits | The trending list with its own "why it is trending" summary; per ticker, the bullish/bearish tags on the latest 30 posts |
| Tradestie | WallStreetBets' 50 most-commented tickers with a sentiment tag |

X has no free API and Reddit's own JSON refuses this client, so neither is read directly.

- Stock reports: a "Social attention" line in the data section and the same context in the prompt.
- Trade Desk answers: a `social_scan` evidence item, shown under the answer.
- Trade opportunities: "🔥 Much discussed: Reddit #3 · WSB #5" on ideas in a source's top 20.
- After 16:20 New York time on trading days: "📣 Social scan" to Discord (the 10 most-discussed
  stocks, index funds excluded, plus the day's YouTube picks). The names are chosen after the
  close, so each is tracked from the next session's close (`kind: social`), 5/10/20 sessions
  against same-day stocks — only on days all three sources answered. Whether attention leads or lags is
  what the weekly track record tests.

**YouTube picks** (`YOUTUBE_CHANNELS=Name=UC…,…`, channel ids, not handles, because a handle
search can land on a clips or fan channel). Every 3 hours the worker reads each channel's RSS
feed, or, while YouTube's feeds are down (they have returned 404 for days at a time), the channel's
video and live tabs with yt-dlp; each new video's publish time, live status and whether it has
captions then come from yt-dlp too (a finished live stream counts from its end; the feed's own
time is not used, since for a stream or premiere it is when it was scheduled). A stream that has
not aired yet is checked again each pass. A video yt-dlp shows with English or Chinese captions (a
stream's chat replay does not count) is not transcribed from audio just because the player listed
no caption track: it waits on its own for a day, then its audio is read. At most 6 videos are
transcribed per pass. A new video's full spoken text goes to `YOUTUBE_PICKS_BACKEND` (default: the routine
`GENERATION_BACKEND`). The model returns only explicit calls as JSON: ticker, bullish or
bearish, conviction, horizon and a short reason; a reply that is not the complete JSON (e.g. cut
off) counts as that model failing, so the channel's next model is tried. Tickers are validated and at most 10 picks
per video are kept. A video is marked read once its picks are stored (setting
`youtube_processed`), so a restart never pays for it twice; a failed model call is retried on
the next pass. The first pass reads the last 30 days to seed the record.

The spoken text is the video's captions (YouTube's own, uploaded before automatic, English or
Chinese; complete transcripts, about 900–1,100 characters a minute in English and 250 in
Chinese). Some channels publish no captions. With `YOUTUBE_TRANSCRIBE_AUDIO=true` (needs
`pip install yt-dlp faster-whisper "av<16"`), such a video's audio is downloaded by yt-dlp into
a temporary folder and transcribed on the CPU by faster-whisper (`YOUTUBE_WHISPER_MODEL`, default
`large-v3-turbo`, int8, `YOUTUBE_WHISPER_THREADS` threads, default 8: a 25-minute video takes
about 6 minutes). The folder is deleted as soon as the transcription ends or fails; a folder
left by a killed process is removed on the next pass, and a server stop ends a transcription
at its next segment. The model (about 1.6 GB) downloads on first use into
`YOUTUBE_WHISPER_DIR` (default: the Hugging Face cache) and is released after each pass.
Titles and descriptions say too little to read a call from: a video without a transcript is
retried on the next two passes and then skipped. Only a clean answer from YouTube counts as "no
captions": a bot check, a refused request or an empty caption file is treated as a block, and an
upcoming or live stream waits until it has aired. When the model fails, the transcript is kept for
the next pass (the video is dropped after three failures); two failures in a row end the pass, and
no audio is transcribed while the model is failing. When YouTube rate-limits caption downloads
(HTTP 429), captioned videos are not transcribed from audio: caption requests pause for 6 hours
and the videos wait for a later pass; only captions refused for a whole day fall back to the
audio. Audio is transcribed only for videos of the last 7 days, so the first pass after a restart
does not spend hours of CPU on the 30-day backfill; older videos without captions are skipped. Videos over two hours (live streams) are not
transcribed. yt-dlp downloads go against YouTube's terms for automated access and can stop
working when YouTube changes; captioned channels are unaffected. yt-dlp needs a JavaScript
runtime for YouTube's player challenges: `pip install deno yt-dlp-ejs` puts `deno` in the venv,
where the scan finds it (or on `PATH`). Caption downloads are spaced 10 seconds apart and capped
at 25 per pass, and every YouTube request is spaced the same way (60 per pass), since bursts are
what trip YouTube's rate limit; the rest wait for the next pass. The evening digest reads the day's
picks from the database, so a restart does not lose them.

- Each pick is tracked (`kind: influencer`, id `influencer:<video>:<ticker>`) from the first
  close after the video was published: during the session, that day's close; after 16:00 or
  on a non-trading day, the next session's close. It settles 5/10/20 sessions later against
  same-day stocks; bearish calls count in their own direction.
- The weekly "📒 Idea track record" adds a line per channel: picks, bullish/bearish, average
  result vs same-day stocks at 5/10/20 sessions, closed count.
- Reports ("YouTube picks, last 30 days") and Trade Desk answers show a ticker's calls from
  the last 30 days; trade ideas show calls from the last 14 days.

Cost: the social sources, RSS, captions and local transcription are free (transcription uses
CPU time). Only the pick extraction uses a model, about one call per video; a transcript is
typically 3–8k tokens, so a low-cost LiteLLM model costs a fraction of a cent per video.

### What's working

The track record (Journal tab, `GET /track-record`, and the weekly Discord summary) opens with a
scoreboard: every tracked source side by side — trade ideas by conviction, candidates the review
rejected, breakout alerts, ideas where NX agrees or not, bullish and bearish report calls, the
social scan's names and each YouTube channel, plus the model's own Buy calls (shown / softened) and Sell
calls — with finished and running counts and the average result against same-day stocks in the
direction of the call. A source gets a verdict only with at least 30 finished records over at least
2 months: "ahead" or "behind" same-day stocks when the month-by-month results are consistent (a
month-clustered t past the two-sided 5% critical value for that many months: 12.7 with 2 months, 4.3
with 3, 3.2 with 4, near 2 only with a year or more), otherwise "no clear difference". Months rather
than weeks because a trade runs about three weeks, so weekly groups overlap and would overstate the
confidence. With a dozen sources, one of them can still clear that bar by chance. A source that stays
behind is a candidate to switch off.

## Broker holdings and your alerts

With `TRADE_DESK_BROKER_ACCOUNT` set (a real moomoo account id or its last
digits; `TRADE_DESK_BROKER_SECURITY_FIRM`, default `FUTUINC`), the worker reads
that account's positions and USD account value through the same OpenD (and RSA
encryption) as quotes: every 10 minutes in the regular session, once after 16:05
New York time, and on **Sync now**. It opens a short-lived trade context and calls
only `get_acc_list`, `position_list_query` and `accinfo_query`; trading is never
unlocked and nothing is ordered. The snapshot is stored locally
(`trade_desk_settings`, id `broker_holdings`).

- **View** (Trade Desk → Holdings, `GET /holdings`): stocks with quantity,
  average cost, live price, value, weight of the account and P&L on average
  cost; options grouped per underlying and expiry (a vertical spread is one
  position) with legs, net cost, value at the bid/ask mid, P&L on cost, share of
  the maximum profit when it is bounded, and trading days to expiry (US holiday
  calendar).
- **Built-in alerts** (regular session, deduplicated): options: 2 and 1 trading
  days before expiry (from 09:45), expiration day at 09:45 and 15:00, a short leg
  in the money within 2 trading days (assignment risk, daily), 75 % of a bounded
  maximum profit (otherwise +50 % and +100 % on cost), −50 % on cost, earnings on
  or before expiry. The profit and loss levels wait while a leg's bid/ask spread is
  over half its mid (and over $0.10), as often in the first minutes after the open,
  so a wide quote does not read as +200 %. Stocks: price below the prior 20-day low or a first drop below
  the 50-day average (daily each), earnings within 7 days. Leveraged/inverse funds
  get no earnings lookups.
- **Your alerts** (`POST /holdings/rules`, `PATCH`/`DELETE /holdings/rules/{id}`):
  underlying price at or below / above X, trading days to expiry ≤ N (options),
  position P&L at or below / above X % on cost; once (then "triggered", re-arm it)
  or once a day. They attach to a position (`position_key`, e.g.
  `USO 2026-10-16` or `NVDA`) or, for price alerts, to any ticker. A rule on a
  closed position waits and never fires. Plain words (`POST /holdings/rules/parse`,
  e.g. "stop loss if the underlying drops to 145", "warn me 3 days before
  expiry", "alert if I lose 40%") are read by a pattern first and the routine model
  second; the draft is shown for confirmation and nothing is saved until you do.
- **What to do** (`trade_desk/position_plan.py`): every holding alert adds the usual next step
  and the position's plan levels (below), also in the dashboard's event text (lines starting
  "→"). Without a plan yet: a stop 2 ATR(14) from the price and a target 3 ATR away on the
  side you hold (no target for leveraged/inverse funds), with the 20-day low/high as structure; a break below the
  20-day low reads "a close below it is the usual exit signal", a drop below the 50-day
  average "tighten the stop or trim". Options: cut a long option at −50 %, take half off
  at +50 % and profit at +100 %, close a spread at 75 % of its maximum, close or roll a short
  leg in the money, sell an in-the-money long option rather than exercise it, and the
  underlying's levels. A big move on a held ticker says "exit if it closes past your stop"
  (against you) or "take some profit near … or trail the stop" (your way). Then the latest
  daily report verdict on the ticker with its stop/target when it is at most 3 days old, and
  your active alerts on the position ("none set" says where to set one). These are the same
  rules of thumb as the ideas' levels, not tested signals (`docs/strategy-backtest.md`).
- **Plans: one action, stop and target per position** (`trade_desk/position_plans.py`, `GET
  /holdings/plans`, `POST /holdings/plans/review`; `trade_desk_settings` id `position_plans`):
  shown on Home ("Your positions: plan", above the report) and on the Holdings tab, and used
  by the Discord cards, so all three agree. Each level comes from your own active alerts on
  the position first ("your alert": price at or below / above, P&L levels; they never move),
  then the **daily review** (after the close, once a trading day from 17:00 New York, the
  generation backend reviews every position with the same data as Ask about your positions
  and sets an action, a stop, a target and P&L levels for options; **Review now** runs it at
  any time, about a minute), then for a position not reviewed yet a 2 ATR stop and 3 ATR
  target from its last close ("2 ATR rule"). Stops trail: each morning and after the close a
  long position's stop becomes max(stop, highest close since it was set − 2 ATR) — a short
  one's min(stop, lowest close + 2 ATR) — so it only moves toward the price. A review can
  tighten a stop but not loosen it ("kept: … would loosen it"), except after the stop was hit
  with the position still held, when the review sets a new one. Targets change only with a
  review or your alerts. The panel lists each open position with its price and day change,
  P&L on cost, action, stop and target, how far the price is from each (%), where each level
  came from, and "Near stop/target" (within half an ATR) or "Stop hit/Target reached";
  positions needing attention come first, and a row opens the reasons, each level's basis and
  the risk. Crossing a plan's stop or target (or an option's P&L cut/take-profit level) sends
  one position card per level (🛑 "Stop hit", 🎯 "Target reached"); levels from your own
  alerts fire as your alerts. Options' price levels are on the underlying. The plans are
  risk-management levels, not tested signals; nothing is traded.
- **Ask about your positions** (Holdings tab, `POST /holdings/questions`, `GET
  /holdings/questions`, `DELETE /holdings/questions/{id}`; `trade_desk/position_questions.py`):
  a question about one position or all of them ("Where should my stop-loss be?", "At what
  price should I take profit?", "Hold, trim or close?", "Review all my positions") is
  answered by the generation backend (its fallback included) in about a minute. The model
  sees each position as prices and percentages — average cost and price, P&L on cost,
  weight of the account, option legs by side and strike — never share or contract counts or
  account amounts, plus the 20-day low/high, 50-day average, ATR, the ATR rule's levels, your
  NX tunnel, the latest daily report, the next earnings date and your alerts. It returns a
  short answer and, per position, an action (hold, add, trim, take profit, close, roll,
  hedge), a stop and a target on the stock or underlying with the reason for each level,
  P&L cut/take-profit levels for options, a reason and the main risk. A level on the wrong
  side of the price (a long position's stop above it) is replaced by the ATR rule's level and
  labelled so. **Set alert** under a level saves it as one of your alerts on that position
  (price at or below / above, or P&L); nothing is traded. One question runs at a time; the
  last 30 are kept (`trade_desk_settings` id `position_questions`) and never go to Discord.
  A holding's **Ask** opens this box on that position; **Compare option strategies** from
  there opens the Ask tab with the position attached, as before.
- **Shadow exits** (`trade_desk/shadow_exits.py`, `shadow_watch.py`, `live_feed.py`, `GET /holdings/shadow`, the
  Journal tab's card, refreshed every 15 seconds; `trade_desk_settings` ids `shadow_exits`, `shadow_levels`,
  `shadow_feed`): what the system would have sold, when and at what quote. **Nothing is sent to moomoo:** there is
  no order code in the app, a test fails if order calls appear, and trading is never unlocked.

  *Live feed.* From 5 minutes before the open to 1 minute after the close on trading days, the server holding the
  monitor lease keeps its own OpenD connections: quote push on every held code and underlying, order-book push on
  held stocks and contracts, and this account's fill push. Each pushed quote wakes a check, at most every 0.1 s,
  that uses only what is in memory, on its own thread, so a slow alert, a locked database or Discord cannot delay
  it. A second thread reads the position list every 15 seconds and, after a fill is heard, at once and then every
  4 seconds for 12 seconds (asking the broker; the limit is 10 such calls per 30 s). It also reads plans and alerts
  every 10 seconds, takes a snapshot of every code every 30 seconds, and saves results at most every 2 seconds. A
  position that vanishes with no closing fill heard since the last read must be gone on two reads in a row, the
  second asking the broker. Just after the open, an option's book counts only once it has moved, for up to 2
  minutes, because the first one seen may be yesterday's. SPY's last trade older than 30
  seconds means the feed is frozen, and nothing is checked; if only pushes stop, checks continue on the snapshots,
  and after 2 minutes the connections start over. Levels stay as they were set (the daily review, your alerts, or
  the rule below): this watches in real time, it does not decide in real time.

  *What it records.* Any change to a position's stop, target, P&L levels or action (so levels replaced before they
  were hit can still be graded), and the first crossing of each exit while you hold the position: the plan's or your
  own stop and target, P&L levels, the built-in option levels (−50 %, +50 %, +100 % on cost, or 75 % of a bounded
  maximum), a held stock's break of its 20-day low, and a review saying trim, take profit or close. Each record says
  how it was seen: pushed quote or snapshot, the quote's age on arrival and the wait for the check. After more than
  60 seconds of not watching (a restart, a lost feed), a level found crossed has no trigger moment until that
  holding has been priced once: it is counted as carried and not graded, as are levels crossed in shadow mode's first
  5 minutes and 20-day lows that loaded over 60 seconds late. A holding whose first trigger was carried is not graded
  on later ones either. Closing and reopening a position lets its levels trigger again; adding a contract on the same
  expiry keeps the holding, and replacing all its contracts starts a new one.

  *The would-be order.* It closes the whole position at a limit on the side you would be filled at (the bid to sell,
  the ask to buy back). It waits while a quote is stale or too wide, a long option has no bid, or the position list
  is over 60 seconds old; the wait is recorded. After 5 minutes it gives up, because a later price would not grade
  this trigger, and closing the position first counts as no exit. Spreads are valued leg by leg and flagged, because
  the first live version would skip them.

  *A position opened during the session* gets its plan as soon as the list shows it and its daily bars load: the ATR
  rule measured from the price then, not from the last close, which after a big move could already be crossed. The
  plan is shared, so its Discord cards start then too. A position you reopen on the same key ignores the old plan and
  the last review, which were about the earlier holding, and gets a new plan the same way. Until then only the
  built-in levels and your alerts apply.

  *Results.* One mark a day, updating through the last 10 minutes of the session so the last one stands, follows each
  record for 10 sessions or to expiry, even after you close the position. On expiration day the mark is what the
  contract settles at: its intrinsic value at the underlying's last price of the session. Results are P&L on cost: if
  sold, if held 5 and 10 sessions (or to expiry), and what you did (your own closing fills since the trigger when each
  leg has one, else the mid when the position was seen gone, else held). Each holding counts once, on its first trigger. The verdict
  stays "too early" under 30 finished positions or 2 months, then uses a month-clustered t like the track record. The
  card shows the feed's state and the day's measured delays, kept for 30 days: stock and option quotes (arrival minus
  the trade's time, to the second, on real trades only), order books (arrival minus moomoo's receipt time) and your
  fill to the position list; the host's clock and moomoo's are not compared, so read these as scale.

  *Limits.* Fills at the bid or ask are assumed, commissions are ignored, partial trims are not seen, a stock's stop
  is judged on its last trade, so one odd print can trigger it, and it only runs in regular hours (SPY and QQQ options
  trade 15 minutes longer than the last mark). Sizes and amounts are never stored, and nothing goes to Discord except
  the Status page's usual failure notice: the feed appears there as Shadow exits live feed. Your Discord holding alerts
  keep their own timers (quotes each minute, positions each 10 minutes). Once the feed is listening, the moomoo SDK's
  file log in the home folder drops from debug to info: at debug it writes two lines per push, over 1 GB a session.
- **Everywhere else:** held tickers join the market pulse and breakout watch;
  trade ideas and breakout alerts on something you hold add a "You hold: …" line,
  worded by the side you hold (shares by sign, options by payoff shape: a bull
  call spread or short puts are long, long puts are short): "already held — hold,
  or add small", "sell or trim your position", "already positioned for a drop",
  or "your position leans the other way — review it".
- **Expired contracts** the broker still lists show as expired, never fire
  rules, and are flagged in the summary. Expiry alerts say which legs are in or
  out of the money. Rule notes are masked for amounts and sizes before Discord.
  Earnings lookups run in the background, never on the monitor loop; a failed
  lookup is retried instead of cached. A sync-problem banner clears as soon as a
  later sync succeeds.
- **Daily portfolio summary** (`portfolio.py`): each trading day at 16:15 New
  York time, after the post-close sync, one `portfolio_summary` Discord message
  (and the Holdings tab card; **Build now** / `POST /holdings/summary` rebuilds it
  without sending): the account's day change (broker "today" P&L over the prior
  value); stock, option and cash shares; market exposure as the account's move
  for a 1 % SPY move (60-day betas of held stocks/ETFs; options excluded) and
  which holdings hedge it; the five largest positions and the day's movers; the
  leveraged/inverse fund share; positions over 25 % of the account; pairs whose
  60-day daily returns offset (correlation ≤ −0.6) or move together (≥ 0.85, both
  ≥ 5 % of the account); option expiries within 5 trading days and earnings within
  7 days; active and triggered alerts. One per day (deduplicated across restarts).
- **Privacy:** Discord messages (`holding_alert`, `portfolio_summary`) contain percentages only —
  weight of the account and P&L on cost — never share counts, cost or dollar
  amounts. The dashboard shows full detail.

Relative results versus SPY use the stock and SPY closes on the exact signal and horizon/exit
dates, with no substitution of a nearby session. Alert-price trade simulations remain separate.
Records without aligned prices are excluded from relative statistics. The next tracker run
recalculates available comparisons for closed records in the last 90 days, without changing
their original advice, simulated exits, or absolute returns.

## Portfolio risk

The Holdings tab's **Portfolio risk** card (`GET /holdings/risk`, `trade_desk/risk.py`) adds up
what you hold, as read-only estimates:

- Delta per underlying in share-equivalents and dollars, and the SPY-beta-weighted total (the
  SPY dollars that would move like the account). Each option's implied volatility is backed out
  of its mark (Black-Scholes; early exercise ignored). Prices are the latest available (after the
  close, the closing price). A missing underlying price, a missing or crossed/wide option book, or
  an unsolvable IV leaves that underlying's exposure unavailable: the totals and scenarios cover
  the priced underlyings and name the ones left out (the card and the daily summary say so); an
  unpriced holding is never counted as zero.
- Time decay: the value change over one day at today's price, per underlying and in total.
- Beta to SPY and QQQ from a year of daily returns (at least 60 shared days, else 1.0, marked
  with *); leveraged and inverse funds get their real betas this way.
- Scenarios SPY ±3% and QQQ ±5%: each holding moves by its beta times the index move and the
  options are re-priced at the moved price with the same volatility and time (an instant move).

The daily portfolio summary on Discord adds one line in percentages only, e.g. "Risk
(estimate): if SPY -3% ≈ -0.7% · QQQ -5% ≈ -1.0% · time decay ≈ -0.02%/day".

Holdings price/P&L alerts require a quote fetched within 150 seconds (the shared snapshot
refreshes every 60; at most 2 seconds of future clock skew), during regular hours, with a usable
book for options. Freshness is when the quote was fetched, not OpenD's `update_time`: that is
the last trade, which for a thin option or ETF can be many minutes old while its bid/ask is
current. Naive OpenD US timestamps are interpreted in New York time. Cached broker prices remain
available for display but cannot consume alerts. Date-only expiry/earnings reminders continue.
Extended-session snapshot fields lack an independently verified timestamp and do not drive
regular-price triggers. Missing cost basis leaves option P&L unavailable. Wide option books
pause custom P&L rules as well as default profit/loss alerts. Discord names the underlyings a
risk line leaves out, and says the estimate is unavailable when nothing could be priced; an
unpriced holding is never reported as zero.

### Risk at your stops

The **Risk at your stops** card (`GET /holdings/stop-risk`, `risk.stop_risk`) sits under Portfolio
risk on the Holdings tab, and on the Chart tab for the charted stock's positions. It shows, as a
percentage of the account only:

- each position's loss if its plan stop is hit (the same stops, prices and weights as the
  Holdings page; options re-priced at the stop with their current implied volatility);
- the total if every stop is hit;
- a gap scenario: each position opens past its stop at twice the stop's distance.

A position already past its stop would sell at today's price, so it adds nothing more. One
without a stop, or an option whose price cannot be used, is named and left out of the totals.

## Chart

The **Chart** tab (`/trade-desk?view=chart&ticker=SPY`) shows a stock with what the system
sees, so you can read it and decide yourself. DSA never places, changes or cancels an order.

- **Pick a stock**: type a ticker, or pick one of your holdings, your watchlist or a recent
  trade idea.
- **Bars**: up to 1,000 moomoo bars at 1, 5 or 15 minutes, or daily, in New York time, with
  volume and the NX tunnel lines (A/B over 26 bars, A1/B1 over 89).
- **Boxes**: each setup's range box (blue) and base box (purple), from the backtested rules.
  A box still in play is drawn solid with dashed edges to the right; an expired one is dashed.
  On daily bars the boxes are hidden, since they come from 15-minute bars.
- **Lines**: the value-area low and high of the last two weeks (VAL/VAH −1w, −2w), the prior
  day's high, low and close (PDH, PDL, PDC), and for a stock you hold, its plan stop and target
  and your price alerts. The side panel lists them with their distance from the last price.
- **Flags**: *armed* (a limit the next 15-minute bar fills), *watching* (a break waiting for a
  retest), *open* and the window's *finished* trades with their R. Selecting a flag draws its
  entry, stop and target. Every flag shows its backtest grade: none has beaten the same trades
  at random times, so they are context, not suggestions.
- **Box rules**: the four backtested variants (impulse 1 or 0.6 T, normal or tidy boxes).

**Your marks and boxes**: "Mark long / short here" with your stop and target records a paper
trade that enters at the open of the next 15-minute bar. "Draw a box" (two clicks on the chart)
and "Keep box" record a box that is traded by the system's fade rule: the first return to it
from outside within five sessions, entered at its near edge, the stop at its far edge, the
target at 2R. A mark or box can be removed until it trades; after that it stays on the record.
Nothing is sent to moomoo.

**Forward grades** (under the chart): one row each for the system's break-and-retest flags, its
fades, your marks and your boxes. A system flag counts only if the chart showed it, armed or
watching, before the bar it entered in started; one first seen during that bar is "late" and left
out. Every trade follows the backtest's exits (stop, target or the fifth session's close, with
cost) and is compared with the same trade at random times in its session (limit entries for
flags and boxes, entries at a bar's open for marks). A row gets a verdict after 30 graded trades:
it passes only if its edge over random timing reaches the month-clustered t the methodology needs
and its trades made money. Records move whenever a chart of their stock is read, and once a day
after 16:20 New York for stocks with trades still running.

**On moomoo's own chart**: `scripts/moomoo_box_indicator.txt` is a moomoo Python indicator that
draws the same range and base boxes (and marks impulse bars) on moomoo's 15-minute chart. Paste
it into moomoo's indicator editor as a Python indicator and use it with extended hours hidden, so
its sessions are the regular ones the rules use. Its inputs are the impulse (1.0 or 0.6 T) and
tidy boxes. It only draws; moomoo's drawing calls are not in the API documentation, so check it
on the chart once against the Chart tab.

**Live updates**: while the tab is open, moomoo pushes the forming bar and each trade to the page
(the header shows "Live" and the last trade's time). When a 15-minute bar closes, the page
reloads the boxes and flags, which only ever use completed bars. Up to five charts stay live at
once; a sixth pauses the oldest, which then refreshes every minute. A closed page's
subscriptions are released after 10 minutes. The Status page's "Chart live bars" line shows the
open charts, pushes, quote delay and the time spent per push.

## Trading assistant

The Chart tab's **Trading assistant** card (`trade_desk/assistant.py`, `GET /assistant`,
`POST /assistant/start` and `/assistant/stop`, server-sent events at `GET /assistant/stream`; plan in
[trading-assistant-plan.md](trading-assistant-plan.md)). Nothing runs until you press **Start** on one
to three stocks with a horizon (0DTE options, intraday or swing); **Stop**, or 15 minutes after the
close, ends it (a session started after the close runs until you stop it). Read only: nothing is sent
to moomoo, and its alerts go to this page only, never to Discord.

- **Levels** for each stock, with the price's distance and the register verdict of each
  ([trading-methodology.md](trading-methodology.md) §11; none has passed): the chart's prior-day high,
  low and close and the two prior weeks' value areas, your plan stops and targets and price alerts, the
  pre-market high and low (from moomoo's snapshot at the start), the 5- and 15-minute opening range
  and VWAP from today's 1-minute bars, kept up to date from pushes.
- **Contracts:** calls and puts of the 10 strikes nearest the price, for the nearest expiry (0DTE and
  intraday) or the first one at least 14 days out (swing), quoted every 5 seconds: bid and ask, the
  spread as % of mid, implied volatility, delta, the move the stock needs to break even at expiry, the
  value lost to time each minute, and what the contract would be worth if the stock reached the nearest
  level above and below or your stop or target, now and in 30 minutes. Those values are Black-Scholes
  estimates at the contract's quoted implied volatility, not quotes; it never picks a contract.
- **Your positions** on the stocks: P&L on cost, weight in the account, plan stop and target, days to
  expiry. They come from shadow mode's live feed (read every 15 seconds, at once after a fill) while it
  runs, otherwise from the last holdings sync, and the card says which.
- **Box flags** that are armed, watching or open, reloaded at every 15-minute close, with their grades.
- **Alerts**, each once, with a sound and a browser notification once allowed (**Allow notifications**):
  the price crossing a level (each level and direction at most every 15 minutes; the first trade after
  the start only sets the side), a box flag becoming armed, watching or filled, a position appearing or
  closing, its P&L on cost crossing a step (10 % for options, 2 % for stock), and 30 and 15 minutes before
  the close while you hold a contract expiring today.
- **How:** a check thread reads only memory every 0.25 s (the chart service's pushed trades and 1- and
  15-minute bars, on its own moomoo connection); an io thread loads each chart at the start and after
  each 15-minute close, today's 1-minute bars, the chain once, the contract quotes and the positions.
  The Status page shows it as "Trading assistant". Your server restart ends a session.

## 0DTE quote recorder

With `OPTION_RECORDER_ENABLED=true` the server holding the monitor lease records option quotes for
later 0DTE tests (`trade_desk/option_recorder.py`; [trading-methodology.md](trading-methodology.md)
§14): there is no option history to backtest on otherwise. Read only: no subscriptions, orders or
database writes, on a moomoo connection and a thread of its own.

- **What:** from 10 minutes before the open it reads each underlying's nearest expiry (0DTE where
  there is one) and that expiry's chain, once a day, 3.5 seconds apart. From the open to 15 minutes
  after the close (SPY and QQQ options trade until 16:15), every 15 seconds on round times: the
  underlying, then the calls and puts of the 24 strikes nearest its price, with bid, ask, their
  sizes, last, volume, open interest, implied volatility, delta, gamma, theta, vega and moomoo's
  update time.
- **Which:** `OPTION_RECORDER_TICKERS` (default `SPY,QQQ`), then the option underlyings you hold and
  the stocks of your trade journal, at most 12; the list is re-read every 10 minutes. A stock without
  listed options is skipped for the day; a chain that fails to load is retried after 5 minutes.
- **Where:** `OPTION_RECORDER_DIR` (default `option_quotes/` next to the database), one
  `<day>/<TICKER>.csv.gz` per underlying (`right` is `S` for the underlying's own row), appended so a
  restart keeps the day's rows. A test run wrote about 50 bytes per row: roughly 100 MB a day at 12
  underlyings, so keep it off a small home folder.
- **Load:** one `get_market_snapshot` for the underlyings and one per 400 contracts every 15 seconds,
  within OpenD's 60 requests per 30 seconds.
- **Status page:** "0DTE quote recorder" shows the underlyings, rows today and the last snapshot's
  age; a warning when a chain failed, an error when no snapshot came for over a minute in the session.

## Your trades (journal from moomoo fills)

The Journal tab's **Your trades** card reads a year of your filled trades from moomoo
(`POST /holdings/journal/refresh`; read-only: `history_deal_list_query` in 90-day windows plus
today's `deal_list_query`, trading is never unlocked) and builds round trips per contract, first in,
first out (`trade_desk/journal.py`). Stock fills before a split are restated in today's shares
(10× the shares at a tenth of the price for a 10:1 split). A refresh also reads current broker
holdings. Today's quantity less net fills detects unexplained inventory, but cannot distinguish
shares held before the history from a later assignment. All round trips for an affected code
are excluded and listed for reconciliation rather than assigning an invented FIFO cost basis.
Current-day corrected fills supersede older versions of the same deal; cancellations remove
those deals. A failed current-day fill query fails the refresh instead of silently truncating it.
An option left open after expiry that finished out of the money (the underlying's actual,
unadjusted close on the expiry day) expired worthless, a certain result that counts as a realized
round trip (a lost premium, or a kept one for a short option). One that finished in the money may
have been exercised or assigned, and one whose close is unknown has no known result: both are
listed for reconciliation instead. Otherwise only quantities closed by matched fills count. Spreads count as their legs; amounts are gross of commissions. The card
shows realized P&L, win rate and average hold, grouped by type (long/short stock, calls, puts), by
holding time, and by whether the trade agreed with one of the system's tracked signals (idea,
breakout, report call, social pick, YouTube call) on the ticker in the five days before it opened
— only signals stored before the trade count (report calls from the day after their report),
plus the best and worst trades. The result is kept (`GET /holdings/journal`); it is web-only and never
sent to Discord. Saved journals from the previous calculation version require **Read fills
from moomoo** to rebuild; their inferred expiry results are not displayed.

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

## Refreshing an answer's prices

A live answer's prices age while you read it. **Refresh prices** (on every finished live
answer, and on a stale one) re-prices the same contracts from current quotes without asking the
model again (`POST /advice/{id}/reprice`): legs, payoff, close-early curves, probability and
scenarios are recalculated, the reasoning is kept, and the answer notes when prices were
refreshed and that the explanation was written at the earlier prices. A candidate that cannot be
priced keeps its earlier numbers and is counted in the note. If the underlying is through a
selected candidate's invalidation level, or its exit time has passed (when the answer was
calculated, or at the refreshed price, even for a candidate whose own contracts could not be
priced), the answer is stale and says the reasoning no longer holds instead of showing the
refreshed-prices note. **Run again** still asks the model anew.

## Positions and operational behavior

The web page no longer offers plan monitoring (the "Monitor" button, trigger/target inputs, the
Positions tab and the paper/manual ledger results): it was never used, and price alerts on
Holdings cover the same need. The plan, fill and position API below is unchanged for existing
clients.


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
cannot mention `@everyone`, `@here` or users. A
"volatile" view defaults to long straddles/strangles only; iron condors always use
distinct short strikes; candidates with no possible expiration profit are omitted.

## API and persistence

All `/api/v1/trade-desk/*` routes inherit the existing administrator session guard.
The API supports health, catalog, advice submission/status/cancellation, plans,
paper fills, manual fills, positions, journal, outcomes, preferences and SSE events.
`POST /advice` accepts `parent_advice_id` for a follow-up version. `GET /advice`
takes `scope=active|archive|all` (default `active`; list rows carry only snapshot timing, the
full quote snapshots come from `GET /advice/{id}`), marks archived items with
`archived` (`expired`, `stale`, `no_result`, `old`) and returns `counts`. `DELETE
/advice/{id}` deletes one finished request (409 while queued/running or linked to a
plan); `DELETE /advice?scope=archive` deletes every archived one and returns `deleted`
and `kept` (id → reason). `POST
/plans/{id}/reconcile` records reconciliation notes after actual fills are entered. `POST
/plans/{id}/paper-settle` (`underlying_price`) settles expired paper option legs.
`GET /outcomes` returns `paper`, `paper_replay` and `manual_live` buckets.
`GET /holdings`, `POST /holdings/refresh` and the `/holdings/rules` routes serve
the read-only broker holdings and your alerts (409 when no account is set).
`GET /assistant`, `POST /assistant/start` (`{"tickers": [..], "horizon": "0dte|intraday|swing"}`, one to three
US stocks; another session is stopped first), `POST /assistant/stop` and the event stream `GET /assistant/stream`
(`state` once a second while a session runs, `alert` as each happens) serve the trading assistant.
`GET /ideas?days=7` (1–30) lists the trend scan's reviewed ideas and the breakout alerts, newest first,
with the register verdicts of the rules each kind rests on and whether it may reach Discord.
`GET /chart/{ticker}?interval=15m` (also `1m`, `5m`, `1d`; `impulse=1.0|0.6`, `tidy=true|false`)
returns up to 1,000 moomoo bars and what the live chart draws
([live-chart-plan.md](live-chart-plan.md)):
- `boxes`: the range and base boxes of every setup in the window, from `trade_desk/boxes.py`.
- `flags`: each setup's trades: `armed` (a limit the next bar fills), `watching` (a break and
  retest waiting for a close past its price), `open` and `closed` (with R after cost by the
  backtest's exits). Each carries its backtest grade; none has passed.
- `levels`: value-area low and high of the two prior weeks, the prior day's high, low and close,
  and for a stock you hold, each position's plan stop and target and your price alerts.
- `nx`: the NX tunnel lines on the chart's own bars.

Boxes, levels and flags always use completed 15-minute regular-session bars. The chart reads
bars on a moomoo connection of its own. It keeps the subscriptions of its 5 most recent
tickers and releases older ones at the next request, and ones idle for 10 minutes at the next
request or the minute's sweep. A moomoo failure returns 503 with its code. OpenAPI at `/docs`
describes request validation.
`GET /chart/{ticker}/stream?interval=15m` is a server-sent event stream for an open chart: `bar`
(the shown interval's latest bar), `quote` (the last trade and its exchange time), `closed` (a
15-minute bar ended; the page reloads the snapshot) and `evicted` (more than five charts are
open). It subscribes the quote and the bars with push on the chart's connection; the pushes are
kept in memory, never in the events table.
`POST /chart/{ticker}/marks` (`direction`, `stop`, `target`, `note`) and `POST /chart/{ticker}/boxes`
(`top`, `bottom`, `note`) record your marks and boxes (422 when a stop or target is on the wrong
side of the last price); `DELETE /chart-yours/{id}` removes one that has not traded (409 after);
`GET /chart-grades` returns the grade table and the latest records. They are kept in the additive
`trade_desk_chart_trades` table.

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
