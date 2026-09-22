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

// -- 6. 入场动效（复刻朋友 PR #6 设计稿的滚动动效） --------------------------
// 四件事：标题逐字升起 · 首屏视差 · 揭幕 · 章节标题的滚动驱动。
// 全程**只有一个 rAF**，滚动时一帧只算一次。
//
// 比设计稿多两条安全设计：
//   * `js-motion` 这个类是**跑到这里才加**的，CSS 里的初始隐藏全挂在它下面 ——
//     没有 JS 就什么都不藏（「禁用 JS 后正文仍在」那条检查盯着的正是这个）；
//   * 系统里选了「减少动态效果」就整段不跑，页面就是一张静态页。
(function () {
  if (!window.matchMedia || !window.requestAnimationFrame ||
      !window.IntersectionObserver || !document.querySelector) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var root = document.documentElement;
  root.classList.add('js-motion');

  // 6a. 标题逐字拆分。**只拆文本节点**，<br> 与 <em> 的结构原样保留；
  //     HTML 里那句原话一个字不动（拆出来的 span 对 innerText 透明）。
  var title = document.querySelector('.hero h1');
  if (title) {
    var index = 0;
    var split = function (node) {
      Array.prototype.slice.call(node.childNodes).forEach(function (child) {
        if (child.nodeType === 3) {
          var frag = document.createDocumentFragment();
          child.nodeValue.split('').forEach(function (ch) {
            var span = document.createElement('span');
            span.className = 'ch';
            span.style.setProperty('--i', index++);
            span.textContent = ch;
            frag.appendChild(span);
          });
          node.replaceChild(frag, child);
        } else if (child.nodeType === 1 && child.tagName !== 'BR') {
          split(child);
        }
      });
    };
    split(title);

    var settle = function () { title.classList.add('entered'); };
    var chars = title.querySelectorAll('.ch');
    var tail = chars[chars.length - 1];
    if (tail) tail.addEventListener('animationend', settle);
    // 兜底：animationend 万一不来（被打断、被扩展拦掉），2.5 秒后照样收尾 ——
    // 否则 will-change 会一直挂在每个字上。设计稿没有这一条，是我们加的。
    window.setTimeout(settle, 2500);
  }

  // 6b. 类名由 JS 挂（标记里不写）：容器 `.reveal` 只当触发器，子元素 `.rv`/`.rv-soft` 才是动效。
  //     这样标记里那些被测试钉住的字面量（例如 `class="actions"`）一个字都不用改。
  var hero = document.querySelector('.hero');
  if (hero) hero.classList.add('reveal', 'is-in');
  [
    ['.hero .standfirst', 'rv', 0],
    ['.hero .actions', 'rv', 1],
    ['.hero .pitch', 'rv', 2],
    ['.hero-grid > aside', 'rv-soft', 3],
    ['.marquee', 'rv-soft', 0]
  ].forEach(function (item) {
    var el = document.querySelector(item[0]);
    if (!el) return;
    el.classList.add(item[1]);
    if (item[2]) el.style.setProperty('--i', item[2]);
  });
  Array.prototype.forEach.call(document.querySelectorAll('.section-head'), function (el) {
    el.classList.add('scrub');     // 章节标题走「滚动进度直接驱动」那条
  });

  // 6c. 进视口就揭幕，揭完不再观察。
  //     bento 卡片走他那套**逐个错峰**：同一批里第 n 张延 n × 90ms（一张接一张像波浪），
  //     放完再加 `.settled` 把 transform 的过渡换回悬停那档 —— 否则鼠标移上去要等 0.8 秒。
  var io = new IntersectionObserver(function (entries) {
    var slot = 0;
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      var el = entry.target;
      if (el.classList.contains('b-card')) {
        var delay = slot * 90;
        slot += 1;
        if (delay) el.style.transitionDelay = delay + 'ms';
        window.setTimeout(function () {
          el.style.transitionDelay = '0ms';
          el.classList.add('settled');
        }, delay + 900);
      }
      el.classList.add('is-in');
      io.unobserve(el);
    });
  }, { threshold: 0.12, rootMargin: '0px 0px -10% 0px' });
  Array.prototype.forEach.call(
    document.querySelectorAll('.reveal, .rv, .rv-soft, .b-card'),
    function (el) { io.observe(el); }
  );

  // 6d. 滚动那一帧：视差 + 章节标题 + 顶栏状态 + 当前章节
  var scrubs = Array.prototype.slice.call(document.querySelectorAll('.scrub'));
  var topbar = document.querySelector('header.top');
  var heroCopy = document.querySelector('.hero-copy');
  var heroVisual = document.querySelector('.hero-grid > aside');
  var links = Array.prototype.slice.call(
    document.querySelectorAll('header.top nav a[href^="#"]'));
  var watched = ['how', 'privacy', 'faq', 'apply']
    .map(function (id) { return document.getElementById(id); })
    .filter(Boolean);
  var frame = 0;

  var tick = function () {
    frame = 0;
    var vh = window.innerHeight || 1;

    // 视差：正文与演示卡反向移动（幅度小，免得正文看着晃）
    if (hero && (heroCopy || heroVisual)) {
      var heroRect = hero.getBoundingClientRect();
      var hp = Math.max(0, Math.min(1, -heroRect.top / vh));
      if (heroCopy) heroCopy.style.setProperty('--par', (hp * 12).toFixed(2) + 'px');
      if (heroVisual) heroVisual.style.setProperty('--par', (-hp * 26).toFixed(2) + 'px');
    }

    // 章节标题：进视口渐入、离开渐出，模糊跟着走
    scrubs.forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.bottom < -60 || r.top > vh + 60) return;
      var q = r.top < 140
        ? r.top / 140
        : (r.top > vh * 0.78 ? (1 - r.top / vh) / 0.22 : 1);
      q = Math.max(0, Math.min(1, q));
      var offset = (r.top < 140 ? -1 : 1) * (1 - q) * 24;
      el.style.opacity = q.toFixed(3);
      el.style.transform = 'translate3d(0,' + offset.toFixed(2) + 'px,0)';
      el.style.filter = q > 0.995 ? 'none' : 'blur(' + ((1 - q) * 6).toFixed(2) + 'px)';
    });

    var y = window.pageYOffset || root.scrollTop || 0;
    if (topbar) topbar.classList.toggle('is-scrolled', y > 8);

    // 当前章节 = 最后一个「顶边已经越过探针」的
    var pad = parseFloat(getComputedStyle(root).scrollPaddingTop) || 112;
    var probe = pad + 40;
    var active = null;
    watched.forEach(function (el) {
      if (el.getBoundingClientRect().top <= probe) active = el.id;
    });
    links.forEach(function (a) {
      if (active && a.getAttribute('href') === '#' + active) {
        a.setAttribute('aria-current', 'true');
      } else {
        a.removeAttribute('aria-current');
      }
    });
  };
  var queue = function () { if (!frame) frame = window.requestAnimationFrame(tick); };
  window.addEventListener('scroll', queue, { passive: true });
  window.addEventListener('resize', queue);
  tick();
})();
