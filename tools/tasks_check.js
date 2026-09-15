/**
 * Browser check for the daily task list: tick one off, get it back.
 *
 *   PILOT_ADMIN=boss@example.com PILOT_INVITE=<code> \
 *     node tools/tasks_check.js <base-url> <screenshot-dir>
 *
 * The server-side suite already proves hiding deletes nothing. What only a real
 * browser can show is that the feature is reachable and honest on a phone:
 * the tick button exists and is big enough to hit, the item really leaves the
 * list without a page reload, the "handled" panel reports the right count, and
 * restoring puts the *same* text back rather than a lookalike.
 *
 * Needs a seeded account: the running server must already have one immediate
 * report for today. `--seed` prints how; see AGENTS.md §5.
 */
'use strict';

const fs = require('fs');
const { chromium } = require('/tmp/pw/node_modules/playwright');

const BASE = process.argv[2] || 'http://127.0.0.1:8915';
const SHOTS = process.argv[3] || '/tmp/tasks-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

const taskTexts = (page) => page.$$eval('#tasks li .task-action', (nodes) => nodes.map((n) => n.textContent));
const doneTexts = (page) => page.$$eval('#tasks-done li .task-action', (nodes) => nodes.map((n) => n.textContent));

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  // A phone is the case that matters: this card is the first thing a student
  // opens, and a tick button that wraps off-screen is the same as not having one.
  const context = await browser.newContext({ viewport: { width: 360, height: 800 },
                                             isMobile: true, hasTouch: true });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));

  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN_EMAIL);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  check(true, '登录成功');

  await page.waitForSelector('#tasks li', { timeout: 10000 });
  const before = await taskTexts(page);
  check(before.length > 0, '今天有任务可处理', `${before.length} 条`);
  const first = before[0];

  // -- the affordance ------------------------------------------------------
  const button = page.locator('#tasks li button').first();
  check(await button.count() === 1, '每条任务都有操作按钮');
  check((await button.innerText()).includes('处理好了'), '按钮文案是「处理好了」', await button.innerText());
  const box = await button.boundingBox();
  check(box && box.height >= 32, '按钮够大好点', box ? `${Math.round(box.height)}px` : 'no box');
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(overflow <= 1, '360px 无横向溢出', `溢出 ${overflow}px`);
  await page.screenshot({ path: `${SHOTS}/tasks-before-360.png`, fullPage: false });

  // -- tick it off ---------------------------------------------------------
  await button.click();
  await page.waitForFunction(
    (text) => !Array.from(document.querySelectorAll('#tasks li .task-action'))
      .some((node) => node.textContent === text),
    first, { timeout: 10000 });
  const after = await taskTexts(page);
  check(!after.includes(first), '处理过的任务从列表消失');
  check(after.length === before.length - 1, '只消失了一条', `${before.length} → ${after.length}`);

  // No full reload: the rest of the page keeps its state.
  check(await page.locator('#dashboard:not(.hidden)').count() === 1, '没有整页刷新');

  // -- it is findable, with the same wording -------------------------------
  const restored = await doneTexts(page);
  check(restored.includes(first), '它出现在「已处理」里', restored.join(' | ').slice(0, 80));
  check((await page.innerText('#tasks-done-note')).trim().length > 0, '「已处理」有计数',
    await page.innerText('#tasks-done-note'));

  const counter = await page.innerText('#metrics');
  check(counter.includes(`需要行动`) && counter.includes(`${after.length} 件`),
    '「需要行动」计数跟着更新', counter.replace(/\s+/g, ' ').slice(0, 60));

  // The panel has to be openable to be useful.
  await page.click('#panel-tasks-done > summary');
  await page.waitForTimeout(200);
  await page.screenshot({ path: `${SHOTS}/tasks-done-360.png`, fullPage: false });

  // -- put it back ---------------------------------------------------------
  const restore = page.locator('#tasks-done li button').first();
  check((await restore.innerText()).includes('恢复'), '「已处理」里是「恢复」按钮', await restore.innerText());
  await restore.click();
  await page.waitForFunction(
    (text) => Array.from(document.querySelectorAll('#tasks li .task-action'))
      .some((node) => node.textContent === text),
    first, { timeout: 10000 });
  const back = await taskTexts(page);
  check(back[0] === first, '恢复后回到原位置且文字未变', back[0]);
  check(back.length === before.length, '数量回到最初', `${back.length}`);
  check((await doneTexts(page)).length === 0, '「已处理」已清空');

  // -- one more, then look back by day -------------------------------------
  await page.locator('#tasks li button').first().click();
  await page.waitForTimeout(800);
  await page.click('#panel-tasks-history > summary');
  await page.waitForTimeout(200);
  const days = await page.$$eval('#tasks-history .history-day', (nodes) => nodes.map((n) => n.textContent));
  check(days.length >= 2, '「按天回看」列出了多天的记录', days.join(' | '));
  check(/已处理\s*\d+\/\d+/.test(days[0] || ''), '每天显示处理进度', days[0]);
  await page.screenshot({ path: `${SHOTS}/tasks-history-360.png`, fullPage: false });

  // Click the *older* day: clicking today would prove nothing, since the view
  // is already there.
  const todayLabel = await page.innerText('#task-day-label');
  const older = page.locator('#tasks-history .history-day').last();
  const olderText = await older.innerText();
  check(!todayLabel.includes(olderText.trim().split('\n')[0]), '更早的那天确实不是今天', olderText);
  await older.click();
  await page.waitForTimeout(900);
  check(await page.locator('#task-back-today:not(.hidden)').count() === 1, '非今天时出现「回到今天」');
  const label = await page.innerText('#task-day-label');
  check(!label.includes('今天 ·'), '日期标签切到了那一天', label);
  // The handled task from that day must be visible there, which is the whole
  // point of the archive.
  const olderDone = await doneTexts(page);
  check(olderDone.length >= 1, '那一天处理过的任务能看见', olderDone.join(' | ').slice(0, 80));
  await page.screenshot({ path: `${SHOTS}/tasks-pastday-360.png`, fullPage: false });

  await page.click('#task-back-today');
  await page.waitForTimeout(900);
  check((await page.innerText('#task-day-label')).includes('今天'), '能回到今天',
    await page.innerText('#task-day-label'));
  check(await page.locator('#task-back-today.hidden').count() === 1, '回到今天后按钮隐藏');

  check(pageErrors.length === 0, '没有 JS 异常', pageErrors.join(' | '));

  await browser.close();
  console.log(`\n${failures.length ? 'FAILED' : 'ALL TASK CHECKS PASSED'}`);
  if (failures.length) { failures.forEach((f) => console.log(' - ' + f)); process.exit(1); }
})().catch((error) => { console.error(error); process.exit(1); });
