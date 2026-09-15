/* Real-browser check for the admin "edit another user" form.
 *
 *   PILOT_INVITE=check-invite PILOT_ADMIN=boss@example.com \
 *     node tools/admin_edit_check.js http://127.0.0.1:8912 /tmp/admin-shots
 *
 * Verifies the things a unit test cannot: that the form is actually usable,
 * that a stored secret is never put back into the page, that saving really
 * changes the other account, and that an ordinary user gets nothing.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('/tmp/pw/node_modules/playwright');
const { goTo, navHas, mintInvite } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8912';
const SHOTS = process.argv[3] || '/tmp/admin-shots';
const ADMIN = process.env.PILOT_ADMIN || 'boss@example.com';
const ADMIN_EMAIL = ADMIN;
const PASSWORD = 'a-long-enough-password';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

async function register(browser, page, email) {
  // Mint the invite at run time; see mintInvite in nav.js for why the old
  // pre-seeded pool was a trap.
  const invite = await mintInvite(browser, {
    base: BASE, email: ADMIN_EMAIL, password: PASSWORD, label: `adminedit-${Date.now()}`,
  });
  if (!invite) return false;
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', email);
  await page.fill('#auth-password', PASSWORD);
  await page.fill('#invite', invite);
  await page.check('#accept-terms');
  await page.click('#register');
  try {
    await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 8000 });
    return true;
  } catch (error) {
    // Surface why, instead of a bare false that reads like "the button is broken".
    console.log('  注册未成功:', await page.textContent('#auth-status').catch(() => '(无状态文本)'));
    return false;
  }
}

async function signIn(page, email) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', email);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  try {
    await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 5000 });
    return true;
  } catch (error) {
    return register(page, email);
  }
}

function cardFor(page, email) {
  return page.locator('#admin-users article', { hasText: email }).first();
}

async function ensurePanel(page, id) {
  // Open it the way a user would. Setting `open` from script does fire the
  // <details> toggle handler, but it races the lazy load that renders the
  // panel's contents, so a read straight afterwards can see an empty panel.
  // Clicking the summary and waiting for the panel's own content is both more
  // realistic and deterministic.
  const details = page.locator(`#${id}`);
  if (!(await details.count())) return;
  if (!(await details.evaluate((node) => node.open))) {
    await page.locator(`#${id} > summary`).click();
  }
  await page.waitForFunction(
    (panelId) => document.getElementById(panelId).open, id, { timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(400);
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  const stamp = Date.now();
  const memberEmail = `editme-${stamp}@example.com`;

  const memberContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const memberPage = await memberContext.newPage();
  check(await register(browser, memberPage, memberEmail), `先注册一个被管理的用户（${memberEmail}）`);
  await memberContext.close();

  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  const expected = (text) => /^Failed to load resource: the server responded with a status of (400|401|403|404)/.test(text);
  page.on('pageerror', (error) => errors.push(`pageerror: ${error}`));
  page.on('console', (m) => { if (m.type() === 'error' && !expected(m.text())) errors.push(m.text()); });

  check(await signIn(page, ADMIN), `管理员登录（${ADMIN}）`);
  await goTo(page, 'admin');
  await page.waitForSelector('#panel-users', { timeout: 10000 });
  // Give the two aggregate calls behind the summary lines a moment to land.
  await page.waitForFunction(() => {
    const nodes = [...document.querySelectorAll('#section-admin .panel-note')];
    return nodes.length > 0 && nodes.every((node) => node.textContent.trim() && node.textContent.trim() !== '—');
  }, null, { timeout: 10000 }).catch(() => {});

  // -- panels start collapsed ---------------------------------------------
  const panelIds = ['panel-edit', 'panel-users', 'panel-mail', 'panel-usage', 'panel-metrics'];
  const openStates = await page.evaluate((ids) => ids.map((id) => {
    const node = document.getElementById(id);
    return node ? node.open : null;
  }), panelIds);
  check(openStates.every((open) => open === false), '五个面板默认全部收起', JSON.stringify(openStates));

  const summaryText = await page.evaluate((ids) => ids.map((id) => {
    const note = document.querySelector(`#${id} .panel-note`);
    return note ? note.textContent.trim() : '';
  }), panelIds);
  check(summaryText.every((text) => text && text !== '—'), '收起时摘要行已经带着数字', JSON.stringify(summaryText));
  check(summaryText.some((text) => /没发出去/.test(text)), '邮件面板收起时就能看到下发情况', summaryText[2]);
  check(summaryText.some((text) => /tokens/.test(text)), 'token 面板收起时就能看到用量', summaryText[3]);
  const mailNoteTone = await page.evaluate(() => {
    const node = document.querySelector('#panel-mail .panel-note');
    return node ? node.className : '';
  });
  check(/warn|bad/.test(mailNoteTone), '有未下发邮件时摘要行会标色提醒', mailNoteTone);
  await page.locator('#panel-edit').scrollIntoViewIfNeeded();
  await page.waitForTimeout(200);
  await page.screenshot({ path: path.join(SHOTS, 'admin-panels-collapsed.png') });

  // The users panel has to be opened to reach the user list.
  await ensurePanel(page, 'panel-users');
  await page.waitForTimeout(250);
  const card = cardFor(page, memberEmail);
  check(await card.count() === 1, '展开「已注册用户」后能看到用户');

  await ensurePanel(page, 'panel-edit');
  await page.waitForTimeout(250);
  await page.selectOption('#edit-user', { label: `${memberEmail}（active）` });
  await page.waitForTimeout(250);
  const form = page.locator('#admin-editor');
  check(await form.locator('input[type="password"]').count() === 3, '三个密钥输入框存在（模型/搜索/邮箱）');

  const secretValues = await form.locator('input[type="password"]').evaluateAll(
    (nodes) => nodes.map((n) => n.value));
  check(secretValues.every((value) => value === ''),
    '密钥框一律为空，绝不回填已存的密钥', JSON.stringify(secretValues));
  const hints = await form.locator('.help').allTextContents();
  check(hints.some((text) => text.includes('尚未配置')), '未配置的密钥有明确提示');

  const timeFieldBefore = await form.locator('input[type="time"]').inputValue();
  await form.locator('input[type="text"]').nth(1).fill('自动化测试专业');
  await form.locator('input[type="time"]').fill('07:30');
  await page.screenshot({ path: path.join(SHOTS, 'admin-editor-open.png') });

  await form.locator('button', { hasText: '保存修改' }).click();
  // The list is re-rendered from the server response, so wait for THIS user's
  // receipt to be filled in rather than for any .saved element (empty hidden
  // ones exist on every card).
  // The editor lives in its own panel now, so the receipt lands there.
  await page.waitForFunction(() => {
    const status = document.querySelector('#admin-editor .saved');
    return Boolean(status && status.textContent.trim());
  }, null, { timeout: 10000 });
  const saved = await page.locator('#admin-editor .saved').textContent();
  check(/已保存/.test(saved), '保存成功并给出回执', saved.slice(0, 90));
  check(/major/.test(saved) && /daily_time/.test(saved), '回执列出了被修改的字段名', saved.slice(0, 90));

  await page.waitForTimeout(600);
  const refreshed = cardFor(page, memberEmail);
  const grid = await refreshed.locator('.chaingrid').textContent();
  check(grid.includes('自动化测试专业'), '列表已显示新的专业', grid.slice(0, 80));
  check(grid.includes('07:30'), '列表已显示新的简报时间');

  await ensurePanel(page, 'panel-audit');
  await page.waitForTimeout(300);
  const audit = await page.locator('#admin-audit').textContent();
  check(audit.includes('admin_user_settings_changed') || audit.includes('修改用户设置') || audit.includes('设置'),
    '审计区出现了这次修改', audit.slice(0, 80));
  await page.screenshot({ path: path.join(SHOTS, 'admin-editor-saved.png') });

  // -- the mail board ------------------------------------------------------
  await goTo(page, 'admin');
  await ensurePanel(page, 'panel-mail');
  await page.waitForSelector('#mail-counts', { timeout: 10000 });
  await page.waitForSelector('#admin-messages details.mailrow', { timeout: 10000 });
  const counts = await page.locator('#mail-counts').textContent();
  check(/共收到/.test(counts), '邮件板块显示了总数', counts.slice(0, 70));

  const rows = page.locator('#admin-messages details.mailrow');
  const rowCount = await rows.count();
  check(rowCount >= 1, '每一封邮件各占一行', String(rowCount));

  const firstRow = rows.first();
  check(await firstRow.locator('span.mailstate').count() === 1, '每行有明确的下发状态徽章');
  const openedBefore = await firstRow.evaluate((node) => node.open);
  check(openedBefore === false, '默认是收起的，点开才展开');
  const detailVisibleBefore = await firstRow.locator('.maildetail').isVisible().catch(() => false);
  check(detailVisibleBefore === false, '收起时看不到细节');

  await firstRow.locator('summary').click();
  await page.waitForTimeout(200);
  await firstRow.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(SHOTS, 'admin-mailboard-expanded.png') });
  const openedAfter = await firstRow.evaluate((node) => node.open);
  check(openedAfter === true, '点击后展开');
  const detailText = await firstRow.locator('.maildetail').textContent();
  check(/收到的账号/.test(detailText), '展开后显示处理细节', detailText.slice(0, 80));
  check(!/body|正文/.test(detailText.replace('邮件正文', '')), '细节里没有邮件正文');

  const filterOptions = await page.locator('#mail-filter option').allTextContents();
  check(filterOptions.some((text) => text.includes('没发出去')) && filterOptions.some((text) => text.includes('已跳过')),
    '可以按下发情况筛选', filterOptions.join('/'));

  await page.selectOption('#mail-filter', 'skipped');
  await page.waitForTimeout(800);
  const skippedRows = await page.locator('#admin-messages details.mailrow').count();
  const skippedStates = await page.locator('#admin-messages span.mailstate').allTextContents();
  check(skippedStates.every((text) => text.trim() === '已跳过'),
    `筛选「已跳过」后只显示跳过的（${skippedRows} 行）`, skippedStates.join(','));

  await page.selectOption('#mail-filter', 'all');
  await page.waitForTimeout(800);
  await page.locator('#mail-counts').scrollIntoViewIfNeeded();
  await page.waitForTimeout(200);
  await page.screenshot({ path: path.join(SHOTS, 'admin-mailboard.png') });

  // -- token usage and cost ------------------------------------------------
  await goTo(page, 'admin');
  await ensurePanel(page, 'panel-usage');
  await page.waitForSelector('#admin-usage details.userow', { timeout: 10000 });
  const usageTotals = await page.locator('#usage-totals').textContent();
  check(/期内调用/.test(usageTotals) && /期内花费/.test(usageTotals),
    'token 板块显示调用次数与花费', usageTotals.slice(0, 80));

  const usageRows = page.locator('#admin-usage details.userow');
  const usageCount = await usageRows.count();
  check(usageCount >= 1, '每个用户一行', String(usageCount));
  const usageFirst = usageRows.first();
  check(await usageFirst.evaluate((node) => node.open) === false, 'token 行默认收起');
  check(await usageFirst.locator('.usebreak').isVisible().catch(() => false) === false, '收起时看不到明细');

  const costText = await usageFirst.locator('.cost').textContent();
  check(/\$/.test(costText), '未展开就能看到花费', costText);

  await usageFirst.locator('summary').click();
  await page.waitForTimeout(250);
  const breakdown = await usageFirst.locator('.usebreak').textContent();
  check(/按模型/.test(breakdown) && /按天/.test(breakdown), '展开后按模型与按天明细都在');
  check(/deepseek/.test(breakdown), '明细里能看到模型名');
  check(/命中缓存/.test(breakdown), '明细里区分了缓存命中', breakdown.slice(0, 100));
  await usageFirst.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(SHOTS, 'admin-usage.png') });
  await usageFirst.locator('summary').click();

  const priceEditor = page.locator('#usage-price-editor details.advanced');
  await priceEditor.locator('summary').click();
  await page.waitForTimeout(250);
  const priceText = await priceEditor.textContent();
  check(/内置价目/.test(priceText) && /deepseek/.test(priceText), '价格编辑器列了内置价目');
  check(/缓存未命中/.test(priceText), '价格编辑器说明了单位与字段');
  await priceEditor.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(SHOTS, 'admin-usage-prices.png') });

  // -- broadcast -----------------------------------------------------------
  await goTo(page, 'admin');
  await ensurePanel(page, 'panel-broadcast');
  await page.waitForTimeout(200);
  check(await page.locator('#broadcast-publish').isVisible(), '广播面板可以用');
  const deliveryOptions = await page.locator('#broadcast-delivery option').allTextContents();
  check(deliveryOptions.some((text) => text.includes('仅站内')) && deliveryOptions.some((text) => text.includes('邮件')),
    '可以选择仅站内或同时发邮件', deliveryOptions.join(' / '));

  // Make the check re-runnable: withdraw anything left active by an earlier
  // run, so "nobody sees it after withdrawing" tests this run's broadcast and
  // not a leftover from the previous one.
  for (let guard = 0; guard < 10; guard += 1) {
    const live = page.locator('#admin-announcements article', { hasText: '正在显示' });
    if (await live.count() === 0) break;
    page.once('dialog', (dialog) => dialog.accept());
    await live.first().locator('button', { hasText: '撤下' }).click();
    await page.waitForTimeout(700);
  }
  check(await page.locator('#admin-announcements article', { hasText: '正在显示' }).count() === 0,
    '开跑前把历史遗留的生效公告都撤下（可重复运行）');

  const broadcastTitle = `自动化测试公告 ${stamp}`;
  await page.fill('#broadcast-title', broadcastTitle);
  await page.fill('#broadcast-body', '这是一条自动化测试公告：只发站内，不发邮件。');
  await page.selectOption('#broadcast-tone', 'warn');
  await page.selectOption('#broadcast-delivery', 'banner');
  await page.click('#broadcast-publish');
  await page.waitForFunction(() => {
    const status = document.getElementById('broadcast-status');
    return Boolean(status && status.textContent.trim());
  }, null, { timeout: 10000 });
  const publishStatus = await page.locator('#broadcast-status').textContent();
  check(/已发布/.test(publishStatus), '发布有明确回执', publishStatus.slice(0, 60));
  check(/仅站内/.test(publishStatus), '回执说明了发送范围', publishStatus.slice(0, 60));
  const history = await page.locator('#admin-announcements').textContent();
  check(history.includes(broadcastTitle), '历史里能看到刚发的公告');
  check(/仅站内广播，没有发邮件/.test(history), '没有选邮件时明确标注未发邮件');
  await page.screenshot({ path: path.join(SHOTS, 'admin-broadcast.png') });

  // A separate browser must see it as the first thing on the home screen.
  const readerContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const readerPage = await readerContext.newPage();
  check(await signIn(readerPage, memberEmail), '被广播的用户登录');
  await readerPage.waitForSelector('#announcement:not(.hidden)', { timeout: 10000 });
  const banner = await readerPage.locator('#announcement').textContent();
  check(banner.includes(broadcastTitle), '用户一进首页就看到广播', banner.slice(0, 80));
  const bannerTone = await readerPage.locator('#announcement').getAttribute('class');
  check(/warn/.test(bannerTone), '广播按类型着色', bannerTone);
  const bannerBox = await readerPage.locator('#announcement').boundingBox();
  const heroBox = await readerPage.locator('#hero').boundingBox();
  check(bannerBox && heroBox && bannerBox.y < heroBox.y, '广播排在首页最上方（在「你的下一步」之前）');
  await readerPage.screenshot({ path: path.join(SHOTS, 'user-broadcast-banner.png') });

  // Only one broadcast at a time, and dismissing is per user.
  await readerPage.click('#announcement button');
  await readerPage.waitForTimeout(600);
  check(await readerPage.locator('#announcement.hidden').count() === 1, '点「我知道了」后本用户不再显示');

  const otherReader = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const otherPage = await otherReader.newPage();
  check(await signIn(otherPage, ADMIN), '管理员自己也能看到这条广播');
  const stillThere = await otherPage.locator('#announcement:not(.hidden)').count();
  check(stillThere === 1, '别的用户仍然看得到（关闭只对自己生效）');
  await otherReader.close();
  await readerContext.close();

  // Withdraw, and nobody sees it any more.
  await ensurePanel(page, 'panel-broadcast');
  await page.waitForTimeout(200);
  page.once('dialog', (dialog) => dialog.accept());
  await page.locator('#admin-announcements article', { hasText: broadcastTitle }).locator('button', { hasText: '撤下' }).click();
  await page.waitForTimeout(900);
  const afterWithdraw = await page.locator('#admin-announcements').textContent();
  check(/已撤下/.test(afterWithdraw), '撤下后历史里标注已撤下', afterWithdraw.slice(0, 60));

  const goneContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const gonePage = await goneContext.newPage();
  await signIn(gonePage, memberEmail);
  await gonePage.waitForSelector('#dashboard:not(.hidden)', { timeout: 10000 });
  await gonePage.waitForTimeout(500);
  const goneState = await gonePage.evaluate(() => {
    const node = document.getElementById('announcement');
    return { cls: node ? node.className : '(缺失)', text: node ? node.textContent.slice(0, 60) : '' };
  });
  check(await gonePage.locator('#announcement.hidden').count() === 1,
    '撤下后所有用户都不再看到', JSON.stringify(goneState));
  await goneContext.close();

  // The endpoint itself must refuse an ordinary account, not just hide the form.
  const member2 = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const member2Page = await member2.newPage();
  await signIn(member2Page, memberEmail);
  const probe = await member2Page.evaluate(async () => {
    const me = await (await fetch('/api/me')).json();
    const mails = await fetch('/api/admin/messages');
    const usage = await fetch('/api/admin/usage');
    const broadcast = await fetch('/api/admin/announcements', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'x', body: 'y' }),
    });
    const res = await fetch('/api/admin/users/whatever/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ major: 'hacked' }),
    });
    return { isAdmin: me.is_admin, status: res.status, mails: mails.status, usage: usage.status,
             broadcast: broadcast.status, tab: document.querySelectorAll('#sidebar-nav button[data-section="admin"]').length };
  });
  check(probe.isAdmin === false && probe.status === 404 && probe.mails === 404
    && probe.usage === 404 && probe.broadcast === 404 && probe.tab === 0,
    '普通用户既看不到入口、也调不动接口', JSON.stringify(probe));
  await member2.close();

  await context.close();
  await browser.close();
  check(errors.length === 0, '没有 JS 异常 / 资源缺失', errors.slice(0, 3).join(' | '));
  console.log(failures.length ? `\nFAILED (${failures.length}): ${failures.join('; ')}` : '\nALL ADMIN EDIT CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
