# Trade Desk validation — September 22, 2026

This records local implementation validation, not a claim of trading profitability
or verified broker integration. No broker orders were submitted, and no paid data
subscription was introduced.

## Feature validation

The final focused backend checks passed: **99 Trade Desk tests** (46 service and
ledger checks plus 53 provider, analytics, and independent payoff fixtures).

- Backend acceptance tests cover the 17-strategy catalog, independent payoff and
  breakeven fixtures, fees, multipliers, unlimited loss, owned shares, American
  pricing, fractional expiration time, probability sensitivity, and missing inputs.
- Provider fixtures exercise stale/crossed quotes, quote identity, missing rights,
  quota exhaustion, reconnection, holiday/early-close timing, and unsupported
  adjusted/index contracts. Replay is explicitly synthetic.
- Service tests cover authentication, cancellation, structured Codex validation,
  stale proposals, immutable advice versions, worker leases/recovery, partial fills,
  paper/live separation, assignment reconciliation, same-timestamp fill ordering,
  owned-share coverage, and durable notification deduplication/retries.
- A real local Codex request produced replay comparisons. A real follow-up used
  the isolated calculation tool to change the allocation from $1,500 to $500 and
  compare bearish strategies while retaining the original version. Usage and tool
  calls were persisted. The examples are synthetic, not investment recommendations.
- Frontend checks passed: 11 Trade Desk page/API tests. The earlier implementation
  validation also passed 77 affected report, screening, and sidebar tests. The production build passes. Full frontend
  lint reports no errors and two existing hook-dependency warnings.
- Four authenticated Chromium smoke tests passed: Trade Desk comparisons,
  report-to-Trade-Desk navigation, screening-to-Trade-Desk navigation, and
  notification links to saved advice and positions. They use
  recorded synthetic Codex advice with fixture responses, plus the running
  dashboard's real authentication, catalog, and provider-health endpoints.

Screenshots and local smoke output are under `local/trade-desk/browser/` (ignored
from version control). The private browser session fixture is not a deliverable.

## Self-review fixes

The follow-up review fixed four concrete paths without introducing new services
or configuration:

- Follow-ups retain the revised inputs shared by the displayed comparisons;
  successive calculator calls also retain their most recent inputs.
- Reconciliation requires complete exercise/assignment share deliveries and no
  remaining expired options. Backdated events entered after an acknowledgement
  reopen reconciliation. Validation and acknowledgement use one transaction.
- Quote selection prioritizes the exact selected/held contracts; monitored plans
  for the same stock and expiry request their contracts together within the
  existing limit, keeping their cached valuations consistent.
- Notification links select the referenced advice (even outside the recent list)
  or open and focus the referenced position.

The 99 backend and 11 frontend focused tests above were rerun after these fixes.
Python compilation and critical flake8 checks passed. The production build and
frontend lint passed (the same two existing screening warnings). Repository-wide
results below are from implementation validation; the entire suites were not
repeated for these bounded follow-up fixes.

## Repository-wide checks

The complete backend CI gate was run. Its offline suite reported 6,953 passed,
20 failed, 4 deselected, and 664 passing subtests. Re-running those 20 failures
with test-only authentication/language/backend settings resolved 15. The five
remaining failures are in the existing agent-pipeline fixture
(`SimpleNamespace.set_realtime_quote`) and four existing configuration-validation
tests involving disabled model channels and available local Codex backends.
The Trade Desk tests passed. The overall backend gate is therefore **not green**.

The complete frontend suite was run after the navigation integration fixes with a
15-second per-test timeout: 1,231 passed, 5 failed, and 2 skipped. The remaining
failures concern an existing native-title governance violation, decision-signal
refresh, saved-report grouping, JP/KR alert translations, and TickFlow settings
translations. These were not changed to force the new feature's checks to pass.
The overall frontend test suite is therefore **not green**.

## Live integration gaps

