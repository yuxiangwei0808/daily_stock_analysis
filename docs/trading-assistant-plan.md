# Plan: real-time trading assistant

Status: draft of 2026-11-06, not started. It builds on the Chart tab
([live-chart-plan.md](live-chart-plan.md)) and follows
[trading-methodology.md](trading-methodology.md), which is the main source for how we trade and how
we find stocks.

## What it is for

The rest of the Trade Desk answers questions: you ask about a stock, or a scan and the alerts reach
you. The assistant joins a trade while it happens. You press **Start** on one to three stocks and
pick a horizon: 0DTE or intraday options by default, a swing if you say so. Until you press
**Stop** or the session closes, it watches the stock with you, explains what changes, tracks your
own plan and your position, and may make calls of its own. Nothing runs before Start.

It is built for minutes, not days. Your fills show that most option trades close the same day,
often within half an hour, and many open on their expiration day.

DSA never sends, changes or cancels an order, and the trading password is never used. You place
every order yourself in moomoo.

## Decisions (2026-11-06)

1. **The assistant may make calls.** "I'd go long here, stop X, target Y" is allowed. Each call is
   labelled *untested model call*, recorded, and graded against the same trade at random times
   (methodology §9: an idea that can't be replayed goes straight to forward tracking with a fixed
   rule).
2. **Alerts go to the dashboard only:** a browser notification and a sound from the open page. No
   Discord and no moomoo price alerts for the assistant.
3. **Trade ideas follow the methodology strictly.** Discord gets an idea only once its rule has
   passed (stage 8). Until then, ideas stay on the dashboard, labelled with their rule and its
   verdict, and are still tracked forward. Today no rule has passed, so Discord ideas stop.
4. Defaults chosen by Claude, open to change:
   - The first version targets 0DTE and intraday. Swing sessions come later and use the same
     panels on daily bars.
   - The model is Sonnet 5.5 through the existing LiteLLM backend. It is set in configuration,
     never in code.
   - The session lives in the Chart tab: **Start** turns the chart into the live session.

## The methodology is the source

Everything the assistant shows carries its verdict from the register (methodology §11), so a level
or a call is never presented as more than it is. Today nothing has passed, so every rule shows as
context and every call as an untested call.

The register moves into code (`trade_desk/register.py`): one table of ideas, their verdicts and the
dates of their tests. The chart's labels, the trade ideas, the assistant's prompt and the doc's
table all read from it, which replaces today's copy in `chart.GRADES`.

The methodology doc gains three sections:

- **Discovery:** how stocks are picked. Only by a written rule, tested like any other idea:
  same-date twins (other stocks, same day), two periods. A model's pick counts only as an untested
  source.
- **Intraday and 0DTE options:**
  - Data: the quote recorder (part 8).
  - Costs: measured spreads instead of the flat 3% per leg.
  - Clusters: by day, since same-day trades share a session.
  - Risks near expiry: pin and gamma, where Black-Scholes is rough.
- **What the dashboard and the assistant may say:** what each verdict allows, and how calls and
  your plans enter forward grading.

## A session, start to finish

1. **Before the open:** the in-play list (part 7) shows which stocks are expected to move most
   today and why. You pick one to three, or take the top of the list, and press Start.
2. **Watching:**
   - the chart (1- or 5-minute bars, with the 15-minute boxes);
   - the day's levels;
   - the 0DTE contract panel;
   - a context strip: SPY and QQQ, the US market light, the time to the close, earnings;
   - your plan box;
   - the assistant's notes.
3. **Entering:** you buy in moomoo. The fill push shows the position within seconds, and the panel
   switches to *in trade*.
4. **In the trade:**
   - P&L in %;
   - the distance to your stop and target, on the stock and on the contract;
   - what the contract is worth at each level;
   - time decay per minute;
   - the cost of exiting now (the spread);
   - the time stop.

   Alerts fire on each event.
5. **After:** the session's notes, calls and your trades (from fills) are recorded, and an
   after-close recap compares what you planned with what happened.

