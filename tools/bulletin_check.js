/* Real-browser check for the public bulletin board.
 *
 * Two halves that only mean something together: a broadcast published from the
 * console with the board flag on has to show up on `/` *for a visitor who is not
 * signed in*, and taking it down has to remove it from there as well. A check
 * that read the board back through an authenticated session would pass even if
 * the board only rendered for operators.
 *
 *   PILOT_ADMIN=boss@example.com node tools/bulletin_check.js http://127.0.0.1:8920 /tmp/bulletin-shots
 */
'use strict';

const fs = require('fs');
const { chromium } = require('./pw');
const { goTo, openPanel } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8920';
const SHOTS = process.argv[3] || '/tmp/bulletin-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
// 一张现成的干净 PNG（背景图那套测试用的同一张：没有 EXIF，服务端才收）。
// 浏览器还会把它重编码成 JPEG —— 那一步正是「上传前先去掉元数据」。
const PNG = require('path').join(__dirname, '..', 'pilot_app', 'static', 'bg-paper.png');
const PASSWORD = process.env.PILOT_PASSWORD || 'a-long-enough-password';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

/** What an anonymous visitor sees at `/`, read from a session with no cookies. */
async function anonymousView(browser) {
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load' });
    return await page.innerText('body');
  } finally {
    await context.close();
  }
}

/**
 * 布告栏上那张配图**真的画出来了吗**。
 *
 * `innerText` 对图片一个字都不说：「文件在服务器上」「<img> 在 HTML 里」
 * 「浏览器把它解码出来了」是三件事，而这个项目在示意图那一轮已经栽过一次
 * （文件在 static/ 里但没登记进白名单，页面上是一个破图标）。
 * 所以量 `naturalWidth`，并且连 `<img>` 到底有几个一起报出来。
 */
async function boardPhoto(browser) {
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load' });
    const photos = page.locator('img.notice-photo');
    const count = await photos.count();
    if (!count) return { count };
    const width = await photos.first().evaluate((node) => node.naturalWidth || 0);
    const shown = await photos.first().boundingBox();
    return { count, width, box: shown };
  } finally {
    await context.close();
  }
}

/** 选一张配图并等它上传完（预览出现 = 服务端已经收下了）。 */
async function attachPhoto(page, file) {
  await page.setInputFiles('#broadcast-image', file);
  await page.waitForSelector('#broadcast-image-preview:not([hidden]) img', { timeout: 15000 });
  return page.innerText('#broadcast-image-note');
}

