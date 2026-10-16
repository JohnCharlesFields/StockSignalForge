const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const dist = process.env.QA_DIST || path.resolve(__dirname, '../dist');
const output = process.env.QA_OUTPUT || path.resolve(__dirname, '../../.tmp/forward-version-20261014');
const cohort = { cohort_id: 'A', ranking_version: 'pullback_parameters_v1:6673443b16f7',
  calibration_ids: ['pullback_hv_8d_replay_20260621233523'], metadata_complete: true,
  horizon_days: 8, total: 30, resolved: 20, pending: 10, resolved_dates: 2,
  first_signal_date: '2026-10-08', last_signal_date: '2026-10-14', realized_win_rate: .4,
  mean_net_return: -.01, mean_net_excess: -.02, overlap_adjusted_ci: [null, null] };
const scorecard = { available: true, counts: { total: 50, resolved: 20, pending: 30, board_decisions: 3 },
  evaluation_note: '记录后开盘口径，旧收盘概率不可比', version_note: '总量是描述性汇总，版本分别查看',
  n_resolved: 20, trading_days: 2, realized_win_rate: .4, predicted_win_rate: null,
  mean_net_return: -.01, mean_net_excess: -.02, net_excess_ci: [null, null], overlap_adjusted_ci: [null, null],
  rank_buckets: [{label: 'Top1-3', n: 20, predicted_win_rate: null, realized_win_rate: .4, mean_net_excess: -.02}],
  version_cohorts: [cohort, {...cohort, cohort_id: 'B', horizon_days: 5, resolved: 0, pending: 30,
    ranking_version: null, metadata_complete: false, calibration_ids: [], realized_win_rate: null,
    mean_net_return: null, mean_net_excess: null, resolved_dates: 0}] };

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const server = http.createServer((req, res) => {
    const p = new URL(req.url, 'http://localhost').pathname;
    const file = p.startsWith('/assets/') ? path.resolve(dist, '.' + p) : path.join(dist, 'index.html');
    if (!file.startsWith(dist + path.sep) || !fs.existsSync(file)) return res.writeHead(404).end();
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = process.env.QA_BASE || `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  const checks = [], errors = [], writes = [];
  try {
    for (const scenario of ['full', 'empty', 'error']) for (const width of [1440, 390]) {
      const context = await browser.newContext({viewport: {width, height: 1000}});
      const versions = [];
      await context.route('**/*', async route => {
        const req = route.request(), url = new URL(req.url());
        if (url.origin === base && req.method() === 'GET' && (req.resourceType() === 'document' || url.pathname.startsWith('/assets/'))) return route.continue();
        if (req.method() !== 'GET') { writes.push(url.pathname); return route.abort(); }
        if (url.pathname === '/priority-board') return route.fulfill({json: {picks: [], n_candidates: 0, confidence: {badge: 'experimental'}}});
        if (url.pathname === '/predictions/scorecard') {
          const v = url.searchParams.get('evaluation_version'); versions.push(v);
          if (scenario === 'error') return route.fulfill({status: 503, json: {detail: 'FIXTURE_READ_FAILURE'}});
          if (v === 'legacy') await new Promise(resolve => setTimeout(resolve, 700));
          return route.fulfill({json: scenario === 'empty' ? {available: false, counts: scorecard.counts, version_cohorts: []}
            : {...scorecard, evaluation_note: v === 'legacy' ? 'OLD_LEGACY' : 'NEW_RECORDED'}});
        }
        return route.fulfill({json: {items: [], picks: [], rows: [], counts: {}, status: 'idle', job: {status: 'idle'}, snapshot: {}, validation: {}}});
      });
      const page = await context.newPage();
      page.on('pageerror', e => errors.push(e.message));
      try {
        await page.goto(base + '/');
        await page.getByRole('button', {name: '兑现对账', exact: true}).click();
        await page.getByLabel('结算口径').waitFor();
        assert.equal(await page.getByLabel('结算口径').inputValue(), 'recorded_open_v3');
        if (scenario === 'full') {
          const table = page.getByRole('table', {name: '冻结版本前向对账'});
          await table.waitFor();
          assert.equal(await table.locator('tbody tr').count(), 2);
          assert.equal(await table.getByText('版本未归档', {exact: true}).count(), 1);
          assert.equal(await table.getByText('实际超额命中', {exact: true}).count(), 1);
          if (width === 1440) {
            await page.getByLabel('结算口径').selectOption('legacy');
            await page.waitForTimeout(50);
            await page.getByLabel('结算口径').selectOption('recorded_open_v3');
            await page.waitForTimeout(1000);
            assert.equal(await page.getByText('OLD_LEGACY', {exact: true}).count(), 0);
            assert.equal(await page.getByText('NEW_RECORDED', {exact: true}).count(), 1);
          }
          await table.screenshot({path: path.join(output, `${process.env.QA_BASE ? 'deployed-' : ''}${width}-cohorts.png`)});
        } else if (scenario === 'error') {
          await page.getByText('FIXTURE_READ_FAILURE', {exact: true}).waitFor();
          assert.equal(await page.getByRole('table', {name: '冻结版本前向对账'}).count(), 0);
        } else {
          await page.getByText(/实盘前向还没有已结算/).waitFor();
        }
        await page.waitForTimeout(100);
        assert(versions.every(v => v === 'recorded_open_v3' || v === 'legacy'));
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        checks.push({scenario, width});
      } finally { await context.close(); }
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    fs.writeFileSync(path.join(output, process.env.QA_BASE ? 'ui-deployed.json' : 'ui.json'), JSON.stringify({checks, errors, writes}, null, 2));
    console.log(JSON.stringify({passed: checks.length, errors, writes}));
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
