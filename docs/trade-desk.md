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
like a dropdown (it opens by itself when there is no history or when a link brings a
ticker); its quick row is the ticker (suggestions: what you hold, then what you asked
recently), your question (Ctrl/Cmd+Enter sends) and **Ask**; under it, the ticker's latest active report
verdict (action, score, stop/target, date and the reason; from the AI-signal store), with data mode, direction
and horizon beside it and everything else under **More options**. Sending folds the
panel so the answer is in view. **Your questions** lists your requests grouped by
stock (newest group first, a filter once there are more than three); the answer to the
selected one sits beside it, with the follow-up box at its end. In **Broker
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
none passing the filters, with the provider's notes) instead of a generic message.

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
intraday comparisons; swing candidates always use a later expiry. Credit spreads
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
  call; only 3 ("major") emits `market_news`, at most 20 per day. If the model
  is unavailable, a keyword rule (earnings, guidance, M&A, regulators, halts,
  offerings…) decides. The first look at each ticker after a restart only
  records existing headlines.

Both event types use the worker's deduplicated, retried Discord delivery
(labelled "Market move" / "Market news") and never create plans or orders.

## Swing trade opportunities

> Backtest note (see [strategy-backtest.md](strategy-backtest.md)): the deterministic
> trend and breakout rules below showed no edge over random entries in 2021–2026,
> and their short side was clearly harmful. The model review on top is untested.

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
the close after 15 sessions; 5 bps per side), with SPY over the same days as the
benchmark (shorts are compared with the market's move in their favour).

After 16:30 New York time each trading day the worker settles open records; on the
week's last trading day it posts "📒 Idea track record" (last 90 days per group,
and approved vs rejected once each has 10 closed ideas). `GET /track-record?days=`
and the Journal tab show the same. Nothing is traded.

Each record also carries your NX tunnel state at the signal (`nx`), filled by the
same after-close job (older open records get it on the next run): the tunnels come
from the daily bars before the signal day, read at the alert price. The slow (89)
tunnel sets the alignment: a long above it or a short below it *agrees*, the
opposite is *against*, inside it is *neutral*. The weekly summary and the Journal
card split closed ideas and breakouts by alignment; with under 20 closed records
they say it is too early to judge.

The stock reports' own calls are tracked the same way (`kind: verdict`): one per US
stock and day (the first report of the day, at its price), grouped as bullish
(buy/add/hold), watch, or bearish (reduce/sell/avoid). They settle on the close 5 and
10 sessions later against SPY and are split by the NX slow tunnel at the report
(above / inside / below). The after-close job reads the last 45 days of report
history, so reports made before this existed are included. The weekly summary adds
"Report calls, 10 sessions later vs SPY" and the Journal card a table of the same.

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
  stocks, index funds excluded, plus the day's YouTube picks). Each name is tracked from that
  day's close (`kind: social`), 5/10/20 sessions against SPY. Heavily discussed stocks have
  tended to lag afterwards; the weekly track record shows whether that holds here.

**YouTube picks** (`YOUTUBE_CHANNELS=Name=UC…,…`, channel ids, not handles, because a handle
search can land on a clips or fan channel). Every 3 hours the worker reads each channel's RSS
feed. A new video's full spoken text goes to `YOUTUBE_PICKS_BACKEND` (default: the routine
`GENERATION_BACKEND`). The model returns only explicit calls as JSON: ticker, bullish or
bearish, conviction, horizon and a short reason. Tickers are validated and at most 10 picks
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
retried on the next two passes and then skipped. Videos over two hours (live streams) are not
transcribed. yt-dlp downloads go against YouTube's terms for automated access and can stop
working when YouTube changes; captioned channels are unaffected.

- Each pick is tracked (`kind: influencer`, id `influencer:<video>:<ticker>`) from the first
  close after the video was published: during the session, that day's close; after 16:00 or
  on a non-trading day, the next session's close. It settles 5/10/20 sessions later against
  SPY; bearish calls count in their own direction.
- The weekly "📒 Idea track record" adds a line per channel: picks, bullish/bearish, average
  result vs SPY at 5/10/20 sessions, closed count.
- Reports ("YouTube picks, last 30 days") and Trade Desk answers show a ticker's calls from
  the last 30 days; trade ideas show calls from the last 14 days.

Cost: the social sources, RSS, captions and local transcription are free (transcription uses
CPU time). Only the pick extraction uses a model, about one call per video; a transcript is
typically 3–8k tokens, so a low-cost LiteLLM model costs a fraction of a cent per video.

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
  or before expiry. Stocks: price below the prior 20-day low or a first drop below
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
