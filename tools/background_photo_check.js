/* Real-browser check for the user's own background photo.
 *
 *   node tools/background_photo_check.js http://127.0.0.1:PORT /tmp/bg-shots
 *
 * The one assertion worth the whole file is the orientation case. A camera held
 * upright usually stores landscape pixels plus an EXIF flag saying "rotate this
 * 90 degrees". drawImage reads raw pixels and a canvas has no EXIF to carry the
 * flag forward, so a re-encode without createImageBitmap's
 * imageOrientation:'from-image' saves every portrait photo permanently on its
 * side -- and it looks correct in the preview right up until the page is
 * reloaded. Asserting the decoded dimensions of the re-encoded upload is the
 * only way to catch it, because nothing else about the flow looks wrong.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('/tmp/pw/node_modules/playwright');
const { goTo, mintInvite } = require('./nav');

const BASE = process.argv[2] || 'http://127.0.0.1:8899';
const SHOTS = process.argv[3] || '/tmp/bg-shots';
const FIXTURES = path.join(__dirname, '..', 'pilot_app', 'tests', 'fixtures');
const PASSWORD = 'a-long-enough-password';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const ADMIN_PASSWORD = process.env.PILOT_PASSWORD || PASSWORD;
const PHOTO_URL = '/api/appearance/background';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

async function backgroundOf(page) {
  return page.evaluate(() => {
    const layer = document.querySelector('.bg-layer');
    return layer ? getComputedStyle(layer).backgroundImage : '';
  });
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await context.newPage();
  const errors = [];
  const expected = (text) => /^Failed to load resource: the server responded with a status of (400|401|404|422)/.test(text);
  page.on('pageerror', (error) => errors.push(`pageerror: ${error}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !expected(message.text())) errors.push(message.text());
  });

  const invite = await mintInvite(browser, {
    base: BASE, email: ADMIN_EMAIL, password: ADMIN_PASSWORD, label: `bgphoto-${Date.now()}`,
  });
  if (!invite) throw new Error('无法生成邀请码：请确认 PILOT_ADMIN 是管理员账号');

  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', `bgphoto-${Date.now()}@example.com`);
  await page.fill('#auth-password', PASSWORD);
  await page.fill('#invite', invite);
  await page.check('#accept-terms');
  await page.click('#register');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  check(true, '注册并进入后台');

  await goTo(page, 'appearance');
  await page.waitForSelector('#theme-grid .theme-card', { timeout: 10000 });

  check(await page.locator('#bg-photo-input').count() === 1, '外观里有选图入口');
  check(await page.locator('#bg-row .bg-chip').count() === 7, '背景多出「我的照片」一项');
  check(!(await page.isVisible('#bg-photo-remove')), '还没有照片时不显示删除按钮');

  // ---- a normal photo ------------------------------------------------
  await page.setInputFiles('#bg-photo-input', path.join(FIXTURES, 'photo-clean.jpg'));
  await page.waitForSelector('#bg-photo-preview img', { timeout: 15000 });
  const caption = await page.textContent('#bg-photo-preview figcaption');
  check(/1280×720/.test(caption), '普通照片处理成 1280×720', caption);
  check(/相机信息已去除/.test(caption), "预览里说明了元数据已去除", caption);
  check(await page.isEnabled('#bg-photo-use'), '处理好之后「用作背景」可点');

  await page.click('#bg-photo-use');
  await page.waitForFunction(
    () => /背景已保存/.test(document.getElementById('bg-photo-status').textContent || ''),
    { timeout: 15000 }).catch(() => {});
  const status = await page.textContent('#bg-photo-status');
  check(/背景已保存/.test(status), '上传后给出保存回执', status);

  const applied = await backgroundOf(page);
  check(new RegExp(`${PHOTO_URL.replace(/\//g, '\\/')}\\?v=1`).test(applied),
    '页面背景指向这张照片且带版本号', applied);
  const chips = await page.$$eval('#bg-row .bg-chip', (nodes) =>
    nodes.filter((n) => n.classList.contains('on')).map((n) => n.dataset.bg));
  check(chips.length === 1 && chips[0] === 'custom', '「我的照片」被选中', chips.join(','));
  check(await page.isVisible('#bg-photo-remove'), '有照片之后出现删除按钮');
  await page.screenshot({ path: path.join(SHOTS, 'photo-applied-desktop.png') });

  // ---- it survives a reload, because it is on the account -------------
  await page.reload({ waitUntil: 'load' });
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
  await goTo(page, 'appearance');
  await page.waitForTimeout(600);
  check(/\/api\/appearance\/background\?v=1/.test(await backgroundOf(page)), '刷新后背景仍在');
  check(await page.isVisible('#bg-photo-remove'), '刷新后仍知道已有照片');

  // ---- the orientation trap ------------------------------------------
  await page.setInputFiles('#bg-photo-input', path.join(FIXTURES, 'photo-orientation-6.jpg'));
  await page.waitForFunction(
    () => /×/.test((document.querySelector('#bg-photo-preview figcaption') || {}).textContent || ''),
    { timeout: 15000 }).catch(() => {});
  const rotated = await page.textContent('#bg-photo-preview figcaption').catch(() => '');
  // Landscape pixels + orientation 6 must come out portrait. Without
  // imageOrientation:'from-image' this reads 1280×720 and the photo is sideways.
  check(/720×1280/.test(rotated), 'EXIF 方向被烤进像素（旋转后是 720×1280）', rotated);

  // ---- something that is not an image --------------------------------
  await page.setInputFiles('#bg-photo-input', {
    name: 'logo.png',
    mimeType: 'image/png',
    buffer: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
  });
  await page.waitForTimeout(800);
  const rejected = await page.textContent('#bg-photo-status');
  check(rejected && !/正在本机处理/.test(rejected), '伪装成 PNG 的 SVG 被挡下', rejected);

  // ---- deleting ------------------------------------------------------
  await page.click('#bg-photo-remove');
  await page.waitForFunction(
    () => /照片已删除/.test(document.getElementById('bg-photo-status').textContent || ''),
    { timeout: 15000 }).catch(() => {});
  check(/照片已删除/.test(await page.textContent('#bg-photo-status')), '删除后有回执');
  const after = await page.evaluate(async () => {
    const res = await fetch('/api/appearance/background');
    return res.status;
  });
  check(after === 404, '删除后服务端已经没有了', String(after));
  check(!/api\/appearance\/background/.test(await backgroundOf(page)), '删除后页面不再引用它');

  // ---- the phone width ------------------------------------------------
  await page.setViewportSize({ width: 360, height: 780 });
  await page.waitForTimeout(400);
  const overflow = await page.evaluate(() =>
    Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - window.innerWidth);
  check(overflow <= 1, '360px 无横向溢出', overflow > 1 ? `溢出 ${overflow}px` : '');
  await page.screenshot({ path: path.join(SHOTS, 'photo-phone.png') });

  check(errors.length === 0, '没有 JS 异常 / 资源缺失', errors.slice(0, 3).join(' | '));

  await browser.close();
  if (failures.length) {
    console.error(`\n${failures.length} 项不成立：`);
    for (const item of failures) console.error(`  - ${item}`);
    process.exit(1);
  }
  console.log('\n背景照片：全部通过。');
})();
