/**
 * Landing-page behaviour. External file on purpose: the Content-Security-Policy
 * is `script-src 'self'`, so an inline <script> would be dropped by the browser
 * without any visible error.
 *
 * Two jobs:
 *
 *  1. If this page is already running as an installed app, get out of the way.
 *     `manifest.start_url` used to be "/", so anyone who added the app to their
 *     home screen before the landing page existed has an icon that now opens
 *     marketing copy instead of their mailbox. Detecting standalone display mode
 *     and forwarding to /app fixes those installs without asking anyone to
 *     reinstall — the alternative is a user tapping their mail icon and landing
 *     on an advert.
 *
 *  2. Submit the message-board form without a page reload, falling back to a
 *     normal form POST when scripting is unavailable so the form still works.
 *
 * 2026-09-22: the pilot **application form and the 「没收到邀请码？」 resend form
 * are gone** — registration is open, so there is nothing to apply for and no code
 * to re-send (see `docs/open-registration-2026-09-22.md`). The template no longer
 * renders either one, and their submit handlers were removed with them. The
 * message board, the standalone passthrough, the nav progress bar, the pointer
 * highlight and the drawer below are untouched.
 */
'use strict';

(function () {
  // -- 1. installed-app passthrough --------------------------------------
  try {
    var standalone = window.matchMedia('(display-mode: standalone)').matches
      || window.matchMedia('(display-mode: minimal-ui)').matches
      || window.navigator.standalone === true;
    // A hash means the visitor followed an in-page link; do not steal that.
    if (standalone && !window.location.hash) {
      window.location.replace('/app');
      return;
    }
  } catch (error) { /* matchMedia missing: carry on as a normal page */ }

  // -- 2. the message board ----------------------------------------------
  var guestForm = document.getElementById('guestbook-form');
  if (!guestForm) return;
  // Load-to-submit time. The server treats a submit under three seconds as
  // automated, and it can only know how long the visitor looked at the page if
  // we tell it -- there is no server-side session to time.
  var loadedAt = Date.now();
  var guestStatus = document.getElementById('guestbook-status');
  var guestButton = document.getElementById('guestbook-submit');

  var sayGuest = function (message, kind) {
    guestStatus.textContent = message;
    guestStatus.className = 'on ' + kind;
  };

  guestForm.addEventListener('submit', function (event) {
    var body = (document.getElementById('guestbook-body').value || '').trim();
    if (!body) return; // let the browser's own validation speak
    event.preventDefault();
    guestButton.disabled = true;
    guestButton.textContent = '提交中…';
    fetch('/api/guestbook', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        body: body,
        nickname: (document.getElementById('guestbook-nickname').value || '').trim(),
        email: (document.getElementById('guestbook-email').value || '').trim(),
        website: document.getElementById('guestbook-website').value || '',
        elapsed_ms: Date.now() - loadedAt,
      }),
    }).then(function (response) {
      return response.json().then(function (payload) { return { ok: response.ok, body: payload }; });
    }).then(function (result) {
      if (!result.ok) {
        sayGuest(result.body && result.body.detail ? result.body.detail : '提交失败，请稍后再试。', 'bad');
      } else {
        sayGuest('收到了。我读过之后如果合适，会匿名放到这一页上。', 'ok');
        guestForm.reset();
        loadedAt = Date.now();
      }
    }).catch(function () {
      sayGuest('网络不通，提交失败。请稍后再试。', 'bad');
    }).then(function () {
      guestButton.disabled = false;
      guestButton.textContent = '提交留言';
    });
  });
})();

// -- 3. 导航那条滚动进度（PR #4 设计里的 `.nav-progress`） -------------------
// 纯装饰：找不到那个元素就什么都不做。关掉 JS 时它只是一条空槽，页面照常读。
// 单独一个 IIFE，放在文件最后 —— 上面的表单逻辑里有 `return`，不能挂在它后面。
(function () {
  var bar = document.getElementById('nav-progress');
  if (!bar) return;
  var tick = function () {
    var doc = document.documentElement;
    var max = doc.scrollHeight - window.innerHeight;
    var pct = max > 0 ? (window.scrollY / max) * 100 : 0;
    bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
  };
  window.addEventListener('scroll', tick, { passive: true });
  window.addEventListener('resize', tick);
  tick();
})();

// -- 4. 玻璃的镜面高光（PR #4 设计里的 `.lg-spec`） --------------------------
// 指针位置写进 --gx/--gy，那两个属性用 @property 注册过，所以 transition 能让高光
// **平滑移动**而不是跳变。一帧只写一次（requestAnimationFrame 合并）。
// 用户在系统里选了「减少动态效果」就整段不跑，高光停在静止位置。
(function () {
  if (!window.matchMedia || !window.requestAnimationFrame) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  var GLASS = '.nav-shell, .shell, .b-card';
  var pending = 0;
  var last = null;
  document.addEventListener('pointermove', function (event) {
    var type = event.pointerType;
    if (type && type !== 'mouse' && type !== 'pen') return;   // 手指划过不算
    var el = event.target && event.target.closest ? event.target.closest(GLASS) : null;
    if (!el) return;
    last = { el: el, x: event.clientX, y: event.clientY };
    if (pending) return;
    pending = window.requestAnimationFrame(function () {
      pending = 0;
      if (!last) return;
      var rect = last.el.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      last.el.style.setProperty('--gx', (((last.x - rect.left) / rect.width) * 100).toFixed(1) + '%');
      last.el.style.setProperty('--gy', (((last.y - rect.top) / rect.height) * 100).toFixed(1) + '%');
    });
  }, { passive: true });
})();

// -- 5. 窄屏抽屉（PR #4 设计里的 `.nav-sheet` + 遮罩） ------------------------
// 只有一个开关函数，四个出口：汉堡、遮罩、点链接、Esc。宽屏 resize 时自动收起，
// 免得它留在屏幕上。**没有**照抄他那个 `<button aria-hidden="true">` 的写法 ——
// 一个元素不能既是可以点的按钮又对无障碍树隐藏（审计会点名）。
(function () {
  var burger = document.getElementById('nav-burger');
  var sheet = document.getElementById('nav-sheet');
  var scrim = document.getElementById('nav-scrim');
  if (!burger || !sheet || !scrim) return;
  var timer = 0;
  var setOpen = function (on) {
    burger.setAttribute('aria-expanded', on ? 'true' : 'false');
    burger.setAttribute('aria-label', on ? '关闭菜单' : '打开菜单');
    sheet.classList.toggle('is-open', on);
    scrim.classList.toggle('is-open', on);
    window.clearTimeout(timer);
    if (on) {
      scrim.hidden = false;
    } else {
      timer = window.setTimeout(function () {
        if (!sheet.classList.contains('is-open')) scrim.hidden = true;
      }, 320);
    }
  };
  burger.addEventListener('click', function () {
    setOpen(burger.getAttribute('aria-expanded') !== 'true');
  });
  scrim.addEventListener('click', function () { setOpen(false); });
  sheet.addEventListener('click', function (event) {
    if (event.target && event.target.tagName === 'A') setOpen(false);
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') setOpen(false);
  });
  window.addEventListener('resize', function () {
    if (window.innerWidth >= 900) setOpen(false);
  });
})();
