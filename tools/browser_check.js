/* Real-browser responsive checks for the CityU Mail Pilot UI.
 *
 * Loads the running app (no stubs), signs in, walks every section at mobile and
 * desktop widths, and fails loudly on horizontal overflow, clipped text, a
 * console error, or a missing "next step" call to action.
 *
 * Usage: node tools/browser_check.js http://127.0.0.1:8790 /tmp/pilot-ui/shots
 */
'use strict';

const { chromium } = require('./pw');
const { goTo } = require('./nav');
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://127.0.0.1:8790';
const SHOTS = process.argv[3] || '/tmp/pilot-ui/shots';
// $PILOT_ADMIN and the shared seeded password, like every other check here.
// A hard-coded fixture account made this one fail as a login timeout.
const EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = process.env.PILOT_PASSWORD || 'a-long-enough-password';

const VIEWPORTS = [
  { name: 'mobile-360', width: 360, height: 780 },
  { name: 'mobile-390', width: 390, height: 844 },
  { name: 'tablet-768', width: 768, height: 1024 },
  { name: 'desktop-1280', width: 1280, height: 900 },
];

const SECTIONS = ['profile', 'appearance', 'mailbox', 'model', 'search', 'reports'];
const failures = [];
const notes = [];

function check(condition, message) {
  if (!condition) failures.push(message);
}

