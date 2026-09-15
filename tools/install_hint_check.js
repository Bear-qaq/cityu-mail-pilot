/* Real-browser check for the "install it on your phone" hint.
 *
 * There is no app store to point at (Apple rejects web wrappers; a new personal
 * Play account cannot publish without twelve testers for fourteen days), so the
 * browser's own install is the whole distribution story. What a screenshot
 * cannot show is that the hint picks the right instructions for the device and
 * then goes away for good — which is the difference between a helper and a nag.
 *
 *   PILOT_ADMIN=boss@example.com node tools/install_hint_check.js http://127.0.0.1:8911 /tmp/install-shots
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('/tmp/pw/node_modules/playwright');

const BASE = process.argv[2] || 'http://127.0.0.1:8911';
const SHOTS = process.argv[3] || '/tmp/install-shots';
const ADMIN_EMAIL = process.env.PILOT_ADMIN || 'boss@example.com';
const PASSWORD = 'a-long-enough-password';

const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 '
  + '(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1';
const ANDROID_UA = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36';

const failures = [];
function check(ok, label, detail) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures.push(label);
}

async function signIn(page) {
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await page.goto(`${BASE}/app`, { waitUntil: 'load' });
  await page.fill('#auth-email', ADMIN_EMAIL);
  await page.fill('#auth-password', PASSWORD);
  await page.click('#login');
  await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
}

const hintVisible = (page) => page.evaluate(
  () => !document.getElementById('install-hint').classList.contains('hidden'));
const hintText = async (page) => {
  // The steps now sit behind a "怎么做" disclosure. Opening it before reading
  // matters: reading textContent would pass even if the summary were not
  // clickable, and the iOS reader is the one who needs those steps.
  await page.evaluate(() => {
    const detail = document.querySelector('#install-hint .install-how');
    if (detail) detail.open = true;
  });
  return page.evaluate(() => document.getElementById('install-hint').innerText);
};

async function contextFor(browser, { userAgent, width = 390, height = 844 }) {
  return browser.newContext({ viewport: { width, height }, userAgent,
                              isMobile: true, hasTouch: true });
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  const pageErrors = [];

  // -- iPhone: Safari has no install API, so the steps have to be spelled out
  const iphone = await contextFor(browser, { userAgent: IPHONE_UA });
  const iosPage = await iphone.newPage();
  iosPage.on('pageerror', (error) => pageErrors.push(`ios: ${error.message}`));
  await signIn(iosPage);
  await iosPage.waitForTimeout(400);
  check(await hintVisible(iosPage), 'iPhone 上显示安装引导');
  const iosText = await hintText(iosPage);
  check(iosText.includes('添加到主屏幕'), 'iPhone 上给出「添加到主屏幕」步骤', iosText.slice(0, 60));
  check(!iosText.includes('安装应用'), 'iPhone 上不给安卓的菜单说法');
  await iosPage.screenshot({ path: path.join(SHOTS, 'install-ios.png') });

  // Dismissing must stick across a reload; a hint that returns every visit is
  // a nag.
  await iosPage.click('#install-dismiss');
  await iosPage.waitForTimeout(300);
  check(!(await hintVisible(iosPage)), '点「以后再说」后立刻隐藏');
  await iosPage.reload({ waitUntil: 'load' });
  await iosPage.waitForTimeout(600);
  check(!(await hintVisible(iosPage)), '重新加载后仍然隐藏');
  await iphone.close();

  // -- Android: the menu wording differs and Chrome may offer a real prompt
  const android = await contextFor(browser, { userAgent: ANDROID_UA, width: 360 });
  const androidPage = await android.newPage();
  androidPage.on('pageerror', (error) => pageErrors.push(`android: ${error.message}`));
  await signIn(androidPage);
  await androidPage.waitForTimeout(400);
  check(await hintVisible(androidPage), '安卓上显示安装引导');
  const androidText = await hintText(androidPage);
  check(androidText.includes('安装'), '安卓上给出安装说法', androidText.slice(0, 60));
  const width = await androidPage.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  check(width.scrollWidth <= width.clientWidth + 1,
        '360px 宽下引导不造成横向溢出', JSON.stringify(width));
  await androidPage.screenshot({ path: path.join(SHOTS, 'install-android.png') });

  // -- already installed: the hint must not appear at all
  const installed = await androidPage.evaluate(() => {
    const real = window.matchMedia;
    window.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} });
    try { return isInstalled(); } finally { window.matchMedia = real; }
  });
  check(installed === true, 'display-mode 为 standalone 时判定为「已安装」');
  await android.close();

  // -- desktop is still a legitimate install target (Chrome/Edge)
  const desktop = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const desktopPage = await desktop.newPage();
  desktopPage.on('pageerror', (error) => pageErrors.push(`desktop: ${error.message}`));
  await signIn(desktopPage);
  await desktopPage.waitForTimeout(400);
  check(await hintVisible(desktopPage), '桌面浏览器也给出安装引导');
  await desktop.close();

  await browser.close();
  check(pageErrors.length === 0, '没有 JS 异常', pageErrors.slice(0, 3).join(' | '));
  console.log(failures.length
    ? `\nFAILED (${failures.length}): ${failures.join('; ')}`
    : '\nALL INSTALL-HINT CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('check crashed:', error);
  process.exit(2);
});
