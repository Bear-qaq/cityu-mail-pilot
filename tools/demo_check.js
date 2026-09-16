/* Real-browser check for the read-only demo at `/demo`.
 *
 * The demo exists so somebody with no account can see the product. Two things
 * make it worth trusting, and neither is visible in a screenshot:
 *
 *   * it makes **no requests to /api/ at all** — the data is a fixture the page
 *     can answer from, so a later change cannot quietly turn the demo into a
 *     live window onto somebody's account. This check fails if even one API
 *     call happens.
 *   * the parts it cannot show are **marked, not broken**: the other tabs are
 *     disabled rather than opening onto an error, and an action that would
 *     change something says why it cannot.
 *
 *   PILOT_ADMIN=boss@example.com node tools/demo_check.js http://127.0.0.1:8924 /tmp/demo-shots
 */
'use strict';

const fs = require('fs');
const { chromium } = require('/tmp/pw/node_modules/playwright');
const { goTo } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8924';
const SHOTS = process.argv[3] || '/tmp/demo-shots';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  const errors = [];

  // A visitor with no account and no cookies: the whole point of the page.
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  const apiCalls = [];
  page.on('request', (request) => {
    if (request.url().includes('/api/')) apiCalls.push(request.url().replace(BASE, ''));
  });

  const response = await page.goto(`${BASE}/demo`, { waitUntil: 'load' });
  check(response.status() === 200, '/demo 不需要登录就能打开', `HTTP ${response.status()}`);

  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  check(true, '演示里直接进的是首页，不是登录页');
  check(await page.locator('#auth').isHidden(), '登录卡片没有出现');

  const body = await page.innerText('body');
  check(/这是演示/.test(body), '顶部写明这是演示');
  check(/数据是编的/.test(body), '并说明数据是编的');
  const cta = page.locator('#demo-banner .demo-banner-cta');
  check(await cta.count() === 1, '横幅里有一个去申请内测的入口');
  check(await cta.getAttribute('href') === '/#apply', '入口指向申请那一节');

  // The product itself has to be visible, otherwise the demo demonstrates nothing.
  check(/今天要处理的事|今天最重要|需要行动/.test(body), '首页真的渲染出了内容', body.slice(0, 60).replace(/\n/g, ' '));
  const taskRows = await page.locator('#tasks .task, #task-list .task, [data-task-key]').count();
  check(taskRows > 0, '演示里有具体的待办条目', `${taskRows} 条`);

  // The tabs it cannot fill are disabled, not clickable-into-an-error.
  const mailboxTab = page.locator('#sidebar-nav button[data-section="mailbox"], #tabbar button[data-section="mailbox"]').first();
  if (await mailboxTab.count()) {
    check(await mailboxTab.isDisabled(), '演示里没有数据的板块是禁用的（不会点开一个报错页）',
      await mailboxTab.getAttribute('title') || '');
  }
  const dashboardTab = page.locator('#sidebar-nav button[data-section="dashboard"], #tabbar button[data-section="dashboard"]').first();
  check(!(await dashboardTab.isDisabled()), '有数据的板块照常可点');

  // Report list, from the fixture.
  await goTo(page, 'reports');
  await page.waitForTimeout(600);
  const reports = await page.innerText('#section-reports');
  check(/作业截止提醒|图书馆逾期/.test(reports), '报告列表渲染的是夹具里的假报告', reports.slice(0, 50).replace(/\n/g, ' '));

  await page.screenshot({ path: `${SHOTS}/demo-390.png`, fullPage: false });

  // Read-only: an action that would change something says so.
  await goTo(page, 'dashboard');
  await page.waitForTimeout(400);
  const firstTick = page.locator('button', { hasText: '处理好了' }).first();
  if (await firstTick.count()) {
    await firstTick.click();
    await page.waitForTimeout(700);
    const after = await page.innerText('body');
    check(/只读演示|不能修改/.test(after), '点「处理好了」会明说这是只读演示，而不是假装成功',
      (after.match(/[^\n]*只读演示[^\n]*/) || [''])[0].slice(0, 60));
  }

  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  check(overflow.scrollWidth <= overflow.clientWidth + 1, '390px 下没有横向溢出', JSON.stringify(overflow));

  // The property that makes the whole thing safe.
  check(apiCalls.length === 0, '整个过程一次都没有请求 /api/', apiCalls.slice(0, 3).join(' | '));

  await context.close();

  // It has to be findable, which is the opposite of how /app is treated.
  const robots = await (await browser.newContext()).newPage().then(async (p) => {
    const res = await p.goto(`${BASE}/robots.txt`, { waitUntil: 'load' });
    return { status: res.status(), text: await p.innerText('body') };
  });
  check(robots.status === 200 && !/^Disallow: \/demo$/m.test(robots.text),
    'robots.txt 没有把 /demo 挡在搜索引擎外面');

  await browser.close();
  check(errors.length === 0, '没有 JS 异常', errors.slice(0, 3).join(' | '));
  console.log(failures.length
    ? `\nFAILED (${failures.length}): ${failures.join('; ')}`
    : '\nALL DEMO CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