async function auditOverflow(page, label) {
  const result = await page.evaluate(() => {
    const doc = document.documentElement;
    const overflow = doc.scrollWidth - doc.clientWidth;
    const offenders = [];
    document.querySelectorAll('body *').forEach((node) => {
      const box = node.getBoundingClientRect();
      if (box.width === 0 && box.height === 0) return;
      if (box.right > doc.clientWidth + 1 || box.left < -1) {
        offenders.push(`${node.tagName.toLowerCase()}#${node.id || ''}.${(node.className || '').toString().slice(0, 40)} right=${Math.round(box.right)}`);
      }
      if (node.scrollWidth > node.clientWidth + 2 && getComputedStyle(node).overflowX === 'visible'
          && node.tagName !== 'HTML' && node.tagName !== 'BODY') {
        offenders.push(`text-overflow ${node.tagName.toLowerCase()}#${node.id || ''} scroll=${node.scrollWidth} client=${node.clientWidth}`);
      }
    });
    return { overflow, offenders: offenders.slice(0, 6), clientWidth: doc.clientWidth };
  });
  check(result.overflow <= 1, `${label}: horizontal overflow of ${result.overflow}px (viewport ${result.clientWidth}px)`);
  result.offenders.forEach((item) => failures.push(`${label}: ${item}`));
  return result;
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  const consoleErrors = [];

  for (const viewport of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: 1,
      isMobile: viewport.width < 700,
    });
    const page = await context.newPage();
    page.on('console', (message) => {
      // The pre-login /api/me probe is expected to answer 401.
      const text = message.text();
      if (message.type() === 'error' && !/401 \(Unauthorized\)/.test(text)) {
        consoleErrors.push(`${viewport.name}: ${text}`);
      }
    });
    page.on('pageerror', (error) => consoleErrors.push(`${viewport.name}: pageerror ${error.message}`));

    await page.goto(BASE + '/app', { waitUntil: 'networkidle' });
    await page.fill('#auth-email', EMAIL);
    await page.fill('#auth-password', PASSWORD);
    await page.click('#login');
    await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
    await page.waitForSelector('#hero h2', { timeout: 15000 });
    await page.waitForTimeout(400);

    const hero = await page.textContent('#hero h2');
    notes.push(`${viewport.name}: next step = ${hero}`);
    check(!!hero && hero.trim().length > 0, `${viewport.name}: hero has no next step`);

    const chipCount = await page.locator('#channels .channel').count();
    check(chipCount === 4, `${viewport.name}: expected 4 status chips, found ${chipCount}`);
    const metricCount = await page.locator('#metrics div').count();
    check(metricCount === 4, `${viewport.name}: expected 4 metrics, found ${metricCount}`);

    await auditOverflow(page, `${viewport.name}/home`);
    await page.screenshot({ path: path.join(SHOTS, `${viewport.name}-home.png`), fullPage: true });

    for (const section of SECTIONS) {
      await goTo(page, section);
      await page.waitForSelector(`#section-${section}:not(.hidden)`, { timeout: 10000 });
      await page.waitForTimeout(150);
      await auditOverflow(page, `${viewport.name}/${section}`);
      await page.screenshot({ path: path.join(SHOTS, `${viewport.name}-${section}.png`), fullPage: true });
    }

    // Long, unbroken content must still not force a horizontal scrollbar.
    if (viewport.name === 'mobile-390') {
      await goTo(page, 'reports');
      await page.waitForSelector('#section-reports:not(.hidden)');
      await page.waitForTimeout(300);
      await auditOverflow(page, `${viewport.name}/reports-content`);
      // Report bodies are collapsed by default now, so the summary must be
      // free of body text and the body must appear once it is opened. Asserting
      // on the collapsed text alone would pass even if expanding did nothing.
      const firstSummary = page.locator('#reports-list .report-item > summary').first();
      const summaryOnly = await firstSummary.innerText();
      check(!/重要程度|邮件内容总结/.test(summaryOnly),
        `${viewport.name}: 折叠时摘要里不该有正文`, summaryOnly.slice(0, 60));
      await firstSummary.click();
      await page.waitForTimeout(400);
      const firstReport = await page.locator('#reports-list .report-item').first().innerText();
      check(/重要程度|邮件内容总结|联网搜索/.test(firstReport),
        `${viewport.name}: 展开后正文渲染出了小节`);
      notes.push(`${viewport.name}: first report rendered ${firstReport.length} chars`);
    }

    await context.close();
  }

  // Email HTML previews: they must not scroll sideways on a narrow phone either.
  const previewContext = await browser.newContext({ viewport: { width: 360, height: 800 } });
  const previewPage = await previewContext.newPage();
  for (const file of ['report-preview.html', 'report-preview-long.html']) {
    const target = 'file://' + path.join(path.dirname(SHOTS), file);
    if (!fs.existsSync(path.join(path.dirname(SHOTS), file))) {
      notes.push(`preview ${file} not generated yet`);
      continue;
    }
    await previewPage.goto(target, { waitUntil: 'load' });
    await previewPage.waitForTimeout(200);
    const result = await previewPage.evaluate(() => {
      const doc = document.documentElement;
      const wide = [];
      document.querySelectorAll('table,div,p,li,h1,h2,span').forEach((node) => {
        const box = node.getBoundingClientRect();
        if (box.right > doc.clientWidth + 1) wide.push(`${node.tagName} right=${Math.round(box.right)}`);
      });
      return { overflow: doc.scrollWidth - doc.clientWidth, wide: wide.slice(0, 5) };
    });
    check(result.overflow <= 1, `${file}: email HTML overflows by ${result.overflow}px at 360px`);
    result.wide.forEach((item) => failures.push(`${file}: ${item}`));
    await previewPage.screenshot({ path: path.join(SHOTS, `email-${file.replace('.html', '')}-360.png`), fullPage: true });
  }
  await previewContext.close();

  consoleErrors.forEach((item) => failures.push(`console: ${item}`));
  await browser.close();

  console.log('--- notes ---');
  notes.forEach((item) => console.log('  ' + item));
  if (failures.length) {
    console.log('--- FAILURES ---');
    failures.forEach((item) => console.log('  ' + item));
    process.exit(1);
  }
  console.log('browser checks passed for ' + VIEWPORTS.length + ' viewports');
})().catch((error) => {
  console.error('browser check crashed:', error);
  process.exit(2);
});
