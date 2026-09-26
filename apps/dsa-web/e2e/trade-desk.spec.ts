import fs from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

// Run against a local authenticated dashboard; these fixtures never submit orders.
const baseURL = process.env.TRADE_DESK_SMOKE_URL;
const storageState = process.env.TRADE_DESK_SMOKE_AUTH;
const adviceFile = process.env.TRADE_DESK_SMOKE_ADVICE;
test.skip(!baseURL || !storageState || !adviceFile, 'Set the Trade Desk smoke URL, local session, and synthetic advice fixture.');
test.use({ baseURL, storageState, locale: 'en-US', video: 'off' });

async function setup(page: Page) {
  const advice = JSON.parse(fs.readFileSync(adviceFile!, 'utf8'));
  expect(advice.request.data_mode).toBe('replay');
  await page.addInitScript(() => { localStorage.setItem('dsa.uiLanguage', 'en'); });
  await page.route('**/api/v1/trade-desk/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/health')) return route.continue();
    if (path.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: ': replay fixture\n\n' });
    if (path.endsWith('/catalog')) return route.continue();
    let body: unknown = { items: [] };
    if (path.endsWith('/advice')) body = route.request().method() === 'POST' ? advice : { items: [advice] };
    else if (path.includes('/advice/')) body = advice;
    else if (path.endsWith('/preferences')) body = { discord_enabled: false };
    else if (path.endsWith('/outcomes')) body = { paper: { closed_trades: 0, realized_pnl: 0, win_rate: null }, manual_live: { closed_trades: 0, realized_pnl: 0, win_rate: null } };
    await route.fulfill({ json: body });
  });
  return advice;
}

test('authenticated replay comparison and on-demand entry render', async ({ page }, info) => {
  const advice = await setup(page);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/trade-desk?ticker=AAPL');
  await expect(page.getByRole('heading', { name: 'Trade Desk', exact: true })).toBeVisible();
  await expect(page.getByLabel('Ticker', { exact: true })).toHaveValue('AAPL');
  await expect(page.getByText(advice.candidates[0].title, { exact: true }).first()).toBeVisible();
  await expect(page.locator('option[value="manual_live"]').first()).toBeDisabled();
  await expect(page.getByText(advice.candidates[0].entry_conditions[0], { exact: false }).first()).toBeVisible();
  await expect(page.getByText(advice.candidates[0].exit_conditions[0], { exact: false }).first()).toBeVisible();
  await page.screenshot({ path: info.outputPath('trade-desk-comparison.png'), fullPage: true });
  await page.getByRole('tab', { name: 'Ask about a stock', exact: true }).click();
  await expect(page.getByLabel('Ticker', { exact: true })).toHaveValue('AAPL');
  await page.getByRole('tab', { name: 'Positions', exact: true }).click();
  await page.screenshot({ path: info.outputPath('trade-desk-positions.png'), fullPage: true });
  await page.getByRole('tab', { name: 'Journal', exact: true }).click();
  await page.screenshot({ path: info.outputPath('trade-desk-diagnostics.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('saved report links to the selected stock and report', async ({ page }, info) => {
  await setup(page);
  await page.route('**/api/v1/history**', async (route) => {
    if (new URL(route.request().url()).pathname.endsWith('/markdown')) {
      return route.fulfill({ json: { content: '# AAPL\n\nSynthetic report fixture for Trade Desk navigation.' } });
    }
    return route.fulfill({ json: { total: 1, page: 1, limit: 50, items: [{ id: 101, stock_code: 'AAPL', stock_name: 'Apple', report_type: 'simple', created_at: '2026-09-22T13:00:00Z', analysis_summary: 'Synthetic report fixture.' }] } });
  });
  await page.goto('/reports');
  const link = page.locator('a[href*="/trade-desk?ticker=AAPL"]').first();
  await expect(link).toBeVisible();
  await page.screenshot({ path: info.outputPath('trade-desk-report-entry.png'), fullPage: true });
  await link.click();
  await expect(page).toHaveURL(/trade-desk\?.*ticker=AAPL/);
  await expect(page.getByLabel('Ticker', { exact: true })).toHaveValue('AAPL');
});

test('US screening result links to strategy exploration', async ({ page }, info) => {
  await setup(page);
  await page.addInitScript(() => sessionStorage.setItem('dsa.screening.activeScreenTask.v1', JSON.stringify({ taskId: 'smoke', runId: 'smoke', market: 'us', strategy: 'us_gainers', maxResults: 3 })));
  await page.route('**/api/v1/screening/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = { enabled: true, available: true };
    if (path.endsWith('/strategies')) body = { enabled: true, strategy_count: 1, strategies: [{ id: 'us_gainers', name: 'US gainers', description: 'Synthetic smoke fixture', market_scope: ['us'] }] };
    else if (path.endsWith('/history/smoke')) body = { run_id: 'smoke', market: 'us', strategy: 'us_gainers', result: { enabled: true, market: 'us', strategy: 'us_gainers', candidates: [{ rank: 1, code: 'AAPL', name: 'Apple', reason: 'Synthetic smoke fixture', score: 75, price: 100, change_pct: 2, raw: {} }], candidate_count: 1 } };
    else if (path.endsWith('/history')) body = { runs: [] };
    else if (path.endsWith('/hotspots')) body = { enabled: true, hotspots: [], hotspot_count: 0 };
    await route.fulfill({ json: body });
  });
  await page.goto('/screening');
  const link = page.locator('a[href*="/trade-desk?ticker=AAPL"]').first();
  await expect(link).toBeVisible();
  await page.screenshot({ path: info.outputPath('trade-desk-screening-entry.png'), fullPage: true });
  await link.click();
  await expect(page.getByLabel('Ticker', { exact: true })).toHaveValue('AAPL');
});


test('notification links open saved advice and the referenced plan', async ({ page }, info) => {
  const advice = await setup(page);
  await page.route('**/api/v1/trade-desk/advice', (route) => route.fulfill({ json: { items: [] } }));
  await page.goto(`/trade-desk?adviceId=${advice.id}`);
  await expect(page.getByText(advice.candidates[0].title, { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: info.outputPath('trade-desk-advice-link.png'), fullPage: true });
  const plan = { id: 'notification-plan', advice_id: advice.id, candidate: advice.candidates[0],
    ledger: 'paper', data_mode: 'replay', status: 'open', monitoring: true };
  await page.route('**/api/v1/trade-desk/plans', (route) => route.fulfill({ json: { items: [plan] } }));
  await page.route('**/api/v1/trade-desk/positions', (route) => route.fulfill({ json: { items: [{
    plan_id: plan.id, underlying: advice.request.ticker, ledger: 'paper', status: 'open',
    legs: [], realized_pnl: 0, fees: 0, unrealized_pnl: null, valuation_status: 'unavailable', plan,
  }] } }));
  await page.goto(`/trade-desk?planId=${plan.id}`);
  await expect(page.getByRole('tab', { name: 'Positions', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#trade-plan-notification-plan')).toBeFocused();
  await page.screenshot({ path: info.outputPath('trade-desk-plan-link.png'), fullPage: true });
});
