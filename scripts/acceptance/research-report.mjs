/** Real static report controls; no source acquisition, model or account IO. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { loadChromium } from './lib/playwright.mjs';

const options = {};
for (let index = 2; index < process.argv.length; index += 2) {
  const key = process.argv[index], value = process.argv[index + 1];
  if (!['--html', '--empty-html', '--evidence-dir', '--executable'].includes(key) || !value) {
    throw new Error('Usage: research-report.mjs --html FILE --empty-html FILE --evidence-dir NEW_DIR [--executable CHROME]');
  }
  options[key] = resolve(value);
}
for (const key of ['--html', '--empty-html', '--evidence-dir']) assert.ok(options[key], key);
const output = options['--evidence-dir'];
await mkdir(output, { recursive: false });
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const browser = await loadChromium(join(root, 'governance')).launch({ headless: true,
  ...(options['--executable'] ? { executablePath: options['--executable'] } : {}) });
const receipt = { schema: 'tiger.research-report-browser/v1',
  browser: 'Installed Chrome, isolated headless QA context; not the user profile',
  inputs: {}, cases: [], pageErrors: [], blockedNetwork: [] };
for (const key of ['--html', '--empty-html']) {
  receipt.inputs[key] = { path: options[key], sha256: createHash('sha256').update(await readFile(options[key])).digest('hex') };
}

async function checkWidth(page) {
  const dimensions = await page.evaluate(() => ({ viewport: innerWidth, page: document.documentElement.scrollWidth,
    offenders: [...document.querySelectorAll('.research-security-panel *,.research-overview *')]
      .filter((node) => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden')
      .filter((node) => { const box = node.getBoundingClientRect(); return box.left < -1 || box.right > innerWidth + 1; })
      .map((node) => ({ tag: node.tagName, id: node.id, className: node.className })) }));
  assert.ok(dimensions.page <= dimensions.viewport + 1, JSON.stringify(dimensions));
  assert.equal(dimensions.offenders.length, 0, JSON.stringify(dimensions));
  return dimensions;
}

try {
  for (const width of [320, 390, 768, 1440]) {
    const context = await browser.newContext({ viewport: { width, height: 920 }, locale: 'zh-CN', reducedMotion: 'reduce' });
    await context.route('**/*', async (route) => {
      if (/^(file:|data:)/.test(route.request().url())) return route.continue();
      receipt.blockedNetwork.push(route.request().url());
      return route.abort();
    });
    const page = await context.newPage();
    page.on('pageerror', (error) => receipt.pageErrors.push({ width, message: error.message }));
    const result = { width, checks: [], screenshots: [], error: null };
    try {
      await page.goto(pathToFileURL(options['--html']).href);
      assert.equal(await page.locator('.research-axis').count(), 4);
      assert.match(await page.locator('.research-axis[data-axis="account"]').innerText(), /等待账户发布核验/);
      assert.match(await page.locator('.research-identity').innerText(), /run-original/);
      await page.locator('.research-overview').screenshot({ path: join(output, `status-${width}.png`), animations: 'disabled' });
      result.screenshots.push(`status-${width}.png`);
      result.checks.push('four-axes-and-original-identity');

      await page.locator('#daily-btn-stock').click();
      assert.equal(await page.locator('[data-research-security]').count(), 7);
      assert.equal(await page.locator('[data-research-security]:visible').count(), 7);
      assert.equal(await page.locator('#research-security-reset').isDisabled(), true);
      result.checks.push('all-seven-securities-and-disabled-reset');
      const detail = page.locator('[data-research-security]').first().locator('summary');
      await detail.click();
      assert.equal(await detail.locator('..').getAttribute('open'), '');
      result.checks.push('real-evidence-details-open');
      result.layout = await checkWidth(page);
      await page.locator('#research-security-search').scrollIntoViewIfNeeded();
      await page.screenshot({ path: join(output, `cards-${width}.png`), animations: 'disabled' });
      result.screenshots.push(`cards-${width}.png`);

      await page.locator('#research-security-filter').selectOption('HOLD');
      assert.equal(await page.locator('[data-research-security]:visible').count(), 1);
      assert.match(await page.locator('[data-research-security]:visible h3').innerText(), /^HOLD$/);
      assert.equal(await page.locator('#research-security-reset').isEnabled(), true);
      result.checks.push('hold-excludes-wait-and-pending-research');
      await page.locator('#research-security-search').fill('nonexistent<>&');
      assert.equal(await page.locator('[data-research-security]:visible').count(), 0);
      assert.equal(await page.locator('#research-security-empty').isVisible(), true);
      assert.match(await page.locator('#research-security-count').innerText(), /显示 0 \/ 7/);
      await page.locator('#research-security-empty').scrollIntoViewIfNeeded();
      await page.screenshot({ path: join(output, `filtered-empty-${width}.png`), animations: 'disabled' });
      result.screenshots.push(`filtered-empty-${width}.png`);
      result.checks.push('special-character-query-and-empty-state');
      await page.locator('#research-security-reset').click();
      assert.equal(await page.locator('[data-research-security]:visible').count(), 7);
      assert.equal(await page.locator('#research-security-reset').isDisabled(), true);
      assert.equal(await page.locator('#research-security-search').evaluate((node) => node === document.activeElement), true);
      result.checks.push('reset-restores-originals-and-keyboard-focus');

      const link = page.locator('[data-research-security]').first().locator('.research-source-links a').first();
      const href = await link.getAttribute('href');
      await link.click();
      assert.equal(new URL(page.url()).hash, href);
      assert.equal(await page.locator(href).isVisible(), true);
      assert.match(await page.locator(href).innerText(), /BUY 原文/);
      assert.equal(await page.locator('.research-source').count(), 4);
      result.checks.push('same-document-original-source-navigation');
      await context.setOffline(true);
      await page.locator('#research-security-filter').selectOption('PENDING_RESEARCH');
      assert.equal(await page.locator('[data-research-security]:visible').count(), 1);
      assert.match(await page.locator('[data-research-security]:visible h3').innerText(), /^LATE$/);
      assert.match(await page.locator('.research-identity').innerText(), /run-original/);
      result.checks.push('offline-static-filter-preserves-original-run');
      await context.setOffline(false);
      await page.goto(pathToFileURL(options['--empty-html']).href);
      await page.locator('#daily-btn-stock').click();
      assert.equal(await page.locator('[data-research-security]').count(), 0);
      assert.match(await page.locator('.research-security-panel').innerText(), /本报告没有逐证券记录/);
      await checkWidth(page);
      result.checks.push('genuine-empty-report-is-unknown');
    } catch (error) {
      result.error = { name: error.name, message: error.message };
      await page.screenshot({ path: join(output, `failure-${width}.png`), animations: 'disabled' });
    } finally {
      receipt.cases.push(result);
      await context.close();
    }
  }
} finally {
  await browser.close();
}
receipt.failed = receipt.cases.filter((result) => result.error).length;
receipt.passed = receipt.cases.length - receipt.failed;
receipt.assertions = receipt.cases.reduce((total, result) => total + result.checks.length, 0);
receipt.exitCode = receipt.failed || receipt.pageErrors.length || receipt.blockedNetwork.length ? 1 : 0;
await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify(receipt, null, 2));
process.exitCode = receipt.exitCode;