## Parts

### 1. Session engine (deterministic, real time)

- **Start and Stop:** up to three stocks per session. It ends on Stop or at the close. It
  subscribes nothing before Start and releases everything after Stop.
- **Inputs:**
  - the chart's bar and quote pushes (`chart.py`, checked in market hours on 2026-11-06);
  - quote pushes for the contracts on the panel (the `live_feed.py` pattern);
  - your fills and positions (fill push and position reads, as in shadow mode);
  - your plans and alerts.
- **On every push, in memory and on its own thread** (like `shadow_watch.py`), it works out:
  - the day's levels: prior day high, low and close, and value areas (all exist); pre-market high
    and low, the 5- and 15-minute opening range, and VWAP from 1-minute bars (new);
  - the price's distance to each level;
  - your plan's state;
  - your position's P&L;
  - the contracts repriced by Black-Scholes at each contract's live implied volatility
    (`analytics.black_scholes_price`).
- **Events:**
  - a level touched or broken;
  - a 15-minute close, and a flag changing state;
  - a plan triggered or invalidated;
  - a stop or target reached;
  - a fill;
  - a P&L step;
  - a time stop;
  - 15 minutes before the close for a 0DTE position.

  Each event fires once.
- Every level shows its verdict. The new ones (VWAP, opening range, pre-market high and low)
  show as *not tested*.

### 2. 0DTE contract panel

- **Contracts:** the nearest expiry (0DTE where it exists, else the next), with calls and puts
  around the money. For each: bid and ask, the spread as % of mid, implied volatility, delta,
  gamma, theta per minute, volume and open interest.
- **What it takes:**
  - the move the stock needs by the close to break even;
  - each contract's estimated value if the stock reaches your stop, your target or the next
    level, now and in 30 minutes;
  - the loss at your stop as % of the account (`risk.py`).

  Estimates are labelled as estimates.
- It filters by your delta range and never picks a contract for you.

### 3. Your plan, made computable

- **Typing a plan:** you type it in words, for example "long SPY calls if it closes a 5-minute
  bar above 580, stop under 578, target 583, out by 15:30". The model turns it into fields:
  trigger, condition, invalidation, stop, target, time stop, instrument.
- **Confirming:** you confirm the form before anything is watched; a guess is never acted on.
  The form can also be filled by hand.
- **Watching:** the engine watches the plan and alerts on trigger, invalidation, stop, target and
  time.
- **Grading:** each plan is recorded and graded forward like your chart marks
  (`chart_grades.py`). It is the fair test of your own judgment, the part the methodology could
  not test so far.

### 4. The assistant: notes, calls and questions (Sonnet 5.5)

- **When it speaks:**
  - on the engine's events, at most one note per stock every few minutes, except for a fill, a
    stop or a target;
  - when you ask it something.

  It never runs on every tick. A daily cap on calls and spend is set in configuration, and the
  panel shows what's been used.
- **What it gets:** a structured state:
  - recent bars;
  - the levels with their verdicts;
  - the flags;
  - the contract panel;
  - your position as % and your plan;
  - the context strip;
  - the register.
- **What it writes:**
  - a short note: what changed, where the price sits, what each rule says and its verdict, what
    your plan says, and the risk in % of the account;
  - optionally a call (direction, entry condition, stop, target, time stop, contract), labelled
    *untested model call* and graded like your plans.
- **Guards:**
  - every price it writes must match a computed level or the quote, or the note is held back;
  - it gives no sizing advice beyond the % math;
  - it says plainly when a rule has no edge.
- **Questions:** for example, "take profit here?" gets an answer built from the numbers and the
  evidence, not a bare yes.
- **Speed:** a note reaches the page a few seconds after its event. The engine's alerts never wait
  for the model.

### 5. Alerts

The page shows a browser notification and plays a sound for each engine event. Model notes follow
on the page. A background tab keeps alerting while it is open. Discord and moomoo are not used
(decision 2).