The restarted local dashboard reports an active Trade Desk worker and available
synthetic replay. OpenD is unconfigured; its default local port was closed. The
environment has the pinned Futu SDK compatibility path, but native moomoo SDK
access, login, stock/OPRA permissions, real subscription quotas, and authenticated
option bid/ask data have not been verified. Live functionality must remain labeled
unverified until those checks succeed.

Discord credentials are not configured. Delivery failure/retry/deduplication is
tested with fixtures; actual Discord delivery remains unverified. Configure it
through the existing Settings page.

Unknown ETF expiration cutoffs and late-close ETF contracts on early-close dates
remain unavailable without verified timing. Adjusted contracts, cash-settled index
options, calendars, and diagonals are intentionally outside this version.

## Rollback

Set `TRADE_DESK_ENABLED=false` and restart. This stops the independent worker and
hides the navigation entry while preserving the additive tables and the existing
research features. No commit or push is part of this delivery.

## Review fixes — September 23, 2026

A follow-up review found and fixed defects that the earlier fixtures did not exercise:

- The Web client camel-cased the keys of ID-keyed maps (`triggers`, `snapshots`,
  `source_snapshots`, `effective_requests`), turning hex UUIDs such as `3f2a…` into
  `3F2A…`. Suggested plan levels, per-candidate quote provenance and follow-up input
  retention therefore never applied in the browser; the earlier tests used
  non-hex IDs. Plan creation now also sends cleared levels explicitly.
- Covered-call plans on owned shares showed as open before any fill (suppressing
  their entry alert) and a paper close sold the owned shares.
- Expired unfilled plans kept monitoring; live outage alerts fired hourly outside
  the regular session; fills were ordered by ISO string rather than time.
- OpenD adapter: subscription checks ignored subscription type and connection,
  rejected unsubscribes leaked quota, IV 0 crashed a snapshot, the underlying had
  no bid/ask from `get_stock_quote`, open interest was never read, health ignored
  quote login, and the calendar excluded LEAPS. The fake SDK now matches those SDK
  contracts. These remain fixture-verified only; no OpenD session was available.

After these fixes: 313 backend tests (Trade Desk, US session, alert worker,
screening) and 81 affected frontend tests passed; lint (same two warnings) and the
production build passed. The Playwright smoke tests were not rerun.

## Second review — September 23, 2026

A second pass verified the numerical core independently: expiration bounds and
break-evens matched a brute-force grid for all 17 strategies, expiration
probabilities matched a 400k-path lognormal Monte Carlo within 0.0015, and
pre-expiry probabilities matched a dense integration within 0.0012. It then fixed:

- Web: live was never blocked when OpenD was unconfigured (the page checked codes
  the backend never sends); replay alerts lost their mode label in the journal;
  manual fills carried the previous plan's intent/quantity; fill quantities were
  silently coerced; a `?ticker=` link could not be cleared; plans could not be
  paused or archived; replay paper results were mixed into paper outcomes.
- Service/worker: expired paper positions could never close (new paper
  settlement); a 0.02% spot move could mark advice stale by re-selecting strikes
  (selected contracts are now re-priced); a crashed leader's running jobs were not
  recovered after a fast restart; expired held positions raised hourly outages;
  one bad watchlist code aborted discovery; "volatile" defaults included short
  volatility; iron condors could collapse to an iron butterfly; follow-up
  allocations like "5000." or "$5k" were rejected; cancel could overwrite a finished
  job; quotes a fraction of a second ahead of the local clock were stale; Discord
  text could mention `@everyone`.
- US monitoring (outside Trade Desk): a leftover extended-hours price from an
  earlier session produced a false session move; Longbridge and index quotes had no
  session metadata and were always stale; the new stale gates also blocked A-share/HK
  alerts and overlays (restored to prior behavior); US quotes had two freshness
  thresholds; US market reviews always included an empty sector section; early-close
  after-hours ran to 20:00.

Live OpenD, real Discord delivery and the Playwright smoke tests remain unverified.
