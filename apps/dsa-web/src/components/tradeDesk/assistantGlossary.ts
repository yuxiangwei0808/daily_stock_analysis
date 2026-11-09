/** "What these mean": the shadow mode card's labels, section by section (AssistantPanel.tsx, folded at the bottom).
 * Keep it in step with the card and with docs/trade-desk.md (Shadow mode). */
export interface GlossarySection {
  title: string;
  entries: Array<[term: string, meaning: string]>;
}

export const GLOSSARY_INTRO = 'Nothing on this card is a tested trading signal. The one thing that has passed a test is the in-play list, and only as a pick of which stocks move more today, not which way.';

export const GLOSSARY: GlossarySection[] = [
  { title: 'Top of the card', entries: [
    ['0DTE options, Intraday, Swing', 'The horizon you started with. It sets which option expiry the contracts show.'],
    ['+4.31%', 'The change from yesterday\'s close.'],
    ['last trade 3.2 s ago', 'How fresh the quote is. Before 9:30 the price is the last pre-market trade.'],
  ] },
  { title: 'Assistant notes', entries: [
    ['Watcher', 'The fast model. It reads each stock every completed minute and right after a market event.'],
    ['Analyst', 'The stronger, slower model. It writes on what the watcher sends it, on your plans and positions, and answers your questions.'],
    ['Grey text after a watcher note', 'Why the watcher did not pass it on.'],
    ['→ Analyst: …', 'The watcher passed it to the analyst, and why.'],
    ['Watcher, each minute', 'The watcher\'s latest quiet read, replaced every minute.'],
    ['Call: …', 'A trade the analyst suggested: an untested model call, tracked like a plan and graded after the close. Not advice.'],
    ['A note was held back', 'The model quoted a price the data does not show, so the note is hidden.'],
  ] },
  { title: 'Your positions', entries: [
    ['on cost', 'Profit or loss against what you paid.'],
    ['of the account', 'The position\'s share of the account.'],
    ['live feed, last sync', 'Seconds old, or from the last holdings sync.'],
  ] },
  { title: 'Your plan', entries: [
    ['Waiting', 'The entry has not triggered yet.'],
    ['In the trade', 'It triggered and is being watched.'],
    ['Graded', 'Scored after the close.'],
    ['Void', 'Your cancel price traded first, or it triggered already past its stop or target.'],
    ['Expired', 'The day ended before it triggered.'],
    ['+0.71R', 'The result in units of your risk; 1R is the distance from the entry to the stop.'],
    ['random timing −0.20R', 'The same trade at 20 random times that day, averaged: the baseline.'],
    ['Assistant call', 'A plan made from the analyst\'s call, not from you.'],
  ] },
  { title: 'Levels', entries: [
    ['Prior day high, low, close', 'Yesterday\'s prices.'],
    ['Value-area high, low', 'The range where about 70% of a week\'s volume traded, for each of the last two weeks.'],
    ['Pre-market high, low', 'This morning\'s pre-market extremes.'],
    ['Opening range (5 or 15 min)', 'The high and low of the first 5 or 15 minutes after 9:30.'],
    ['VWAP', 'Today\'s average price, weighted by volume.'],
    ['Price now', 'Where the price sits among the levels. The distance is each level against it.'],
    ['not tested', 'Never tested as a level on its own: every level here today.'],
    ['no edge shown', 'Tested, and not better than random timing.'],
    ['rejected', 'Tested, and worse than random timing (t at or below −2) or losing money after costs, in both test periods.'],
    ['yours', 'Your own stop or target.'],
  ] },
  { title: 'Box flags', entries: [
    ['Box', 'After one 15-minute bar moves at least a typical day\'s range, the bodies of the 8 bars after it make the range box and the 8 before it the base box.'],
    ['Fade short', 'Sell at a box\'s bottom edge when the price climbs back up to it from below: a bet that the bounce fails. Stop past the box, target twice the stop distance.'],
    ['Fade long', 'Buy at a box\'s top edge when the price drops back to it from above: a bet that the dip holds.'],
    ['Break and retest', 'After a close outside the box, an order back at its edge.'],
    ['armed, watching, open', 'The next 15-minute bar fills it if it trades there; it needs a close outside the box first; it was filled.'],
    ['t −1.78 / −0.90', 'The rule against the same trades at random times, in 2020–23 and 2023–26. A pass needs about +2 or more in both, and a profit after costs.'],
  ] },
  { title: 'Contracts', entries: [
    ['Spread', 'The gap between bid and ask, as % of the middle. A wide one costs you going in and out.'],
    ['IV', 'Implied volatility: how much movement the option\'s price assumes.'],
    ['Δ', 'Delta: roughly how much the option moves for each $1 in the stock.'],
    ['To break even', 'The stock move needed by expiry to get back what the option costs.'],
    ['Decay a minute', 'Value lost to time each minute.'],
    ['At VWAP 191.89: 2.06 → 2.03', 'Its estimated worth if the stock traded at that level now, and in 30 minutes. An estimate, not a quote.'],
  ] },
  { title: 'Alerts', entries: [
    ['NVDA below Opening range low', 'The price traded 0.05% past the level. Each level and direction alerts at most every 15 minutes.'],
    ['its best this session', 'A position reached a new high for the session. Also: a new low below cost, giving back half of its best gain, a new position.'],
    ['Close warnings', '30 and 15 minutes before the close, while you hold a contract expiring today.'],
    ['assistant note', 'An analyst note, listed here too so you see it for every stock.'],
  ] },
  { title: 'Models today', entries: [
    ['analyst 22 of 150 calls', 'The analyst\'s calls today against its daily limit.'],
    ['watcher 121 reads, 18 sent', 'The watcher\'s reads today, and how many it passed to the analyst.'],
    ['too early: 0 of 30 graded days', 'Whether the watcher picks moments that move more. It needs 30 trading days of results first.'],
  ] },
];