### 6. Grading (methodology §6 and §9)

- **Sources:** your plans, the assistant's calls and the rule flags. Each is compared with twins
  at random times: the same stock and session, the same entry mechanics and exits.
- **Statistics:** clustered by day, at least 30 finished trades, and the verdict words of §6.
- **Options results:** % of premium at recorded quotes (sell at the bid, buy back at the ask) once
  the recorder has them. Until then, results are in R on the stock.
- **Where they show:** new rows in the Forward grades table (*Your plans*, *Assistant calls*), and
  a daily recap on the dashboard.

### 7. Discovery: which stocks today (methodology rule)

- **What is tested:** options pay for movement, so the day's question is which stocks will move
  most, not which way. The candidate rule is a pre-open list built from the gap, volume relative
  to normal, and news or earnings. The test is whether it picks stocks with a larger same-day
  range than same-date twins.
- **How:** pre-registered on moomoo 15-minute history since 2018, over two periods. Range is far
  easier to predict than direction, so this could become the first *pass* in the register.
- **Output:** before the open, the in-play names with their expected range against normal,
  labelled with the rule's verdict. "Let the agent pick" means the top of this list, not a guess
  by the model.
- **Swing ideas:** the existing scan is labelled with its rule and verdict and follows
  decision 3.

### 8. 0DTE quote recorder (starts first)

- **Why first:** there are no historical option quotes (methodology §7), so 0DTE rules can't be
  backtested honestly yet. The only way to get the data is to record it from now on.
- **What it records, every trading day:**
  - SPY, QQQ and the underlyings you traded options on in the last 120 days, plus the stocks of
    any session;
  - the nearest expiry within a band around the price;
  - bid, ask, implied volatility, greeks and volume, every 15 seconds, through
    `get_market_snapshot` (no subscription needed, within its rate limit);
  - the stock's 1-minute bars.
- **Storage:** compressed daily files on the local data disk, not the home folder and not SQLite
  on NFS. The size gets measured in the first week.
- **Then:** after about 60 sessions, your setups and the assistant's calls get tested on real
  quotes.

## Phases

| Phase | What | Needs |
|---|---|---|
| 0 | Methodology sections, `register.py`, trade ideas made strict (decision 3) | Small. Changes what Discord gets |
| 1 | 0DTE quote recorder | Start soon: the data only accumulates forward |
| 2 | Session engine, contract panel, Start/Stop, dashboard alerts (no model yet) | The core |
| 3 | Your plan: typed or filled in, watched, graded | Phase 2 |
| 4 | Assistant notes, calls and questions, with grading of its calls | Phase 2. Model cost per day measured here |
| 5 | Pre-registered in-play range test, then the in-play list | moomoo history quota (1,000 stocks per 30 days) |
| 6 | 0DTE research on recorded quotes | About 60 sessions of phase 1 data |

Each phase ships on its own, with tests, docs and a changelog line, like the chart's phases.

## Limits and costs

- **Model:** expected tens of calls per stock per session. Phase 4 measures the tokens per call
  and shows the day's cost on the panel, with a cap in configuration.
- **OpenD:** the chart, shadow mode and the recorder share one subscription quota and the snapshot
  rate limit. Phase 1 checks both (`query_subscription`) before choosing how many strikes to
  record.
- **Host:** small. The market-hours probe on 2026-11-06 used 2–5% of a core for five live charts.

## Not part of this

- Orders of any kind, the trading password, or a moomoo paper account (a separate decision,
  methodology §1).
- Discord or moomoo alerts for the assistant (decision 2).
- Advice on position size beyond showing the loss at the stop.

## Risks

- **A model narrating in real time sounds sure of itself.** The verdict labels and the forward
  grading are the guard, and the register records that the model's review of ideas showed no
  measurable value so far.
- **0DTE estimates near the close are rough** (pin, gamma, implied volatility moving within the
  day). They are labelled as estimates.
- **Attention:** alerts must stay few, at one per event, so the ones that matter get noticed.
