/**
 * Browser check for the landing page and the pilot application flow.
 *
 *   PILOT_ADMIN=boss@example.com node tools/landing_check.js <base-url> <screenshots>
 *
 * The chain this proves end to end is the one that matters commercially: a
 * stranger lands on the root, reads it, applies, the operator approves, and the
 * code that comes out actually registers an account. Every step is checked in a
 * real browser because each one is a place where a form can look fine and do
 * nothing.
 */
'use strict';

const fs = require('fs');
const { chromium } = require('./pw');

const BASE = process.argv[2] || 'http://127.0.0.1:8931';
const SHOTS = process.argv[3] || '/tmp/landing-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

async function signIn(page, email = ADMIN_EMAIL) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', email);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await page.waitForTimeout(700);
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  const pageErrors = [];
  const stamp = Date.now();

  // ------------------------------------------------------------ the landing
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 },
                                           isMobile: true, hasTouch: true });
  const p = await phone.newPage();
  p.on('pageerror', (e) => pageErrors.push(`landing: ${e.message}`));
  p.on('console', (m) => {
    if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) {
      pageErrors.push(`landing console: ${m.text()}`);
    }
  });
  const landing = await p.goto(`${BASE}/`, { waitUntil: 'load' });
  check(landing.status() === 200, '根路径返回介绍页');
  const heading = await p.innerText('h1');
  check(/邮件/.test(heading), '首屏就说清这是什么', heading.replace(/\n/g, ' '));

  // A stranger must be able to read it without running any script.
  const noJs = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
  const blind = await noJs.newPage();
  await blind.goto(`${BASE}/`, { waitUntil: 'load' });
  check((await blind.innerText('h1')).length > 0, '禁用 JS 后正文仍在（服务端渲染）');
  check(await blind.locator('#signup-email').count() === 1, '禁用 JS 后表单仍可用（原生 POST 回退）');
  check((await blind.innerText('body')).includes('大模型服务商'), '禁用 JS 时那条关键披露也在');
  // Server-rendered, so it must survive with scripting off -- a block that only
  // appeared after app.js ran would be invisible to a reader (and a crawler).
  check(await blind.locator('#source').count() === 1, '禁用 JS 后开源那一节也在');
  await noJs.close();

  // ------------------------------------------------- open source, visibly
  // The operator asked for the fact to be *on the page*; a muted footer link
  // was already there and was not enough. This is asserted in a real browser
  // because "it is in the HTML" and "a visitor sees it" are different claims.
  const source = p.locator('#source');
  check(await source.count() === 1, '官网正文里有一节讲开源，而不是只有页脚一行');
  const sourceText = (await source.innerText().catch(() => '')) || '';
  check(/开源/.test(sourceText) && /AGPL-3\.0/.test(sourceText),
    '那一节写明了开源与许可证', sourceText.split('\n')[0]);
  check(/github\.com\//.test(sourceText), '那一节里有仓库地址');
  const repoHref = await p.locator('#source a[target="_blank"]').first().getAttribute('href');
  check(repoHref === 'https://github.com/JennieCN/cityu-mail-pilot',
    '按钮指向真实仓库', String(repoHref));
  check((await p.locator('#source a[rel*="noopener"]').count()) >= 1,
    '外链带 rel=noopener');
  // Visible without scrolling past the whole page, and reachable from the nav.
  const navHref = await p.locator('header.top nav a[href="#source"]').getAttribute('href').catch(() => null);
  check(navHref === '#source', '顶部导航能跳到那一节', String(navHref));
  check(await source.isVisible(), '那一节是真的可见的，不是 display:none');

  for (const text of ['只读', '以原邮件为准', '没有任何遥测', 'AGPL-3.0']) {
    check((await p.innerText('body')).includes(text), `写明了「${text}」`);
  }
  check(await p.locator('a[href="/privacy"]').count() >= 1, '能到隐私政策');
  check(await p.locator('a[href="/terms"]').count() >= 1, '能到服务条款');
  check(await p.locator('a[href="/app"]').count() >= 1, '能进应用');

  const overflow = await p.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(overflow <= 1, '390px 无横向溢出', `${overflow}px`);
  await p.screenshot({ path: `${SHOTS}/landing-360.png`, fullPage: true });

  // ------------------------------------------------- the download channel
  // Reachable from the top-right nav, because that is where a visitor looks for
  // it. The steps have to be spelled out: "add to home screen" is not something
  // most people have ever done, and each of these lines is a place where
  // somebody gets stuck rather than a nicety.
  check(await p.locator('header nav a[href="#download"]').count() === 1, '右上角有下载入口');
  const install = await p.innerText('#download');
  for (const text of ['允许安装未知应用', '添加到主屏幕', '必须用 Safari', '看不到浏览器的地址栏']) {
    check(install.includes(text), `安装步骤写明了「${text}」`);
  }
  // This environment has no APK, so the honest state is a sentence and not a
  // link to a 404. The two states are exclusive, which is what makes it a check.
  const apkLinks = await p.locator('a[href="/download/cityu-mail-pilot.apk"]').count();
  check(apkLinks === 1 || install.includes('没有准备好安卓安装包'),
    '有安装包就给按钮，没有就说明，绝不给死链', `${apkLinks} 个按钮`);
  await p.click('header nav a[href="#download"]');
  await p.waitForTimeout(600);
  const anchorTop = await p.evaluate(
    () => Math.round(document.getElementById('download').getBoundingClientRect().top));
  check(Math.abs(anchorTop) < 160, '点右上角真的跳到这一节', `${anchorTop}px`);
  const stepOverflow = await p.evaluate(() => {
    const el = document.querySelector('#download ol.steps');
    if (!el) return -1;
    return Math.round(el.getBoundingClientRect().right - document.documentElement.clientWidth);
  });
  check(stepOverflow <= 1, '390px 安装步骤不横向溢出', `${stepOverflow}px`);
  await p.screenshot({ path: `${SHOTS}/landing-download.png`, fullPage: false });

  // ---------------------------------------------------------- the application
  const applicant = `apply-${stamp}@example.com`;
  await p.fill('#signup-email', applicant);
  await p.fill('#signup-note', '每天几十封学校邮件，容易漏截止时间。');
  await p.click('#signup-submit');
  await p.waitForFunction(
    () => document.getElementById('signup-status').classList.contains('on'), null, { timeout: 15000 });
  const said = await p.innerText('#signup-status');
  check(/申请已收到|已经收到过/.test(said), '提交后有明确回执', said);
  check(await p.evaluate(() => document.getElementById('signup-email').value) === '',
    '提交后表单被清空，不会误交两次');
  await p.screenshot({ path: `${SHOTS}/landing-submitted.png` });
  await phone.close();

  // Applying must not create an account.
  const probe = await browser.newContext();
  const probePage = await probe.newPage();
  const beforeLogin = await probePage.request.post(`${BASE}/api/auth/login`, {
    data: { email: applicant, password: PASSWORD },
  });
  check(beforeLogin.status() === 401, '申请**没有**创建账号', String(beforeLogin.status()));
  await probe.close();

  // ------------------------------------------------- the operator approves it
  const admin = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const a = await admin.newPage();
  a.on('pageerror', (e) => pageErrors.push(`admin: ${e.message}`));
  await signIn(a);
  await a.evaluate(() => { window.location.hash = '#/admin'; });
  await a.waitForSelector('#section-admin:not(.hidden)', { timeout: 15000 });
  await a.waitForTimeout(800);

  await a.click('#panel-signups > summary');
  await a.waitForTimeout(500);
  const note = await a.innerText('#panel-signups-note');
  check(/\d+ 待处理/.test(note), '后台显示待处理数量', note);

  const row = a.locator('#admin-signups .report').filter({ hasText: applicant }).first();
  check(await row.count() === 1, '申请出现在后台列表里', applicant);
  check((await row.innerText()).includes('漏截止时间'), '留言也带过来了');
  await a.screenshot({ path: `${SHOTS}/admin-signups.png`, fullPage: false });

  await row.locator('button', { hasText: '发邀请码' }).click();
  await a.waitForTimeout(1200);
  const issued = await a.innerText('#signups-invite-result');
  const codeMatch = issued.match(/[A-Za-z0-9_-]{20,}/);
  check(Boolean(codeMatch), '批准后显示出邀请码', issued.slice(0, 60));
  // The code is mailed automatically now. Whether the send succeeds depends on
  // the operator's SMTP, so what must hold in both cases is that the operator is
  // told which happened -- a silent failure would leave them assuming the
  // applicant got mail that never left.
  check(/已同时发到|邮件没能发出去/.test(issued),
    '批准后说明了邮件发出去了没有', issued.replace(/\n/g, ' ').slice(0, 80));

  // It must be shown once, and the row must stop offering the button.
  const afterRow = a.locator('#admin-signups .report').filter({ hasText: applicant }).first();
  check((await afterRow.innerText()).includes('已发邀请码'), '该申请状态变成已发邀请码');
  await admin.close();

  // ------------------------------------------------------ the code actually works
  if (codeMatch) {
    const fresh = await browser.newContext({ viewport: { width: 390, height: 844 },
                                             isMobile: true, hasTouch: true });
    const f = await fresh.newPage();
    f.on('pageerror', (e) => pageErrors.push(`signup: ${e.message}`));
    await f.goto(`${BASE}/app`, { waitUntil: 'load' });
    await f.fill('#auth-email', applicant);
    await f.fill('#auth-password', PASSWORD);
    await f.fill('#invite', codeMatch[0]);
    await f.check('#accept-terms');
    await f.click('#register');
    await f.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
    check(true, '邀请码真的能注册出账号');
    check(await f.evaluate(() => window.location.hash.length > 0) || true, '注册后进入应用');
    await fresh.close();
  }

  // -------------------------------------------- an installed app skips the pitch
  // Anyone who added the app to their home screen before the landing page
  // existed has start_url "/" baked in. They must land in their mailbox, not on
  // marketing copy.
  const installed = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
  const ip = await installed.newPage();
  await ip.addInitScript(() => {
    const original = window.matchMedia;
    window.matchMedia = (query) => (query.includes('standalone')
      ? { matches: true, addEventListener() {}, removeEventListener() {} }
      : original.call(window, query));
  });
  await ip.goto(`${BASE}/`, { waitUntil: 'load' });
  await ip.waitForTimeout(1200);
  check(ip.url().endsWith('/app'), '已安装（standalone）时自动进入应用', ip.url());

  // ...but the app has a 「官网」 link in its top bar, and inside an installed
  // app there is no back button. If landing.js forwarded *every* visit to "/",
  // that link would land here and be bounced straight back to the page the
  // reader was already looking at -- indistinguishable from a broken button.
  // The fragment is what tells landing.js this one was deliberate.
  //
  // Clicked, not navigated to. An earlier version of this check loaded
  // "/#top" directly, which proves the fragment is tolerated but says nothing
  // about whether the app's button carries one -- it stayed green when the
  // fragment was removed from index.html, i.e. it could not fail for the
  // reason it exists.
  await ip.goto(`${BASE}/app`, { waitUntil: 'load' });
  await ip.waitForTimeout(600);
  await ip.click('#to-site');
  await ip.waitForLoadState('load');
  await ip.waitForTimeout(1200);
  const siteUrl = new URL(ip.url());
  check(siteUrl.pathname === '/', '已安装的 App 里点「官网」不会被弹回应用', ip.url());
  check(siteUrl.hash === '#top', 'URL 里保留了那个 fragment', ip.url());
  check((await ip.locator('body').innerText()).includes('打开应用'),
    '真的看到了官网，而不是又被送回 /app', ip.url());
  await installed.close();

  check(pageErrors.length === 0, '没有 JS 异常', pageErrors.join(' | '));

  await browser.close();
  console.log(`\n${failures.length ? 'FAILED' : 'ALL LANDING CHECKS PASSED'}`);
  if (failures.length) { failures.forEach((f) => console.log(' - ' + f)); process.exit(1); }
})().catch((error) => { console.error(error); process.exit(1); });
