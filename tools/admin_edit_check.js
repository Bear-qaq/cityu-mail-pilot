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
const { chromium } = require('./pw');
const { goTo, navHas, openPanel, mintInvite } = require('./nav');

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
  // Read the health line now: a later step saves a setting, which replaces this
  // same status element with 「已修改 …的设置。」. The first run of this check
  // asserted on the overwritten text and reported three failures that were
  // really one missing snapshot.
  const healthStatus = (await page.textContent('#admin-status')) || '';
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

  // -- the health card must not call a broken mailbox "running" -----------
  // Reported from production: 「有一个用户的 imap 授权码都没有填对，为什么后台
  // 显示他正在跑」. `last_polled_at` is written on failure too, so one figure
  // could not tell "we are polling it" from "it works"; the seed carries a
  // mailbox that is polled every few minutes and can never log in.
  const health = await page.locator('#admin-health').innerText();
  check(/轮询在跑/.test(health) && /收信正常/.test(health),
    '健康卡把「轮询在跑」和「收信正常」分成两个数', health.replace(/\n/g, ' '));
  const polled = Number((health.match(/轮询在跑[\s\S]{0,40}?(\d+)\s*\//) || [])[1]);
  const healthy = Number((health.match(/收信正常[\s\S]{0,40}?(\d+)\s*\//) || [])[1]);
  check(Number.isFinite(polled) && Number.isFinite(healthy),
    '两个数都读得出来', `polled=${polled} healthy=${healthy}`);
  check(healthy < polled,
    '轮询得到但登不进去的邮箱，不能算进「收信正常」', `${healthy} < ${polled}`);
  const status = healthStatus;
  check(/登不进去|授权码/.test(status),
    '状态行点名了登不进去的账号，而不是只说一句一切正常', status);
  check(/wrongcode@example\.com/.test(status),
    '状态行写出了是哪个邮箱', status);

  // -- a suspended model key is visible, and does not hide the mailbox ----
  // The breaker (open-items item 8) stops generating for an account whose key
  // keeps being refused. The symptom on the user's side is *silence*, so the one
  // place it can be seen is here. Both this and the broken mailbox are on the
  // same card, and the card used to be an if/else chain -- so the second
  // assertion below is really about that: adding a louder warning must not
  // silence the one underneath it.
  check(/模型 key/.test(status) && /暂停/.test(status),
    '健康卡说了有账号的模型 key 被拒绝、已暂停生成', status.slice(0, 240));
  check(/stalled@example.com/.test(status),
    '状态行点名了是哪个账号被暂停', status);
  check(/队列里|还在队列/.test(status),
    '说清了邮件没有丢，只是排队等一把能用的 key', status);
  check(/wrongcode@example\.com/.test(status),
    '两条警告同时在，新的没有把旧的顶掉', status);
  await page.screenshot({ path: path.join(SHOTS, 'admin-health.png') });

  // -- one-click reminders for the accounts that never finished ------------
  // The console could already *show* who is stuck; this is the part where the
  // stuck person finds out. Two things are easy to get wrong and both are
  // checked here: *which sentence* each account gets (sending "go turn on IMAP"
  // to somebody who already did is worse than sending nothing), and that this
  // button cannot mail anybody twice by accident.
  const reminderPanel = page.locator('#panel-reminders');
  await reminderPanel.locator('> summary').first().click();
  await page.waitForFunction(() => {
    const node = document.querySelector('#panel-reminders-note');
    return node && /个卡住/.test(node.textContent || '');
  }, null, { timeout: 10000 });
  const reminderNote = await page.locator('#panel-reminders-note').innerText();
  check(/\d+ 个卡住 · \d+ 个还没提醒过/.test(reminderNote),
    '面板说出了有几个卡住、几个还没提醒过', reminderNote);
  const reminderRows = await page.locator('#reminders-rows').innerText();
  check(/从没配过私人邮箱/.test(reminderRows), '一个账号被判为「从没配邮箱」', reminderRows.slice(0, 120));
  check(/登不进去/.test(reminderRows), '一个账号被判为「配了邮箱但登不进去」', reminderRows.slice(0, 160));
  check(/stalled@example\.com/.test(reminderRows) && /stalledcode@example\.com/.test(reminderRows),
    '两个夹具都在名单里', reminderRows.slice(0, 200));
  check(!/wrongcode@example\.com/.test(reminderRows),
    '刚注册的账号不会被当成卡住（它还没到 6 小时门槛）');
  await page.locator('#reminders-preview-box > summary').click();
  const reminderPreview = await page.locator('#reminders-preview').innerText();
  check(/还差一步/.test(reminderPreview) && /登录被拒绝/.test(reminderPreview),
    '两封信都能在按之前先看一遍', reminderPreview.slice(0, 120));
  check(/没有配微信联系方式/.test(reminderPreview),
    '这台机器没配微信，面板明说了（所以邮件里不会出现那一行）', reminderPreview.slice(0, 120));
  const sendLabel = await page.locator('#reminders-send').innerText();
  check(/\d/.test(sendLabel), '按钮上写着要发几封，而不是一个光秃秃的「发送」', sendLabel);
  await page.screenshot({ path: path.join(SHOTS, 'admin-reminders.png') });

  // Press it. This server has no operator mailbox, so every send fails -- which
  // is a real exercise of the whole path with nothing leaving the machine. The
  // failures must NOT be recorded as delivered: recording before the send is the
  // one way to actually lose a person, because the record would then say they
  // had been told.
  page.once('dialog', (dialog) => dialog.accept());
  await page.locator('#reminders-send').click();
  await page.waitForFunction(() => {
    const node = document.querySelector('#reminders-status');
    return node && /发出|失败/.test(node.textContent || '');
  }, null, { timeout: 20000 });
  const reminderResult = await page.locator('#reminders-status').innerText();
  check(/失败/.test(reminderResult), '发不出去时如实说失败，而不是报成功', reminderResult);
  const remindersAfter = await page.locator('#reminders-rows').innerText();
  check(/还没提醒过/.test(remindersAfter),
    '失败的没有被记成「已提醒」', remindersAfter.slice(0, 160));
  check(!/已在/.test(remindersAfter), '失败的账号没有被盖章', remindersAfter.slice(0, 160));
  await page.screenshot({ path: path.join(SHOTS, 'admin-reminders-failed.png') });

  // -- the account's own "what did I use" panel ---------------------------
  // It lives in the reports section, not the console, but it is checked here
  // because this is the one suite that runs with --admin-fixtures, and the
  // fixture is what makes the three payer buckets assertable. The account doing
  // the looking is an ordinary user of that page, so nothing here needs admin.
  await goTo(page, 'reports');
  await page.locator('#panel-usage-mine > summary').click();
  await page.waitForFunction(() => {
    const note = document.querySelector('#myusage-note');
    return note && /次调用/.test(note.textContent || '');
  }, null, { timeout: 10000 });
  const usageNote = await page.locator('#myusage-note').innerText();
  check(/3 次调用/.test(usageNote), '面板说出了调用次数', usageNote);
  const usageBody = await page.locator('#myusage-body').innerText();
  check(/合计 tokens/.test(usageBody), '给出了合计数', usageBody.slice(0, 80));
  check(/谁付的/.test(usageBody), '把「谁付的」单独列出来', usageBody.slice(0, 160));
  check(/平台代付/.test(usageBody) && /你自己的 key/.test(usageBody) && /早期记录/.test(usageBody),
    '三种来源都分开列，而不是混成一个数字', usageBody.slice(0, 260));
  check(/按模型/.test(usageBody), '按模型分列', usageBody.slice(0, 200));
  check(/按天/.test(usageBody), '按天分列', usageBody.slice(0, 200));
  check(/估算/.test(usageBody), '说清楚金额是估算而不是账单', usageBody.slice(-200));
  // Someone else's spending must not be reachable from here even by asking.
  const leak = await page.evaluate(async () => {
    const res = await fetch('/api/usage?user_id=usr_stalled_never');
    const body = await res.json();
    return { status: res.status, calls: (body.totals || {}).calls };
  });
  check(leak.status === 200 && leak.calls === 3,
    '带上别人的 user_id 也只会拿到自己的用量', JSON.stringify(leak));
  await page.screenshot({ path: path.join(SHOTS, 'usage-mine.png') });
  await goTo(page, 'admin');

  // -- the four lights -----------------------------------------------------
  // They are drawn from the server's verdict; what matters here is that all four
  // arrive and that an account with *no* evidence is red. This member was
  // created moments ago in this run and has never polled a mailbox, tested a key
  // or had a report sent, so every light must be red -- a green one here would
  // mean the console is glowing on "configured" rather than on "proved", which
  // is the exact failure this feature exists to avoid.
  const lights = refreshed.locator('.light');
  const lightCount = await lights.count();
  check(lightCount === 4, '每个账号四盏灯都在', String(lightCount));
  const lightText = (await lights.allInnerTexts()).join(' / ');
  check(/没测过|还没出过报告/.test(lightText),
    '红灯写明了是「没测过」，而不是笼统的一句失败', lightText);
  const greenCount = await refreshed.locator('.light.ok').count();
  check(greenCount === 0, '一个从没真正跑过的账号不允许出现绿灯', `${greenCount} 盏绿`);
  const dotColours = await refreshed.locator('.light .dot').evaluateAll(
    (nodes) => nodes.map((node) => getComputedStyle(node).backgroundColor));
  check(dotColours.length === 4 && new Set(dotColours).size === 1,
    '四盏灯都是红的（同一个颜色）', JSON.stringify(dotColours));
  await page.screenshot({ path: path.join(SHOTS, 'admin-lights.png') });

  // -- the operator's note -------------------------------------------------
  const noteBox = refreshed.locator('.adminnote textarea');
  check(await noteBox.count() === 1, '每个账号都有管理员备注框');
  await refreshed.locator('.adminnote').screenshot({ path: path.join(SHOTS, 'admin-note.png') });
  const noteText = `自动化检查备注-${Date.now()}`;
  await noteBox.fill(noteText);
  await refreshed.locator('.adminnote button').click();
  await page.waitForTimeout(1000);
  // Closed and reopened on purpose. Reopening re-renders from the cached admin
  // payload rather than re-fetching, so a note that only ever lived in the
  // textarea would silently revert right here -- and that is precisely the bug
  // the audit list had before v0.49.0.
  await page.locator('#panel-users > summary').click();
  await page.waitForTimeout(300);
  await page.locator('#panel-users > summary').click();
  await page.waitForTimeout(800);
  const noteAfter = await cardFor(page, memberEmail).locator('.adminnote textarea').inputValue();
  check(noteAfter === noteText, '备注保存后切走再回来还在', noteAfter.slice(0, 40));

  await ensurePanel(page, 'panel-audit');
  await page.waitForTimeout(300);
  const audit = await page.locator('#admin-audit').textContent();
  check(audit.includes('admin_user_settings_changed') || audit.includes('修改用户设置') || audit.includes('设置'),
    '审计区出现了这次修改', audit.slice(0, 80));
  await page.screenshot({ path: path.join(SHOTS, 'admin-editor-saved.png') });

  // -- the sentinel panel --------------------------------------------------
  // Seeded with one finding per tier. What matters is that the three channels
  // are visibly different, and that "已知晓" quietens a finding *without*
  // removing it -- a panel where acknowledging made the row vanish would be a
  // delete button wearing another name, and the operator would have no way to
  // go back and look at the thing they silenced.
  await ensurePanel(page, 'panel-alerts');
  await page.waitForTimeout(500);
  const alertNote = await page.innerText('#panel-alerts-note');
  check(/3 条 · 1 条会发邮件/.test(alertNote), '收起行就说清有几条会真的发邮件', alertNote);
  const alertRows = page.locator('#admin-alerts article');
  check(await alertRows.count() === 3, '三档各一行', String(await alertRows.count()));
  const alertsText = await page.innerText('#admin-alerts');
  for (const label of ['立刻发邮件', '每天汇总一封', '只在这里显示']) {
    check(alertsText.includes(label), `行上标出了渠道：${label}`);
  }
  await page.screenshot({ path: path.join(SHOTS, 'admin-alerts.png') });

  const ackButton = alertRows.filter({ hasText: '磁盘空间不足' })
    .locator('button', { hasText: '已知晓' });
  check(await ackButton.count() === 1, '会发邮件的那条有「已知晓」按钮');
  await ackButton.click();
  await page.waitForTimeout(1000);
  check(await page.locator('#admin-alerts article').count() === 3,
    '已知晓之后它仍然在列表里（不是删除）');
  const afterAck = await page.innerText('#panel-alerts-note');
  check(/3 条 · 0 条会发邮件/.test(afterAck), '已知晓之后不再计入「会发邮件」', afterAck);
  check((await page.innerText('#admin-alerts')).includes('已知晓：不再为这条发邮件'),
    '行上写明了它为什么安静');
  await page.screenshot({ path: path.join(SHOTS, 'admin-alerts-acknowledged.png') });

  // -- and now one whose key carries an identifier -------------------------
  // `disk` is the fixture's only colon-less key, and it used to be the only one
  // this check pressed. Every key the console actually meets in production is
  // `mailbox_error:usr_…` or `setup_stalled:usr_…`. `encodeURIComponent` turns
  // that ':' into %3A, and the server did not percent-decode route parameters,
  // so every one of those clicks answered 404 -- for real operators, on every
  // row that mattered, while this suite stayed green.
  //
  // The assertion is on the *response status*, not on the rendered text: the
  // re-render is driven by the same response, but a 404 also leaves the old
  // text up, and "the page looks unchanged" is exactly what 「点了没有用」
  // looked like from the outside.
  const colonButton = alertRows.filter({ hasText: '注册后没配完' })
    .locator('button', { hasText: '已知晓' });
  check(await colonButton.count() === 1, '带标识的巡检项也有「已知晓」按钮');
  const ackStatuses = [];
  const watchAck = (response) => {
    if (response.url().includes('/acknowledge')) ackStatuses.push(response.status());
  };
  page.on('response', watchAck);
  await colonButton.click();
  await page.waitForTimeout(1200);
  page.off('response', watchAck);
  check(ackStatuses.length === 1 && ackStatuses[0] === 200,
    '点带标识的那条「已知晓」真的成功了（不是 404）', JSON.stringify(ackStatuses));
  const ackToast = await page.evaluate(() => Array.from(
    document.querySelectorAll('#toasts .toast')).map((node) => node.textContent).join(' | '));
  check(!/操作失败/.test(ackToast), '没有弹出「操作失败」', ackToast || '（无提示）');
  await page.screenshot({ path: path.join(SHOTS, 'admin-alerts-acknowledged-colon.png') });

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
  check(banner.includes(broadcastTitle), '用户一打开应用就看到广播', banner.slice(0, 80));
  // 盖住整页的对话框：它必须挡住背后的界面，而且只有「确认收到」能关掉它。
  const modalBox = await readerPage.locator('#announcement').boundingBox();
  const viewport = readerPage.viewportSize();
  check(modalBox && viewport && modalBox.width >= viewport.width - 2 && modalBox.height >= viewport.height - 2,
        '广播是盖住整页的对话框，不是首页里的一条横幅',
        JSON.stringify({ modalBox, viewport }));
  check(await readerPage.locator('#announcement').getAttribute('aria-modal') === 'true',
        '对话框标了 aria-modal');
  const cardTone = await readerPage.locator('#announcement-card').getAttribute('class');
  check(/warn/.test(cardTone), '广播按类型着色', cardTone);
  check(await readerPage.locator('#announcement-ack').innerText()
          .then((text) => text.trim() === '确认收到'), '唯一的按钮写着「确认收到」');
  // 没确认就走不掉：ESC 和点空白都不该关掉它。
  await readerPage.keyboard.press('Escape');
  await readerPage.waitForTimeout(200);
  check(await readerPage.locator('#announcement:not(.hidden)').count() === 1, '按 ESC 关不掉（必须点确认）');
  await readerPage.mouse.click(5, 5);
  await readerPage.waitForTimeout(200);
  check(await readerPage.locator('#announcement:not(.hidden)').count() === 1, '点空白也关不掉');
  await readerPage.screenshot({ path: path.join(SHOTS, 'user-broadcast-modal.png') });

  // 点「确认收到」——用的是产品自己的处理函数（事件委托，所以 DOM click 就够）。
  await readerPage.locator('#announcement-ack').evaluate((node) => node.click());
  await readerPage.waitForTimeout(900);

  const ackState = await readerPage.evaluate(() => {
    const button = document.getElementById('announcement-ack');
    const probe = { cls: document.getElementById('announcement').className,
      help: document.getElementById('announcement-help').textContent,
      disabled: button.disabled, hasButton: Boolean(button),
      scriptSrc: Array.from(document.scripts).map((s) => s.src).join(','),
      loadedFully: !document.body.innerText.includes('页面没有完整加载'),
      hasAck: typeof window.acknowledgeAnnouncement };
    return probe;
  });
  check(await readerPage.locator('#announcement.hidden').count() === 1, '点「确认收到」后本用户不再显示',
        JSON.stringify(ackState));

  const otherReader = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const otherPage = await otherReader.newPage();
  check(await signIn(otherPage, ADMIN), '管理员自己也能看到这条广播');
  // The reader path above waits for the banner; this one used to count straight
  // after sign-in. The notice is fetched after the dashboard paints, so on a busy
  // runner the count could run first -- CI failed once with "别的用户仍然看得到"
  // while the product was fine. Wait for the same selector, and let a timeout mean
  // "it never appeared" instead of an unhandled crash.
  let stillThere = 0;
  try {
    await otherPage.waitForSelector('#announcement:not(.hidden)', { timeout: 10000 });
    stillThere = await otherPage.locator('#announcement:not(.hidden)').count();
  } catch (error) {
    stillThere = 0;
  }
  check(stillThere === 1, '别的用户仍然看得到（关闭只对自己生效）', `count=${stillThere}`);
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

  // -- 每日简报的综览开关 ------------------------------------------------
  // The control has to be reachable *and* leave the setting where it started:
  // this panel switches on an extra model call per user per day, and a check
  // that flips it and walks away would silently change what everyone receives.
  await openPanel(page, 'panel-digest');
  await page.waitForTimeout(400);
  const digestButton = page.locator('#digest-toggle');
  check(await digestButton.count() === 1, '后台有「每日简报」面板和综览开关');
  const startedOn = (await digestButton.innerText()).includes('关闭');
  const startedNote = await page.innerText('#panel-digest-note');
  check(/只发清单|综览已开启/.test(startedNote), '面板上说清楚了当前是哪种状态', startedNote);

  await digestButton.click();
  await page.waitForTimeout(500);
  const flipped = (await digestButton.innerText()).includes('关闭') !== startedOn;
  check(flipped, '点一下开关，按钮与状态都跟着变',
        `${startedOn ? '开' : '关'} → ${await page.innerText('#panel-digest-note')}`);

  await digestButton.click();
  await page.waitForTimeout(500);
  const restored = (await digestButton.innerText()).includes('关闭') === startedOn;
  check(restored, '再点一下回到原来的状态（检查不该改变产品行为）');

  const digestState = await page.evaluate(async () => {
    const response = await fetch('/api/admin/digest');
    return { status: response.status, body: await response.json() };
  });
  check(digestState.status === 200 && digestState.body.synthesis === startedOn,
        '服务端的值确实回到了原样', JSON.stringify(digestState.body));

  await context.close();
  await browser.close();
  check(errors.length === 0, '没有 JS 异常 / 资源缺失', errors.slice(0, 3).join(' | '));
  console.log(failures.length ? `\nFAILED (${failures.length}): ${failures.join('; ')}` : '\nALL ADMIN EDIT CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
