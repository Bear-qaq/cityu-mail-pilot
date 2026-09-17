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

/* 截图只是存档，不该把套件判红。
 *
 * CI 上真实红过一次：`locator.screenshot: Element is not attached to the DOM` ——
 * 面板背后有轮询，取景框和快门之间隔了一次重渲染，元素就没了。产品是好的。
 * 所以：重试一次；还不行就整页截一张。**失败的是存档，不是断言。**
 */
async function elementShot(page, scope, selector, name) {
  const target = path.join(SHOTS, name);
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await scope.locator(selector).first().screenshot({ path: target });
      return true;
    } catch (error) {
      await page.waitForTimeout(250);
    }
  }
  try {
    await page.screenshot({ path: target });
    console.log(`  note  元素截图没成功（重绘），改整页存档：${name}`);
  } catch (error) {
    console.log(`  note  连整页截图都失败了：${name} — ${String(error).split('\n')[0]}`);
  }
  return false;
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

// 账号卡默认是收起的（v0.63.56），要按里面的按钮就得先像人一样点开它。
async function expandCard(page, email) {
  const details = cardFor(page, email).locator('details.admin-user-box');
  if (!(await details.evaluate((node) => node.open))) {
    await details.locator('> summary').click();
    await page.waitForTimeout(150);
  }
  return details;
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

  // -- 账号卡默认收起（用户原话：「一点开全部展开了，显得太杂乱了」）---------
  //
  // 这一段必须在**别处还没有展开过任何卡**的时候跑：展开状态是记在内存里的
  // （刷新之后不塌），所以中途再断言「一张都没开」只会测到测试自己的动作。
  await page.waitForSelector('#admin-users article.admin-user', { timeout: 8000 });
  const openCount = await page.locator('#admin-users details.admin-user-box[open]').count();
  check(openCount === 0, '一打开面板，所有账号卡都是收起的', `${openCount} 张展开`);
  await page.screenshot({ path: path.join(SHOTS, 'admin-users-collapsed.png') });
  const collapsedText = await page.locator('#admin-users').innerText();
  check(/收信/.test(collapsedText) && /出报告/.test(collapsedText),
        '收起时仍然看得见那四盏灯（藏起来就等于要他一一点开找）', collapsedText.slice(0, 100));
  // 判据不能用「转发邮箱」这种**导语里也有的词**（第一版就是这么错的）：
  // 只在展开区里的是那些格子的**标签组合**。
  check(!/只读验证/.test(collapsedText) && !/每日简报/.test(collapsedText),
        '收起时看不到 12 格资料（展开后才有的那部分）', collapsedText.slice(0, 100));

  // 勾选框**按真实坐标点**：它若在 <summary> 里，这一下会顺手把卡展开，而这个
  // 项目已经两次栽在「点击落到祖先元素」上（公告按钮、换邮箱按钮）。
  const firstCard = page.locator('#admin-users article.admin-user').first();
  const firstBox = firstCard.locator('details.admin-user-box');
  const memberPick = firstCard.locator('input.user-pick');
  // 真人点之前会先滚到那里 —— `locator.click()` 自带这一步，裸的 `mouse.click`
  // 没有，所以坐标必须自己先滚进视口，否则点的是屏幕外的空气。
  await memberPick.scrollIntoViewIfNeeded();
  await page.waitForTimeout(150);
  const pickBox = await memberPick.boundingBox();
  await page.mouse.click(pickBox.x + pickBox.width / 2, pickBox.y + pickBox.height / 2);
  await page.waitForTimeout(150);
  check(await memberPick.isChecked(), '点勾选框本身：勾上了');
  check(!(await firstBox.evaluate((node) => node.open)),
        '点勾选框**不会**顺手把这一行展开（它故意放在 summary 外面）');
  await page.mouse.click(pickBox.x + pickBox.width / 2, pickBox.y + pickBox.height / 2);
  await page.waitForTimeout(150);
  check(!(await memberPick.isChecked()), '再点一下取消勾选');

  await firstBox.locator('> summary').click();
  await page.waitForTimeout(200);
  check(await firstBox.evaluate((node) => node.open), '点一下 summary 就展开');
  check(/只读验证/.test(await firstCard.innerText()), '展开后才出现 12 格资料');
  const cardCount = await page.locator('#admin-users article.admin-user').count();
  if (cardCount > 1) {
    const secondBox = page.locator('#admin-users details.admin-user-box').nth(1);
    await secondBox.locator('> summary').click();
    await page.waitForTimeout(200);
    check(await secondBox.evaluate((node) => node.open)
      && !(await firstBox.evaluate((node) => node.open)),
      '展开另一个时，上一个自动收起（一次只摊开一张）');
    await secondBox.locator('> summary').click();
    await page.waitForTimeout(150);
  }
  await firstBox.locator('> summary').click();     // 收回原状，别影响后面
  await page.waitForTimeout(150);
  // -- 同一句话不要印两遍（2026-09-16 生产截图）-------------------------------
  //
  // 一次失败的「只读验证」会把同一句话同时写进 `last_error` 与
  // `last_verify_error`（两个列记的是两件事：上次轮询、上次显式测试），而面板
  // 原来把它们 join 起来直接印——于是同一个故障看起来像两个，运营者会开始怀疑
  // 这个面板到底读的是哪一份数据。夹具（seed_preview 的 wrongcode）两列都写了，
  // 就是为了让这条断言真的能红。
  const wrongCard = cardFor(page, 'wrongcode@example.com');
  check(await wrongCard.count() === 1, '夹具里那个授权码错的账号在列表里');
  await expandCard(page, 'wrongcode@example.com');   // 卡片收起时，错误行在展开区里
  const wrongCaution = await wrongCard.locator('.caution').first().innerText();
  const occurrences = wrongCaution.split('LOGIN Login error or password error').length - 1;
  check(occurrences === 1, '同一条错误只印一次，不因为写进了两列就变成两条',
    wrongCaution.slice(0, 120));

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

  // 这一段同时是那个「回执消失」bug 的回归测试：页面一加载就会把**每个**面板都画一遍
  // （v0.63.68 起「刷新全部」刷全部，含 `PANEL_LOADED` 全部置位），而保存设置的响应只带
  // `users` + `audit`——`renderAdminInvites(undefined)` 会在半路抛异常，把回执吃掉。
  // 以前要「先展开邀请码面板再改人」才撞得上，现在必然撞上，所以这里必须绿。
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
  await expandCard(page, memberEmail);          // 卡片默认收起，资料在展开区里
  const grid = await refreshed.locator('.chaingrid').textContent();
  check(grid.includes('自动化测试专业'), '列表已显示新的专业', grid.slice(0, 80));
  check(grid.includes('07:30'), '列表已显示新的简报时间');

  // -- the health card must not call a broken mailbox "running" -----------
  // Reported from production: 「有一个用户的 imap 授权码都没有填对，为什么后台
  // 显示他正在跑」. `last_polled_at` is written on failure too, so one figure
  // could not tell "we are polling it" from "it works"; the seed carries a
  // mailbox that is polled every few minutes and can never log in.
  const health = await page.locator('#admin-health').innerText();
  check(/轮询在跑/.test(health) && /取信正常/.test(health),
    '健康卡把「轮询在跑」和「取信正常」分成两个数', health.replace(/\n/g, ' '));
  const polled = Number((health.match(/轮询在跑[\s\S]{0,40}?(\d+)\s*\//) || [])[1]);
  const healthy = Number((health.match(/取信正常[\s\S]{0,40}?(\d+)\s*\//) || [])[1]);
  check(Number.isFinite(polled) && Number.isFinite(healthy),
    '两个数都读得出来', `polled=${polled} healthy=${healthy}`);
  check(healthy < polled,
    '轮询得到但登不进去的邮箱，不能算进「取信正常」', `${healthy} < ${polled}`);

  // -- 「正常」要能确定，就得看信有没有到 -----------------------------------
  // 用户原话：「收信正常那里一直显示 4，为什么每次都会这样，我要换一个方式来确定正常情况」。
  // 「我们登进去了几个」回答不了这个问题：好日子里它一动不动。真正的证据是**学校那封信
  // 真的到了**——所以那一格旁边必须有来信的数字，而且每个邮箱都要能自己下结论。
  check(/最近 24 小时本校来信/.test(health), '健康卡上有「最近 24 小时本校来信」这一格',
    health.replace(/\n/g, ' '));
  const delivery = await page.locator('#admin-delivery').innerText();
  check(/最近 \d+ 小时收到 \d+ 封本校来信|过去 \d+ 小时没有本校来信|没有任何一个邮箱收到过本校来信/.test(delivery),
    '证据区的第一句是「信有没有到」，三种情况分得开（有信到/这阵子没发/从来没到过）',
    delivery.slice(0, 160));
  check(/每个邮箱的收信证据/.test(delivery),
    '每个邮箱都能自己下结论，而不是只有全网一个数字', delivery.slice(0, 120));
  // 逐邮箱那几行在一个**默认收起**的 <details> 里，而 innerText 看不到收起的正文，
  // 所以这里像运营者那样点开再读。顺带证明折叠本身是好的——写死的「展开后长这样」
  // 是这一页最容易骗过自己的地方。
  const disclosure = page.locator('#admin-delivery details summary');
  check(await disclosure.count() === 1,
    '证据区有一个能点开的「每个邮箱的收信证据」', `count=${await disclosure.count()}`);
  await disclosure.click();
  const evidence = await page.locator('#admin-delivery details').innerText();
  check(/登不进去/.test(evidence) && /wrongcode@example\.com/.test(evidence),
    '登不进去的那个邮箱在证据里被点名', evidence.slice(0, 200));
  check(/从没收到过本校来信|最近一封本校来信/.test(evidence),
    '证据里写明了「有没有收到过本校来信」——转发唯一能被看见的证据', evidence.slice(0, 200));
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
  // 「我发出去的那封信到底有没有把人叫回来」——**印章答不了这个问题**（它只说明
  // 我们做了什么），会话表也答不了（退出登录就把行删了）。所以夹具里两种形状各有
  // 一个：一个提醒之后回来过、一个从没打开过应用。少了后者，「没回来」和「没提醒过」
  // 在面板上长得一模一样。
  const backCard = page.locator('#reminders-rows .adminnote',
    { hasText: 'cameback@example.com' });
  const neverCard = page.locator('#reminders-rows .adminnote',
    { hasText: 'nevercame@example.com' });
  check(/提醒之后回来过/.test(await backCard.innerText()),
    '提醒之后回来过的人，面板说了出来（带着他最近一次活跃的时间）',
    (await backCard.innerText()).replace(/\n/g, ' ').slice(0, 200));
  check(/提醒之后从没打开过应用/.test(await neverCard.innerText()),
    '从没打开过应用的人，不会被说成「回来过」',
    (await neverCard.innerText()).replace(/\n/g, ' ').slice(0, 200));
  await page.locator('#reminders-preview-box > summary').click();
  const reminderPreview = await page.locator('#reminders-preview').innerText();
  check(/还差一步/.test(reminderPreview) && /登录被拒绝/.test(reminderPreview),
    '两封信都能在按之前先看一遍', reminderPreview.slice(0, 120));
  check(/没有配微信联系方式/.test(reminderPreview),
    '这台机器没配微信，面板明说了（所以邮件里不会出现那一行）', reminderPreview.slice(0, 120));
  // 四种情况**各有一封**，而且每一封都能在按之前看到。
  // v0.63.47 加了 provider 那一组，预览却还留着手写的三条 —— 结果运营者
  // 看不到「换个邮箱」那封信，而它会在没人读过的情况下发出去。
  check(/没填过私人邮箱的人收到这封/.test(reminderPreview)
    && /授权码被拒的人收到这封/.test(reminderPreview)
    && /邮箱通了却收不到信的人收到这封/.test(reminderPreview)
    && /邮箱服务商停用了授权码登录的人收到这封/.test(reminderPreview),
    '四种情况各自的信都能先看一遍（含「服务商停用了授权码」那一封）',
    reminderPreview.replace(/\n/g, ' ').slice(0, 200));
  check(/每个人只会收到其中一封/.test(reminderPreview),
    '预览说清了「每个人只收到一封，不是一次发好几封」');
  // 四种情况的标题，一处定义、两处使用（预览 + 单人确认框）。
  const wantedHeads = ['没填过私人邮箱的人收到这封', '授权码被拒的人收到这封',
    '邮箱通了却收不到信的人收到这封', '邮箱服务商停用了授权码登录的人收到这封'];

  // 单独发一个人：用户原话「为什么不能单独发一个邮件给一个客户」。
  // 断言的是**请求体**：一次点击只能带一个 id，而且确认框要说出他会收到哪一封。
  const rowButtons = page.locator('#reminders-rows button', { hasText: '只发给他' });
  check(await rowButtons.count() >= 2, '名单上每一行都有自己的「只发给他」',
    `${await rowButtons.count()} 个`);
  let oneDialog = '';
  const oneRequest = page.waitForRequest((request) =>
    request.url().includes('/api/admin/setup-reminders') && request.method() === 'POST');
  page.once('dialog', (dialog) => { oneDialog = dialog.message(); dialog.accept(); });
  await rowButtons.first().click();
  const oneBody = JSON.parse((await oneRequest).postData() || '{}');
  check(oneBody.audience === 'selected' && Array.isArray(oneBody.user_ids)
    && oneBody.user_ids.length === 1, '一次点击只发给一个人',
    JSON.stringify(oneBody.user_ids));
  // 判据是「他说的那封，正是预览里的某一封」——两处用同一批标题，就不会各说各话。
  const named = wantedHeads.some((head) => oneDialog.includes(head));
  check(/他会收到：/.test(oneDialog) && named,
    '确认框说清了这个人会收到哪一封（用的是预览里同一批标题）',
    oneDialog.split('\n').slice(0, 4).join(' / '));
  await page.waitForTimeout(800);
  const sendLabel = await page.locator('#reminders-send').innerText();
  check(/\d/.test(sendLabel), '按钮上写着要发几封，而不是一个光秃秃的「发送」', sendLabel);
  // 第三个按钮：不管注册多久、也已经提醒过的，全都发一遍。运营者要的就是它。
  const allLabel = await page.locator('#reminders-all').innerText();
  check(/所有人/.test(allLabel) && /不管多久/.test(allLabel),
        '有一个「所有人都发，不管多久」的按钮', allLabel);

  // 自己选人发：「我要可以自己选给谁发卡住的邮件提醒」。
  //
  // 这一条**看请求体**，不是看按钮变灰 —— 这个动作的后果是给真人寄信，
  // 唯一值得断言的是「点出去的 user_ids 正好是勾选的那些」。
  const pickBoxes = page.locator('#reminders-pick-list input.reminder-pick');
  const pickCount = await pickBoxes.count();
  check(pickCount >= 2, '自己选人的名单里列出了卡住的账号', `checkbox=${pickCount}`);
  const pickSend = page.locator('#reminders-pick-send');
  check(await pickSend.isDisabled(), '一个人都没勾时按钮是禁用的');
  const pickValues = await pickBoxes.evaluateAll((nodes) => nodes.map((node) => node.value));
  await pickBoxes.nth(0).check();
  check(/（1）/.test(await pickSend.innerText()), '勾一个，按钮上的数字跟着变',
        await pickSend.innerText());
  check(!(await pickSend.isDisabled()), '勾上之后就点得动了');
  const sentBody = page.waitForRequest((request) =>
    request.url().includes('/api/admin/setup-reminders') && request.method() === 'POST');
  page.once('dialog', (dialog) => dialog.accept());          // 「真实邮箱，确定吗？」
  await pickSend.click();
  const body = JSON.parse((await sentBody).postData() || '{}');
  check(body.audience === 'selected', '发出去的请求写明是「手选」这一档', JSON.stringify(body));
  check(Array.isArray(body.user_ids) && body.user_ids.length === 1
        && body.user_ids[0] === pickValues[0],
        '请求体里正好是勾选的那一个人', JSON.stringify(body.user_ids));
  await page.waitForTimeout(1500);
  // 这个套件里**真的发不出信**（运营者账号没有配 SMTP，夹具也不该往真人邮箱发信），
  // 所以这里断的不是「发出去了」，而是**面板说的话与实际结果一致**：
  // 发出去了就把勾去掉（再按不会重复发），没发出去就留着勾并把原因说出来。
  // 「失败时静默当作已发」才是真正要防的那件事。
  const afterStatus = (await page.locator('#reminders-status').innerText()).trim();
  const afterLabel = await pickSend.innerText();
  const sentSomething = /发出 [1-9]/.test(afterStatus);
  check(/发出 \d+ 封|发送失败/.test(afterStatus), '发完如实说了结果', afterStatus);
  check(sentSomething ? /（0）/.test(afterLabel) : /（1）/.test(afterLabel),
        sentSomething ? '发出去的人从勾选里去掉（再按不会重复发）'
                      : '没发出去时勾选留着，可以再按一次',
        `sent=${sentSomething} status=${afterStatus} label=${afterLabel} `
        + `note=${await page.locator('#reminders-pick-note').innerText()}`);
  // 正文可以直接在后台改，占位符写错会被服务端拒绝（不原样寄给用户）。
  await page.locator('#panel-reminder-text > summary').click();
  const templateBox = page.locator('#reminder-text-never');
  check((await templateBox.inputValue()).includes('{link}'),
        '正文编辑器里是那封信的原文（含 {link} 占位符）');
  // 「写错的占位符被拒绝」不在这里按：那是一次真实的 422，浏览器会把它记成
  // 一条资源错误，而本套件把任何 console 错误都当成失败。接口层已经钉住了它
  // （test_admin.test_the_letter_can_be_edited_and_a_bad_placeholder_is_refused）。
  await page.fill('#reminder-text-never', '同学你好：\n\n请看 {link} 把邮箱接上，有问题直接回这封邮件。');
  await page.locator('#reminder-text-save-never').click();
  await page.waitForTimeout(500);
  check(/已保存/.test(await page.locator('#reminder-text-status-never').innerText()),
        '改成自己的措辞能保存', await page.locator('#reminder-text-status-never').innerText());
  const editedPreview = await page.locator('#reminder-text-note').innerText();
  check(/改过/.test(editedPreview), '面板标明这一封已经改过', editedPreview);
  await page.locator('#reminder-text-reset-never').click();   // 恢复默认，别影响后面的断言
  await page.waitForTimeout(400);
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
  // 按账号断言：夹具里本来就有两个「已提醒过」的账号（用来断言「回来过没有」），
  // 对整块名单说「一个字都不许出现『已在』」测的就不是这件事了。
  const failedCards = page.locator('#reminders-rows .adminnote',
    { hasText: 'stalled@example.com' });
  const failedCard = await failedCards.first().innerText();
  check(/还没提醒过/.test(failedCard),
    '失败的没有被记成「已提醒」', failedCard.replace(/\n/g, ' ').slice(0, 160));
  check(!/已在/.test(failedCard), '失败的账号没有被盖章',
    failedCard.replace(/\n/g, ' ').slice(0, 160));
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
  // or had a report sent -- except for 搜索, and that exception is the point of
  // the last two assertions below.
  await expandCard(page, memberEmail);          // 展开后那半句「为什么」才显示
  const lights = refreshed.locator('.light');
  const lightCount = await lights.count();
  check(lightCount === 4, '每个账号四盏灯都在', String(lightCount));
  const lightText = (await lights.allInnerTexts()).join(' / ');
  check(/没测过|还没出过报告/.test(lightText),
    '红灯写明了是「没测过」，而不是笼统的一句失败', lightText);
  const greenCount = await refreshed.locator('.light.ok').count();
  check(greenCount === 0, '一个从没真正跑过的账号不允许出现绿灯', `${greenCount} 盏绿`);

  // 第三态：这一格不适用，所以它既不是绿的也不是红的。
  //
  // 用户原话（2026-09-16）：「为什么点刷新用户状态还是亮红灯」。没有自己的 key、
  // 走平台兜底 key 的账号，模型/搜索那两盏灯**点多少次刷新都不会变绿**（测试结果
  // 写进 `connections` 那一行，而他没有那一行）——红灯点不亮，读的人就学会不看
  // 这个面板了。这个套件的环境里只给了**平台搜索 key**（`run_browser_checks.sh`，
  // base URL 指向一个关着的本地端口，所以不产生任何外部请求），所以灰的那一盏是
  // 搜索；模型那盏在这个环境里仍然红得对（连平台 key 都没有，确实谁都用不了）。
  const lightClasses = await lights.evaluateAll((nodes) => nodes.map((node) => node.className));
  const sharedIndex = lightClasses.findIndex((cls) => /\bshared\b/.test(cls));
  check(sharedIndex === 2 && lightClasses[2].includes('shared'),
    '走平台兜底 key 的那一盏画成第三态（搜索），不是红灯',
    JSON.stringify(lightClasses));
  const sharedText = await lights.nth(2).innerText();
  check(/平台兜底/.test(sharedText) && !/没测过/.test(sharedText),
    '第三态说的是「走平台兜底 key」，不是「从没测过」', sharedText);
  const dotColours = await lights.locator('.dot').evaluateAll(
    (nodes) => nodes.map((node) => getComputedStyle(node).backgroundColor));
  check(dotColours.length === 4
        && new Set(dotColours.slice(0, 2).concat(dotColours.slice(3))).size === 1
        && dotColours[2] !== dotColours[0],
    '红色只出现在真的没做到的地方，第三态是另一种颜色', JSON.stringify(dotColours));
  await page.screenshot({ path: path.join(SHOTS, 'admin-lights.png') });

  // -- the operator's note -------------------------------------------------
  await expandCard(page, memberEmail);
  const noteBox = refreshed.locator('.adminnote textarea');
  check(await noteBox.count() === 1, '每个账号都有管理员备注框');
  // 截图不能把「元素在取景那一刻被重绘掉」当成产品失败：CI 上就是这么红的
  // （`locator.screenshot: Element is not attached to the DOM`）——这个面板背后
  // 有轮询在刷新列表，取景框和快门之间隔着一次重渲染。截图只是存档，
  // 所以重试一次、再不行就整页截，绝不让它把一个绿色的套件判红。
  await elementShot(page, refreshed, '.adminnote', 'admin-note.png');
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

  // -- 一次刷新必须真的把**每一个**面板都同步一遍（2026-09-17）----------------
  // 用户原话：「我刷新后台……是不是后台所有的数据都可以被实时同步一遍」。在那之前
  // 这条只对**访问统计**一个面板断言过（refresh_feedback_check），而且只覆盖**展开
  // 着**的面板——收起的面板摘要行上照样写着数字（「4 个卡住 · 2 个还没提醒过」
  // 「2 个可用」…），于是刷新之后屏幕上仍有一半是旧的。现在三件事都查：
  //   ① 后台里的每个面板都登记了加载函数（没登记的当场点名）；
  //   ② **全部收起**再按一次刷新，每一个面板都真的重新拉了一次；
  //   ③ 收起时还显示数字的那些摘要行，其接口确实出现在这一次刷新的请求里。
  const panelMap = await page.evaluate(() => {
    const all = [];
    const nested = [];
    document.querySelectorAll('#section-admin details[id^="panel-"]').forEach((node) => {
      all.push(node.id);
      const holder = node.parentElement && node.parentElement.closest('details[id^="panel-"]');
      // 嵌在别的面板里的说明块（例：两封信的正文）不是独立面板：它的数据跟父面板
      // 同一次请求回来，所以它不需要（也不该有）自己的加载函数。
      if (holder) nested.push(node.id);
    });
    return { all, nested, wired: Object.keys(PANEL_LOADERS) };
  });
  const unwired = panelMap.all.filter(
    (id) => !panelMap.wired.includes(id) && !panelMap.nested.includes(id));
  check(unwired.length === 0,
    '后台每一个面板都登记了加载函数（漏一个它就会一直显示旧数字）',
    unwired.length ? unwired.join('、')
      : `${panelMap.all.length} 个面板节点，其中 ${panelMap.nested.length} 个是嵌在别的面板里的说明块`);
  // 先把它们打开一次（每个面板自己拉一遍），再全部收起——接下来数到的就只是刷新那一次。
  await page.evaluate((ids) => {
    ids.forEach((id) => { const node = document.getElementById(id); if (node) node.open = true; });
  }, panelMap.wired);
  await page.waitForTimeout(1500);
  const collapsedEvidence = await page.evaluate((ids) => {
    ids.forEach((id) => { const node = document.getElementById(id); if (node) node.open = false; });
    const out = {};
    document.querySelectorAll('#section-admin [id$="-note"]').forEach((node) => {
      const text = (node.textContent || '').trim();
      // 「—」和空串是「还没取」；剩下的都声称自己知道一个数，那它就必须在这次刷新里被重取。
      if (text && text !== '—') out[node.id] = text.slice(0, 60);
    });
    return out;
  }, panelMap.wired);
  check(Object.keys(collapsedEvidence).length >= 5,
    '收起的面板摘要行上就写着数字（这正是「收起≠不用刷新」的理由）',
    Object.entries(collapsedEvidence).slice(0, 4).map(([id, t]) => `${id}="${t}"`).join(' · '));
  await page.evaluate(() => {
    window.__panelCalls = {};
    for (const [id, fn] of Object.entries(PANEL_LOADERS)) {
      PANEL_LOADERS[id] = (...args) => {
        window.__panelCalls[id] = (window.__panelCalls[id] || 0) + 1;
        return fn(...args);
      };
    }
    window.__adminUrls = [];
  });
  page.on('request', (request) => {
    const url = request.url();
    if (url.includes('/api/admin/')) {
      page.evaluate((u) => window.__adminUrls.push(u), url).catch(() => {});
    }
  });
  await page.click('#admin-refresh');
  await page.waitForFunction(() => {
    const node = document.getElementById('admin-refresh');
    return node && !node.disabled && node.textContent === '刷新全部';
  }, null, { timeout: 30000 });
  await page.waitForTimeout(600);
  const panelCalls = await page.evaluate(() => window.__panelCalls || {});
  const notRefreshed = panelMap.wired.filter((id) => !panelCalls[id]);
  check(notRefreshed.length === 0,
    '全部收起时按一次「刷新全部」，每一个面板都真的重新拉了一次数据',
    notRefreshed.length ? `没拉到的：${notRefreshed.join('、')}`
      : `${Object.keys(panelCalls).length} 个面板全部拉过`);
  // 数字从哪来：摘要行上写着数字的面板，其接口必须出现在**这一次**刷新的请求里。
  // （`/api/admin/users` 是概览那一次，用户/管理员/申请/审计/广播/巡检/邀请码这些
  // 摘要都由它重画。）
  const summarySource = {
    'panel-mail-note': '/api/admin/messages',
    'panel-usage-note': '/api/admin/usage',
    'panel-capacity-note': '/api/admin/capacity',
    'panel-agent-note': '/api/admin/agent',
    'panel-digest-note': '/api/admin/digest',
    'panel-reminders-note': '/api/admin/setup-reminders',
    'panel-metrics-note': '/api/admin/metrics',
    'panel-analytics-note': '/api/admin/analytics',
    'panel-guestbook-note': '/api/admin/guestbook',
  };
  const adminUrls = await page.evaluate(() => window.__adminUrls || []);
  const notFetched = Object.entries(summarySource)
    .filter(([id]) => id in collapsedEvidence)
    .filter(([, needle]) => !adminUrls.some((url) => url.includes(needle)))
    .map(([id]) => id);
  check(notFetched.length === 0,
    '收起但带着数字的摘要行，也跟着这一次刷新重新拉过（以前只刷展开的那几个）',
    notFetched.length ? `没拉的：${notFetched.join('、')}`
      : `这一次刷新打了：${[...new Set(adminUrls.map((u) => u.split('/api/admin/')[1].split('?')[0]))].sort().join('、')}`);
  const notesAfter = await page.evaluate(() => {
    const out = {};
    document.querySelectorAll('#section-admin [id$="-note"]').forEach((node) => {
      const text = (node.textContent || '').trim();
      if (text && text !== '—') out[node.id] = text.slice(0, 60);
    });
    return out;
  });
  const wiped = Object.keys(collapsedEvidence).filter((id) => !(id in notesAfter));
  check(wiped.length === 0, '刷新不会把摘要行擦成「—」或错误（那等于把知道的事忘掉）',
    wiped.join('、') || `仍然是 ${Object.keys(notesAfter).length} 行带数字`);
  const refreshToast = await page.evaluate(() => Array.from(
    document.querySelectorAll('#toasts .toast')).map((node) => node.textContent).join(' | '));
  check(/已刷新：概览，\d+ 个面板/.test(refreshToast) && !/失败/.test(refreshToast),
    '刷新结果如实说「刷了几个面板、有没有失败」', refreshToast || '（无提示）');
  // 刷新完把面板收回去，后面几段仍然按「展开才加载」的老规矩跑。
  await page.evaluate((ids) => {
    ids.forEach((id) => {
      const node = document.getElementById(id);
      if (node && id !== 'panel-users') node.open = false;
    });
  }, panelMap.wired);
  await page.waitForTimeout(400);

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

  // 再发一条：用户手上有**两条**没确认的公告。这是 2026-09-16 那个故障的形状 ——
  // 点掉第一条之后紧接着显示第二条，而那颗按钮还停在禁用态，于是怎么点都没反应，
  // 整个应用被一条关不掉的公告挡住（生产上 6 个账号一条都没确认掉）。
  // 标题不能包含第一条的标题：下面按标题过滤文章时 strict 模式会因为前缀撞车报错
  // （第一版就是这么挂的 —— 工装的错，不是产品的）。
  const secondTitle = `第二条公告 ${stamp}`;
  await page.fill('#broadcast-title', secondTitle);
  await page.fill('#broadcast-body', '第二条公告：用来验证连续两条都能点掉。');
  await page.selectOption('#broadcast-tone', 'info');
  await page.click('#broadcast-publish');
  await page.waitForFunction(() => {
    const status = document.getElementById('broadcast-status');
    return Boolean(status && /已发布/.test(status.textContent));
  }, null, { timeout: 10000 });
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
  check(banner.includes(secondTitle), '用户一打开应用就看到广播（最新那条先说）', banner.slice(0, 80));
  // 盖住整页的对话框：它必须挡住背后的界面，而且只有「确认收到」能关掉它。
  const modalBox = await readerPage.locator('#announcement').boundingBox();
  const viewport = readerPage.viewportSize();
  check(modalBox && viewport && modalBox.width >= viewport.width - 2 && modalBox.height >= viewport.height - 2,
        '广播是盖住整页的对话框，不是首页里的一条横幅',
        JSON.stringify({ modalBox, viewport }));
  check(await readerPage.locator('#announcement').getAttribute('aria-modal') === 'true',
        '对话框标了 aria-modal');
  const cardTone = await readerPage.locator('#announcement-card').getAttribute('class');
  check(!/warn|critical/.test(cardTone), '广播按类型着色（这条是 info，不该带警告色）', cardTone);
  check(await readerPage.locator('#announcement-ack').innerText()
          .then((text) => /^确认收到(（还有 \d+ 条）)?$/.test(text.trim())),
        '唯一的按钮写着「确认收到」（还有几条时会带上「还有 N 条」）');
  // 没确认就走不掉：ESC 和点空白都不该关掉它。
  await readerPage.keyboard.press('Escape');
  await readerPage.waitForTimeout(200);
  check(await readerPage.locator('#announcement:not(.hidden)').count() === 1, '按 ESC 关不掉（必须点确认）');
  await readerPage.mouse.click(5, 5);
  await readerPage.waitForTimeout(200);
  check(await readerPage.locator('#announcement:not(.hidden)').count() === 1, '点空白也关不掉');
  await readerPage.screenshot({ path: path.join(SHOTS, 'user-broadcast-modal.png') });

  // 点「确认收到」——**用真实坐标点**，不是 `node.click()`。
  //
  // 这一条是这次故障的关键：`node.click()` 绕过命中测试（事件直接派给元素，
  // 不管它此刻是不是 disabled、上面有没有东西压着），所以旧写法在「按钮是
  // 禁用的」这个真故障下照样绿。真人点的是坐标，这里就点坐标。
  const ackTarget = async () => readerPage.evaluate(() => {
    const button = document.getElementById('announcement-ack');
    const box = button.getBoundingClientRect();
    const centre = { x: Math.round(box.left + box.width / 2), y: Math.round(box.top + box.height / 2) };
    const hit = document.elementFromPoint(centre.x, centre.y);
    return {
      centre, disabled: button.disabled, label: button.textContent.trim(),
      hitIsButton: hit === button,
      help: document.getElementById('announcement-help').textContent,
      title: document.getElementById('announcement-title').textContent,
    };
  });

  let first = await ackTarget();
  check(first.hitIsButton, '「确认收到」的中心点真的能被打到（命中测试）', JSON.stringify(first.centre));
  check(first.disabled === false, '第一条公告的按钮是可点的', first.label);
  check(/还有 1 条/.test(first.label) || /还有 1 条/.test(first.help),
        '按钮/说明写清还有几条没确认（否则「点完又弹一条」看着像没生效）', first.label + ' | ' + first.help);
  await readerPage.mouse.click(first.centre.x, first.centre.y);
  await readerPage.waitForTimeout(1200);

  // 第二条：必须**还能点**。这是那个 bug 的核心断言。
  const second = await ackTarget();
  const secondVisible = await readerPage.locator('#announcement:not(.hidden)').count() === 1;
  check(secondVisible, '确认第一条之后紧接着显示下一条', second.title);
  if (secondVisible) {
    check(second.title.includes(broadcastTitle), '第一条确认掉之后轮到较旧的那条', second.title);
    const secondTone = await readerPage.locator('#announcement-card').getAttribute('class');
    check(/warn/.test(secondTone), '广播按类型着色（这条是 warn，必须带警告色）', secondTone);
    check(second.disabled === false,
          '**第二条公告的按钮不是禁用的**（2026-09-16 的故障：点掉一条后按钮留在禁用态）',
          `disabled=${second.disabled} label=${second.label}`);
    check(second.hitIsButton, '第二条的按钮中心点也能被打到', JSON.stringify(second.centre));
    await readerPage.mouse.click(second.centre.x, second.centre.y);
    await readerPage.waitForTimeout(1200);
  }
  check(await readerPage.locator('#announcement.hidden').count() === 1,
        '两条都点掉之后对话框消失', `second=${JSON.stringify(second)}`);

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
  check(ackState.disabled === false, '对话框关掉之后按钮不留在禁用态', JSON.stringify(ackState));

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

  // -- 替用户刷新状态：全部人 / 某一个人 --------------------------------------
  //
  // 用户原话：「帮我做对每一个用户都可以一键刷新他们所有状态的按钮，我要这个按钮
  // 可以选择全部人也可以单某个人」。夹具里的邮箱连不上（imap_host='h'），所以这一
  // 段钉住的是**它有没有真的去测、有没有如实报**，不是它能不能成功——一个连不上
  // 的账号被报成通过，比不刷新更糟。
  await ensurePanel(page, 'panel-users');
  await page.waitForTimeout(300);
  const everyLabel = await page.innerText('#users-refresh-all');
  check(/刷新全部（\d+）/.test(everyLabel), '用户面板上有「刷新全部（N）」', everyLabel);

  await expandCard(page, memberEmail);
  const [refreshRequest] = await Promise.all([
    page.waitForRequest((req) => req.method() === 'POST' && req.url().includes('/refresh')),
    cardFor(page, memberEmail).locator('button', { hasText: '刷新状态' }).click(),
  ]);
  const refreshPath = new URL(refreshRequest.url()).pathname;
  check(/^\/api\/admin\/users\/[^/]+\/refresh$/.test(refreshPath),
        '「刷新状态」打的是这个账号自己的刷新接口', refreshPath);
  await page.waitForFunction(
    () => /刷新完成|已停止/.test((document.getElementById('users-refresh-progress') || {}).textContent || ''),
    null, { timeout: 30000 });
  const refreshReport = await page.innerText('#users-refresh-results');
  check(/收信/.test(refreshReport) && /模型/.test(refreshReport) && /搜索/.test(refreshReport),
        '三件事逐项写了结果，不是一句「已刷新」', refreshReport.slice(0, 160));
  check(/✗/.test(refreshReport),
        '连不上的账号如实报成失败（夹具的邮箱本来就连不上）', refreshReport.slice(0, 160));
  // 面板要**先说清**点完为什么还可能红（用户 2026-09-16 的疑问就是这么来的）：
  // 一句话写在按钮旁边，不用他再点一次才发现没有用。
  const panelHint = await page.locator('#panel-users .panel-body .help').first().innerText();
  check(/「出报告」不在这次测试里/.test(panelHint) && /走平台兜底 key」不是故障/.test(panelHint),
        '面板先说清了「点完为什么还可能红」', panelHint.slice(0, 120));
  check(await page.locator('#admin-users details.admin-user-box[open]').count() === 1,
        '刷新之后正在看的那一张还开着（重画不该把人正在读的东西收起来）');
  const reportTone = await cardFor(page, memberEmail)
    .locator('.light', { hasText: '出报告' }).first().getAttribute('class');
  check(!/ ok/.test(reportTone || ''),
        '刷新不会伪造「出报告」那盏灯——它只能由一封真的来信点亮', reportTone || '');

  await page.check('#users-pick-all');
  await page.waitForTimeout(150);
  const pickedLabel = await page.innerText('#users-refresh-picked');
  check(/刷新勾选的（[1-9]\d*）/.test(pickedLabel), '全选之后按钮上带着人数', pickedLabel);
  await page.uncheck('#users-pick-all');
  await page.waitForTimeout(150);
  check(/（0）/.test(await page.innerText('#users-refresh-picked')), '取消全选后回到 0');

  // -- 第三种提醒：邮箱通了，却一封 CityU 来信都没到过 -------------------------
  //
  // 「转发的唯一证据是信真的到了」——这句话以前只写在文档里。运营者要能一眼认出
  // 这种人，并且有一封说学校那一边该怎么做（而不是「你还没配好」）的信发给他。
  await ensurePanel(page, 'panel-reminders');
  await page.waitForTimeout(400);
  // 编辑框里是**模板**（留着 {steps}），真正发出去的那一份在预览里 —— 两件
  // 事要分开断言：第一版就是在编辑框里找 URL，结果它当然找不到。
  const noMailBody = await page.inputValue('#reminder-text-no_mail');
  check(/\{steps\}/.test(noMailBody) && /转发/.test(noMailBody),
        '第三种提醒的模板留着 {steps} 占位符（步骤发送时才替换）', noMailBody.slice(0, 90));
  const previewText = await page.innerText('#reminders-preview');
  check(/autoforward\.htm/.test(previewText), '预览里那份带着 CityU 官方的转发说明链接',
        previewText.slice(previewText.indexOf('邮箱通了'), previewText.indexOf('邮箱通了') + 120));
  check(/不要转给自己/.test(previewText), '预览里有官方那条循环警告（转发给自己会两头都收不到）');
  check(/weblogon_o365_student/.test(previewText), '预览里给了学校邮箱的登录入口，不是一个要用户自己猜的菜单');
  // 数目不写死：这里以前数的是「3」，加第四种情况（服务商停用授权码）时它照样绿，
  // 因为那句断言只认总数——而真正要防的是「某一种情况的信没显示出来」。
  // 所以逐个点名，缺哪一个就红在名字上。
  const previewHeads = await page.locator('#reminders-preview h4').allTextContents();
  const missingHeads = wantedHeads.filter((head) => !previewHeads.includes(head));
  check(missingHeads.length === 0, '四种情况各自的信都在预览里（一个不缺）',
    missingHeads.length ? `缺：${missingHeads.join(' / ')}` : `${previewHeads.length} 封`);
  await page.screenshot({ path: path.join(SHOTS, 'reminders-all-letters.png') });

  // 用户侧：第 2 步必须说清楚「转发的证据到底有没有」——那是唯一无法从我们这边
  // 测试的一步，以前它只在四格进度的 tooltip 里，手机上根本没有 hover。
  await goTo(page, 'mailbox');
  await page.waitForTimeout(400);
  const forwardCheck = await page.innerText('#forward-check');
  check(/已经处理过|还没收到过/.test(forwardCheck),
        '设置向导第 2 步写出了转发的证据', forwardCheck);
  check(await page.locator('#forward-check').isVisible(), '这句话是画出来的，不是藏在 tooltip 里');

  await context.close();
  await browser.close();
  check(errors.length === 0, '没有 JS 异常 / 资源缺失', errors.slice(0, 3).join(' | '));
  console.log(failures.length ? `\nFAILED (${failures.length}): ${failures.join('; ')}` : '\nALL ADMIN EDIT CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