/** Fill the broadcast form and publish. */
async function publish(page, { title, body, toBoard, tone }) {
  await page.fill('#broadcast-title', title);
  await page.fill('#broadcast-body', body);
  if (tone) await page.selectOption('#broadcast-tone', tone);
  const flag = page.locator('#broadcast-public');
  if ((await flag.isChecked()) !== Boolean(toBoard)) await flag.click();
  await page.click('#broadcast-publish');
  await page.waitForFunction(
    () => !/正在提交/.test(document.getElementById('broadcast-status').textContent || ''),
    null, { timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(300);
  return page.innerText('#broadcast-status');
}

/** Click a row button by its *exact* label. */
async function rowButton(page, title, label) {
  const row = page.locator('#admin-announcements article').filter({ hasText: title });
  // Exact match, not a substring: 「撤下」 is contained in 「从布告栏撤下」, and a
  // substring match would click the wrong button and report a product failure.
  await row.locator('button').filter({ hasText: new RegExp(`^${label}$`) }).first().click();
  await page.waitForTimeout(700);
  return page.innerText('#admin-status');
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  const errors = [];

  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  // One handler for the whole run, accepting every confirmation. Registering it
  // per publish would stack handlers, and the second one to run on the same
  // dialog throws.
  page.on('dialog', (dialog) => { dialog.accept().catch(() => {}); });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN_EMAIL);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await goTo(page, 'admin');
  await openPanel(page, 'panel-broadcast');
  await page.waitForSelector('#broadcast-public', { state: 'visible', timeout: 10000 });

  const flag = page.locator('#broadcast-public');
  check(await flag.getAttribute('type') === 'checkbox',
        '广播面板里有一个「同时贴到官网布告栏」复选框');
  check(!(await flag.isChecked()), '它默认不勾（公开是显式选择，不是默认）');
  const warning = await page.innerText('#panel-broadcast');
  check(/公开/.test(warning) && /搜索引擎/.test(warning),
        '旁边写明了这是公开内容、搜索引擎也看得到');

  const before = await anonymousView(browser);
  check(!before.includes('布告栏'), '还没有公告时，首页上完全没有布告栏这一块');

  // ---- a notice that stays internal -------------------------------------
  const internal = await publish(page, {
    title: '只给内部看的通知', body: '这条不该出现在官网上。', toBoard: false,
  });
  check(/已发布/.test(internal), '不发布告栏时普通发布照常成功', internal);
  const afterInternal = await anonymousView(browser);
  check(!afterInternal.includes('只给内部看的通知'),
        '没勾那个开关的公告不会跑到官网上');

  // ---- a notice that goes public ----------------------------------------
  // 配图：用户原话「我要在广播哪里可以添加图片和文字一起广播」。这里走完整条路 ——
  // 选文件（浏览器重编码）→ 上传草稿 → 预览 → 发布 → **未登录的访客看到那张图**。
  const photoNote = await attachPhoto(page, PNG);
  check(/会随广播一起显示/.test(photoNote), '选好的配图在上传后立刻给出预览', photoNote);
  const visible = await publish(page, {
    title: '本周六 22:00 系统维护', body: '维护大约两小时，期间报告会晚一点。',
    toBoard: true, tone: 'warn',
  });
  check(/布告栏/.test(visible), '发布回执里说明了官网布告栏也更新了', visible);

  // 图**真的解码出来了**才算数：`naturalWidth` 是浏览器给的答案，不是我们猜的。
  const photo = await boardPhoto(browser);
  check(photo.count === 1 && photo.width > 100,
        '布告栏上的配图真的画出来了（不是 HTML 里有个 <img> 而已）',
        JSON.stringify(photo));

  const board = await anonymousView(browser);
  check(board.includes('本周六 22:00 系统维护') && board.includes('维护大约两小时'),
        '未登录的访客在首页看到了这条通知');
  check(/布告栏/.test(board), '首页出现了「布告栏」小标题');
  check(/\(GMT\+8\)/.test(board), '布告栏上的时间带了明确的时区标记');
  check(board.indexOf('本周六 22:00 系统维护') > board.indexOf('布告栏'),
        '通知排在布告栏标题下面');
  check(!board.includes('{{BULLETIN}}'), '页面上没有留下服务端占位符');

  const roster = await page.innerText('#admin-announcements');
  check(/已在官网布告栏/.test(roster), '控制台的历史公告里标出了「已在官网布告栏」');

  // ---- taking it down ----------------------------------------------------
  await rowButton(page, '本周六 22:00 系统维护', '从布告栏撤下');
  const afterOff = await anonymousView(browser);
  check(!afterOff.includes('本周六 22:00 系统维护'), '撤下之后官网上就没有了');
  check(!afterOff.includes('布告栏'), '撤下最后一条之后整块布告栏都不见了');
  const internalStillThere = await page.innerText('#admin-announcements');
  check(internalStillThere.includes('本周六 22:00 系统维护'),
        '从布告栏撤下不会把站内广播一起撤掉');

  // ---- putting it back ---------------------------------------------------
  await rowButton(page, '本周六 22:00 系统维护', '贴到布告栏');
  const afterOn = await anonymousView(browser);
  check(afterOn.includes('本周六 22:00 系统维护'), '可以重新贴回布告栏');

  // ---- the limit ---------------------------------------------------------
  for (const index of [1, 2, 3]) {
    await publish(page, { title: `补一条 ${index}`, body: `第 ${index} 条。`, toBoard: true });
  }
  const full = await anonymousView(browser);
  check(full.includes('补一条 3') && full.includes('补一条 1'),
        '最新的三条都在布告栏上');
  check(!full.includes('本周六 22:00 系统维护'),
        '超过三条时最早的一条不再显示（布告栏不是归档）');

  // ---- withdrawing also clears the board ---------------------------------
  await rowButton(page, '补一条 3', '撤下');
  const afterWithdraw = await anonymousView(browser);
  check(!afterWithdraw.includes('补一条 3'), '撤下公告会同时把它从布告栏拿掉');

  // ---- phone -------------------------------------------------------------
  const phone = await browser.newContext({ viewport: { width: 360, height: 780 } });
  const phonePage = await phone.newPage();
  phonePage.on('pageerror', (error) => errors.push(error.message));
  await phonePage.goto(`${BASE}/`, { waitUntil: 'load' });
  const landingOverflow = await phonePage.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  check(landingOverflow.scroll <= landingOverflow.client + 1,
        '360px 下首页（含布告栏）不横向溢出',
        `${landingOverflow.scroll} > ${landingOverflow.client}`);
  await phonePage.screenshot({ path: `${SHOTS}/landing-board-360.png`, fullPage: true });

  await phonePage.goto(`${BASE}/app`, { waitUntil: 'load' });
  await phonePage.fill('#auth-email', ADMIN_EMAIL);
  await phonePage.fill('#auth-password', PASSWORD);
  await phonePage.click('#login');
  await phonePage.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await goTo(phonePage, 'admin');
  await openPanel(phonePage, 'panel-broadcast');
  await phonePage.waitForTimeout(300);
  const panelOverflow = await phonePage.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  check(panelOverflow.scroll <= panelOverflow.client + 1,
        '360px 下广播面板（含新开关）不横向溢出',
        `${panelOverflow.scroll} > ${panelOverflow.client}`);
  await phonePage.screenshot({ path: `${SHOTS}/console-broadcast-360.png`, fullPage: true });
  await page.screenshot({ path: `${SHOTS}/console-broadcast.png`, fullPage: true });

  check(errors.length === 0, '没有未捕获的前端异常', errors.join(' | ') || '（无）');

  await browser.close();
  console.log(`\n截图在 ${SHOTS}`);
  if (failures.length) {
    console.log(`\n${failures.length} 项未过：`);
    failures.forEach((name) => console.log(`  - ${name}`));
    process.exit(1);
  }
  console.log('\n全部通过。');
})();
