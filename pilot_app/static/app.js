/* CityU Mail Pilot — "clear action" front end.
 *
 * Three rules drive this file:
 *   1. The home screen answers "what is my next step?" before anything else.
 *   2. Status chips report what we actually verified, never what we assume.
 *   3. Every value that comes from the server or an email is inserted with
 *      textContent / createElement, never as raw HTML.
 */
'use strict';

const $ = (id) => document.getElementById(id);
let state = null;
let catalog = null;
let dash = null;
let activeSection = '';

/* ------------------------------------------------------------------ utils */

async function api(path, options = {}) {
  const { raw, contentType, headers, ...rest } = options;
  // `raw` is the one request whose body is bytes rather than JSON: the background
  // photo. Passing a Blob through JSON.stringify would send the two characters
  // "{}" and the server would reject a body it never actually received.
  const res = await fetch(path, {
    ...rest,
    body: raw !== undefined ? raw : rest.body,
    headers: {
      'Content-Type': raw !== undefined
        ? (contentType || 'application/octet-stream')
        : 'application/json',
      ...(headers || {}),
    },
  });
  let body = {};
  try { body = await res.json(); } catch (_) { body = {}; }
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

function setStatus(id, text, kind = '') {
  const node = $(id);
  if (!node) return;
  node.className = text ? `status${kind ? ' ' + kind : ''}` : 'status';
  node.textContent = text || '';
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) { if (node) node.textContent = ''; }

/* ------------------------------------------------------------------ time */

/* Every timestamp the server sends is a UTC ISO string. It used to be shown by
 * slicing the string — `created_at.slice(0, 16)` — which prints UTC while
 * looking exactly like a local time. A Hong Kong reader saw 10:18 for a mail
 * that arrived at 18:18, and the same message appeared eight hours apart in two
 * panels of the same app.
 *
 * `Intl.DateTimeFormat` with an explicit timeZone is the whole fix: no library,
 * no hand-rolled offset arithmetic, and it follows DST for zones that have it.
 */

function userTimezone() {
  const zone = state && state.profile && state.profile.timezone;
  return zone || 'Asia/Hong_Kong';
}

/** Short label like "GMT+8", so a reader never has to guess whose clock this is. */
function zoneLabel() {
  const zone = userTimezone();
  try {
    const parts = new Intl.DateTimeFormat('en-US',
      { timeZone: zone, timeZoneName: 'shortOffset' }).formatToParts(new Date());
    const found = parts.find((part) => part.type === 'timeZoneName');
    return found ? found.value : zone;
  } catch (error) {
    return zone;
  }
}

/**
 * Format a server timestamp in the reader's own timezone.
 *
 * `withZone` appends "GMT+8". Use it for the admin console, where the times
 * belong to other people and an unlabelled clock is ambiguous; the reader's own
 * screens need no label, the same way a phone's clock has none.
 */
function momentText(value, { seconds = false, withZone = false, fallback = '—' } = {}) {
  if (!value) return fallback;
  const parsed = new Date(String(value));
  if (Number.isNaN(parsed.getTime())) return String(value);
  const zone = userTimezone();
  let day;
  let clock;
  try {
    day = new Intl.DateTimeFormat('zh-CN', { timeZone: zone, month: 'long', day: 'numeric' }).format(parsed);
    clock = new Intl.DateTimeFormat('zh-CN', {
      timeZone: zone, hour: '2-digit', minute: '2-digit', hour12: false,
      ...(seconds ? { second: '2-digit' } : {}),
    }).format(parsed);
  } catch (error) {
    return String(value);
  }
  return `${day} ${clock}${withZone ? ` (${zoneLabel()})` : ''}`;
}

/* ---------------------------------------------------------------- notices */

/* Clicking "刷新" used to leave no trace at all. The list silently changed, or
 * silently did not, and a slow failure looked exactly like a fast success —
 * there are six such buttons across three modules and none of them had an
 * inline place to report back to. So the answer lives in one place instead.
 *
 * Callers opt in with `notify: true`. Background callers must not: the metrics
 * panel reloads every three seconds and the mail board reloads on every filter
 * change, and a notice for something the user did not ask for is not feedback,
 * it is a stream of noise.
 */
const TOAST_MS = { ok: 2600, info: 2600, warn: 5000, error: 7000 };
const TOAST_MAX = 3;

function toastHost() {
  let host = $('toasts');
  if (!host) {
    host = el('div', 'toasts');
    host.id = 'toasts';
    // polite, not assertive: this confirms something the user just did, so it
    // should be announced after the current utterance rather than interrupt it.
    host.setAttribute('role', 'status');
    host.setAttribute('aria-live', 'polite');
    document.body.appendChild(host);
  }
  return host;
}

function toast(text, kind = 'ok') {
  const host = toastHost();
  const node = el('div', `toast ${kind}`, text);
  node.title = '点击关闭';
  const dismiss = () => { if (node.parentNode) node.parentNode.removeChild(node); };
  node.addEventListener('click', dismiss);
  host.appendChild(node);
  // Three stacked confirmations is noise, not feedback: drop the oldest.
  while (host.children.length > TOAST_MAX) host.removeChild(host.firstChild);
  setTimeout(dismiss, TOAST_MS[kind] || TOAST_MS.info);
  return node;
}

function list(value) {
  return String(value || '').split(/[,，]/).map((item) => item.trim()).filter(Boolean);
}

/** Minimal, escaping markdown renderer for stored reports (bold + bullets). */
function renderMarkdown(container, markdown) {
  clear(container);
  let currentList = null;
  String(markdown || '').split('\n').forEach((raw) => {
    const line = raw.trim();
    if (!line) return;
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    const bullet = /^(?:[-*•·]|\d+[.)、])\s+(.*)$/.exec(line);
    if (heading) {
      currentList = null;
      container.appendChild(el('h4', null, heading[1]));
      return;
    }
    const text = bullet ? bullet[1] : line;
    const node = el('li');
    text.split(/(\*\*[^*]+\*\*)/).forEach((part) => {
      const bold = /^\*\*([^*]+)\*\*$/.exec(part);
      if (bold) node.appendChild(el('b', null, bold[1]));
      else node.textContent += part;
    });
    if (bullet) {
      if (!currentList) {
        currentList = el('ul');
        container.appendChild(currentList);
      }
      currentList.appendChild(node);
    } else {
      currentList = null;
      const paragraph = el('p');
      paragraph.textContent = text.replace(/\*\*/g, '');
      container.appendChild(paragraph);
    }
  });
}

/* ------------------------------------------------------------- boot / auth */

// The single source of navigation. Every surface below reads this list, so a
// destination that is hidden or added appears consistently everywhere instead
// of needing four edits. `primary` decides what earns a slot in the phone's
// bottom bar; everything else lives behind "更多". Material's navigation bar
// and iOS's tab bar both top out around five items, so the bar renders four
// primaries plus "更多" rather than squeezing a sixth label to nothing.
const NAV = [
  { key: 'dashboard', label: '首页', title: '首页', primary: true },
  { key: 'mailbox', label: '邮箱', title: '邮箱设置', primary: true },
  { key: 'model', label: '模型', title: 'AI 模型', primary: true },
  { key: 'reports', label: '报告', title: '报告与账户', primary: true },
  { key: 'profile', label: '个人资料', title: '个人资料' },
  { key: 'appearance', label: '外观', title: '外观' },
  { key: 'security', label: '账户安全', title: '账户安全' },
  { key: 'search', label: '联网搜索', title: '联网搜索（可选）' },
  { key: 'admin', label: '管理后台', title: '管理后台', adminOnly: true },
];

function navItems() {
  const isAdmin = Boolean(state && state.is_admin);
  return NAV.filter((item) => !item.adminOnly || isAdmin);
}

function navItem(key) {
  return navItems().find((item) => item.key === key) || null;
}

// Inline SVG rather than emoji: emoji render as a different picture on every
// platform, ignore the theme's text colour, and cannot show which tab is
// active. These inherit currentColor, so the active tab colours them for free,
// and they cost no extra request.
const TAB_ICONS = {
  dashboard: 'M3 10.6 12 3.2l9 7.4V20a1 1 0 0 1-1 1h-5.2v-6.2H9.2V21H4a1 1 0 0 1-1-1z',
  mailbox: 'M3.5 5.8h17v12.4h-17zM3.5 6.6l8.5 5.7 8.5-5.7',
  model: 'M12 3.4v3.4M12 17.2v3.4M3.4 12h3.4M17.2 12h3.4M6.4 6.4l2.4 2.4M15.2 15.2l2.4 2.4M17.6 6.4l-2.4 2.4M8.8 15.2l-2.4 2.4',
  reports: 'M6.4 2.6h7.4l3.8 3.8v15H6.4zM13.8 2.6v3.8h3.8M9.4 12.4h5.2M9.4 16.4h5.2',
  more: 'M6.2 12h.02M12 12h.02M17.8 12h.02',
};

function navIcon(key) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '22');
  svg.setAttribute('height', '22');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', TAB_ICONS[key] || TAB_ICONS.reports);
  svg.appendChild(path);
  return svg;
}

function navButton(item, { withIcon = false } = {}) {
  const button = el('button');
  button.type = 'button';
  button.dataset.section = item.key;
  if (withIcon) {
    const icon = el('span', 'tab-icon');
    icon.appendChild(navIcon(item.key));
    button.appendChild(icon);
    button.appendChild(el('span', 'tab-label', item.label));
  } else {
    button.textContent = item.label;
  }
  button.addEventListener('click', () => openSection(item.key));
  return button;
}

function renderNav() {
  const items = navItems();
  const current = navItem(activeSection) ? activeSection : 'dashboard';

  const sidebar = $('sidebar-nav');
  if (sidebar) {
    clear(sidebar);
    items.forEach((item) => {
      const li = el('li');
      li.appendChild(navButton(item));
      sidebar.appendChild(li);
    });
  }

  const drawerNav = $('drawer-nav');
  if (drawerNav) {
    clear(drawerNav);
    items.forEach((item) => {
      const li = el('li');
      li.appendChild(navButton(item));
      drawerNav.appendChild(li);
    });
  }

  const tabbar = $('tabbar');
  if (tabbar) {
    clear(tabbar);
    items.filter((item) => item.primary).forEach((item) => {
      tabbar.appendChild(navButton(item, { withIcon: true }));
    });
    // "更多" is the fifth slot and the only way to reach the rest on a phone.
    const more = el('button');
    more.type = 'button';
    more.id = 'tab-more';
    const moreIcon = el('span', 'tab-icon');
    moreIcon.appendChild(navIcon('more'));
    more.appendChild(moreIcon);
    more.appendChild(el('span', 'tab-label', '更多'));
    more.addEventListener('click', () => setDrawer(true));
    tabbar.appendChild(more);
  }

  document.querySelectorAll('.navlist button, .tabbar button').forEach((button) => {
    const key = button.dataset.section;
    const on = key === current;
    button.classList.toggle('active', on);
    if (on) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });

  const sub = $('sidebar-sub');
  if (sub && state) sub.textContent = state.user.email;
}

function setDrawer(open) {
  const drawer = $('drawer');
  const scrim = $('drawer-scrim');
  if (!drawer) return;
  drawer.hidden = !open;
  if (scrim) scrim.classList.toggle('hidden', !open);
  document.body.classList.toggle('drawer-open', open);
  if (open) {
    const first = drawer.querySelector('button');
    if (first) first.focus();
  }
}

function showDashboard(on) {
  $('auth').classList.toggle('hidden', on);
  $('dashboard').classList.toggle('hidden', !on);
  $('logout').classList.toggle('hidden', !on);
  const sidebar = $('sidebar');
  if (sidebar) sidebar.hidden = !on;
  const tabbar = $('tabbar');
  if (tabbar) tabbar.hidden = !on;
  if (!on) setDrawer(false);
  // Drop the previous account's task text from memory on sign-out; the next
  // login refetches it anyway.
  if (!on) taskView = null;
}

async function load() {
  try {
    state = await api('/api/me');
    catalog = await api('/api/catalog');
  } catch (_) {
    showDashboard(false);
    return;
  }
  showDashboard(true);
  renderNav();
  renderSourceLink();
  $('pause').classList.toggle('hidden', state.user.status !== 'active');
  $('resume').classList.toggle('hidden', state.user.status === 'active');
  fill();
  await refreshDashboard();
  // The hash is the source of truth on a cold load, so a bookmark or a refresh
  // lands on the same screen instead of always resetting to the first one.
  openSection(sectionFromHash(), { updateHash: false });
}

/* The footer's link to the published source, when the operator configured one.
 *
 * AGPL-3.0 section 13: running this as a network service obliges the operator to
 * offer that source to the people using it, so the link belongs where users can
 * see it rather than only in a file they will never open. It is rendered here
 * instead of in index.html because the shell is a static file and the URL is
 * per-installation; an installation without one shows nothing rather than a link
 * to somebody else's repository.
 */
function renderSourceLink() {
  const slot = $('source-link');
  if (!slot) return;
  const url = (state && state.source_url) || '';
  clear(slot);
  // Only http(s), and only ever as a link built by us: the value comes from the
  // server's environment, but a `javascript:` URL would still run in the
  // browser of anyone reading the footer.
  if (!/^https?:\/\//i.test(url)) return;
  slot.appendChild(document.createTextNode(' · '));
  const link = el('a', null, '源代码');
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener';
  slot.appendChild(link);
}

async function refreshDashboard({ notify = false } = {}) {
  try {
    // Stay on whatever day the user is browsing; only a fresh page load starts
    // at today. Both are fetched together so the counters and the list cannot
    // disagree for a frame.
    const day = taskView && !taskView.is_today ? taskView.day : '';
    const [dashboardData, view] = await Promise.all([
      api('/api/dashboard'),
      api(day ? `/api/tasks/day/${day}` : '/api/tasks'),
    ]);
    dash = dashboardData;
    taskView = view;
    renderDashboard();
    if (notify) toast('状态已刷新', 'ok');
  } catch (error) {
    setStatus('status-note', `无法读取状态：${error.message}`, 'error');
    if (notify) toast(`刷新状态失败：${error.message}`, 'error');
  }
}

/* --------------------------------------------------------------- dashboard */

const PROGRESS_STEPS = ['学校邮箱和专业', '私人转发邮箱', 'AI 模型', '联网搜索或自带搜索'];

function progressCount() {
  const profile = state.profile || {};
  const done = [
    Boolean(profile.school_email && profile.major),
    Boolean(state.mailbox),
    Boolean(state.connections.model),
    Boolean(state.connections.search || modelHasNativeSearch()),
  ];
  return done.filter(Boolean).length;
}

function renderProgress() {
  const done = progressCount();
  // A finished checklist is not status, it is clutter — and on a phone it used
  // to hold a whole card above the fold, above the thing the reader came for.
  const card = $('progress-card');
  if (card) card.classList.toggle('hidden', done >= PROGRESS_STEPS.length);
  $('progress').style.width = `${(done / PROGRESS_STEPS.length) * 100}%`;
  $('progress-note').textContent = done === PROGRESS_STEPS.length
    ? '设置已完成：新邮件会自动生成摘要。'
    : `已完成 ${done}/${PROGRESS_STEPS.length} 项设置。`;
}

const CHANNEL_STATE_TEXT = {
  ok: '正常', error: '需要处理', missing: '未设置',
  stale: '待复查', unknown: '待检查', optional: '可跳过',
};

/**
 * Mark the two key sections as skippable when the pilot provides the credential.
 *
 * `showConnectionState()` (below) already explains the situation inside each
 * section, so this adds nothing but a three-word marker in the heading -- which
 * is the thing a user sees *before* deciding whether this page is a step they
 * still owe. The first attempt also wrote a second status line into
 * `#model-status` / `#search-status`; the screenshot showed two boxes saying the
 * same thing, so that half was removed -- along with those two empty divs, since
 * an unused status element is an invitation for the next person to fill it in and
 * recreate the duplication.
 */
function renderKeySkipNotes() {
  if (!state) return;
  const connections = state.connections || {};
  [['model', 'model-skip-note'], ['search', 'search-skip-note']].forEach(([kind, noteId]) => {
    const note = $(noteId);
    if (!note) return;
    const mine = connections[kind] || {};
    note.textContent = mine.platform ? '（管理员已提供，可跳过）' : '';
  });
}

function renderChannels() {
  const box = $('channels');
  clear(box);
  if (!dash) return;
  ['mailbox', 'model', 'search', 'digest'].forEach((key) => {
    const item = dash.channels[key];
    const card = el('div', `channel ${item.state}`);
    const head = el('div', 'spread');
    head.appendChild(el('strong', null, item.label));
    head.appendChild(el('span', 'dot', CHANNEL_STATE_TEXT[item.state] || item.state));
    card.appendChild(head);
    card.appendChild(el('div', 'help', item.detail));
    box.appendChild(card);
  });
  if (dash.send_error) {
    setStatus('status-note', `上次收信或发信出错：${dash.send_error}`, 'error');
  } else {
    setStatus('status-note', '');
  }
  renderKeySkipNotes();
}

/* ----------------------------------------------------------- install hint */

/* "Download our app" is not a thing we can offer. Apple rejects web wrappers
 * under guideline 4.2 ("Websites served in an iOS app ... do not make a quality
 * app"), and a new personal Google Play account cannot publish until twelve
 * testers have stayed opted in for fourteen consecutive days.
 *
 * What *is* available today, on both platforms and at no cost, is the browser's
 * own install: Chrome produces a real WebAPK that appears in the app drawer and
 * the app switcher, and Safari puts a standalone web app on the home screen.
 * Neither needs a store, a developer account or a fee.
 *
 * The catch is that the two platforms do it differently and neither advertises
 * it, so this card names the steps for the browser actually in the reader's
 * hand — once, and then stays out of the way if they decline.
 */
const INSTALL_DISMISSED_KEY = 'install-hint-dismissed';
let installPromptEvent = null;

function isInstalled() {
  return window.matchMedia('(display-mode: standalone)').matches
    || window.matchMedia('(display-mode: fullscreen)').matches
    || window.navigator.standalone === true;
}

function isIOS() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    // iPadOS 13+ claims to be a Mac; touch points give it away.
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function renderInstallHint() {
  const box = $('install-hint');
  if (!box) return;
  let dismissed = false;
  try { dismissed = localStorage.getItem(INSTALL_DISMISSED_KEY) === '1'; } catch (error) { /* private mode */ }
  if (dismissed || isInstalled()) {
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');
  const title = $('install-title');
  const steps = $('install-steps');
  const actions = $('install-actions');
  clear(steps);
  clear(actions);

  if (isIOS()) {
    title.textContent = '加到手机主屏幕';
    steps.textContent = 'Safari 里点底部的「分享」按钮，往下找「添加到主屏幕」，再点「添加」。'
      + '之后它会像 App 一样全屏打开，不用每次找网址。';
    return;
  }

  title.textContent = '装到这台设备';
  if (installPromptEvent) {
    steps.textContent = '装成应用后可以从桌面图标直接打开，不必先开浏览器。';
    const button = el('button', null, '立即安装');
    button.addEventListener('click', async () => {
      const pending = installPromptEvent;
      installPromptEvent = null;
      try {
        pending.prompt();
        const choice = await pending.userChoice;
        toast(choice && choice.outcome === 'accepted' ? '已开始安装' : '好的，随时可以再装',
              choice && choice.outcome === 'accepted' ? 'ok' : 'info');
      } catch (error) { /* the prompt can only be used once */ }
      renderInstallHint();
    });
    actions.appendChild(button);
    return;
  }
  steps.textContent = '在浏览器菜单里选「安装应用」或「添加到主屏幕」即可；'
    + '桌面版 Chrome / Edge 也可以点地址栏右侧的安装图标。';
}

window.addEventListener('beforeinstallprompt', (event) => {
  // Chrome fires this when the manifest is installable. Holding the event lets
  // the button raise the real prompt instead of sending people hunting menus.
  event.preventDefault();
  installPromptEvent = event;
  renderInstallHint();
});

window.addEventListener('appinstalled', () => {
  installPromptEvent = null;
  const box = $('install-hint');
  if (box) box.classList.add('hidden');
});

const ANNOUNCEMENT_LABEL = { info: '通知', warn: '提醒', critical: '重要' };

function renderAnnouncement() {
  const box = $('announcement');
  if (!box) return;
  const item = dash && dash.announcement;
  clear(box);
  if (!item) {
    box.classList.add('hidden');
    return;
  }
  box.className = `announce ${item.tone || 'info'}`;
  box.appendChild(el('div', 'tag', `全体广播 · ${ANNOUNCEMENT_LABEL[item.tone] || '通知'} · ${item.created_display || ''}`));
  box.appendChild(el('h3', null, item.title));
  box.appendChild(el('p', null, item.body));
  const foot = el('div', 'foot');
  foot.appendChild(el('div', 'help', '这条消息由试点管理员发出。'));
  const dismiss = el('button', 'secondary', '我知道了');
  dismiss.type = 'button';
  dismiss.addEventListener('click', async () => {
    dismiss.disabled = true;
    try {
      await api(`/api/announcements/${encodeURIComponent(item.id)}/dismiss`, { method: 'POST' });
      if (dash) dash.announcement = null;
      renderAnnouncement();
    } catch (error) {
      dismiss.disabled = false;
      setStatus('status-note', `暂时无法关闭这条公告：${error.message}`, 'error');
    }
  });
  foot.appendChild(dismiss);
  box.appendChild(foot);
}

function renderHero() {
  const hero = $('hero');
  const step = dash.next_step;
  const tone = step.tone === 'warn' ? ' warn' : '';
  hero.className = `hero card${step.kind === 'done' ? ' done' : ''}${tone}`;
  clear(hero);
  const kicker = step.kind === 'done' ? '当前状态' : (tone ? '需要你确认' : '你的下一步');
  hero.appendChild(el('div', 'kicker', kicker));
  hero.appendChild(el('h2', null, step.title));
  hero.appendChild(el('p', null, step.detail));
  const button = el('button', null, step.action);
  button.type = 'button';
  button.addEventListener('click', () => handleNextStep(step.kind));
  hero.appendChild(button);
}

function handleNextStep(kind) {
  if (kind === 'verify') { verifyMailbox(); return; }
  if (kind === 'task') {
    document.getElementById('tasks').scrollIntoView({ behavior: 'smooth', block: 'center' });
    return;
  }
  if (kind === 'done') { openSection('reports'); return; }
  openSection(kind);
}

function renderTaskSummary() {
  const box = $('metrics');
  clear(box);
  const today = dash.today;
  // "需要行动" counts only what is still open. It reads the task view rather
  // than the dashboard payload when one is loaded, so ticking a task off updates
  // the number immediately instead of at the next poll.
  const open = (taskView && taskView.is_today) ? taskView.counts.open : today.tasks;
  [
    ['今日邮件', `${today.messages} 封`],
    ['需要行动', `${open} 件`],
    ['失败/异常', `${today.failed} 封`],
    ['下次简报', dash.next_run_display],
  ].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    box.appendChild(cell);
  });
}

/* -------------------------------------- daily tasks: hide one, find it again */

// The task card renders from this, not from `dash`: today comes from
// /api/tasks and an earlier day from /api/tasks/day/<day>, so one code path
// draws both. `day` is always sent back with a change so the reply is the view
// the user is actually looking at, including when they are browsing the past.
let taskView = null;

function taskActions(task, mode) {
  const wrap = el('div', 'task-item-actions');
  const button = el('button', 'secondary', mode === 'done' ? '✓ 处理好了' : '恢复');
  button.dataset.taskKey = task.task_key;
  button.dataset.taskState = mode === 'done' ? 'done' : 'open';
  if (mode === 'done') button.title = '从今天列表里收起，任务不会被删除';
  button.addEventListener('click', () => markTask(task.task_key, button.dataset.taskState, button));
  wrap.appendChild(button);
  return wrap;
}

function taskItem(task, mode) {
  const item = el('li');
  const head = el('div', 'task-head');
  const badges = el('div', 'task-badges');
  badges.appendChild(el('span', `pill ${task.priority}`,
    task.priority === 'high' ? '重要' : task.priority === 'medium' ? '一般' : '低'));
  if (task.deadline) badges.appendChild(el('span', 'pill deadline', `截止 ${task.deadline}`));
  head.appendChild(badges);
  head.appendChild(taskActions(task, mode));
  item.appendChild(head);
  item.appendChild(el('div', 'task-action', task.action));
  const source = task.received_display
    ? `来自「${task.subject}」 · ${task.sender || '未知发件人'} · ${task.received_display}`
    : `来自「${task.subject}」 · ${task.sender || '未知发件人'}`;
  item.appendChild(el('div', 'help', source));
  if (task.done_at) {
    item.appendChild(el('div', 'help', `处理于 ${momentText(task.done_at)}`));
  }
  if (task.archived) {
    item.appendChild(el('div', 'task-archived', '原始邮件已不在库里，这条是按记录保留的。'));
  }
  return item;
}

function renderTasks() {
  const view = taskView;
  const list = $('tasks');
  clear(list);
  if (!view) return;

  const label = $('task-day-label');
  label.textContent = view.is_today ? `今天 · ${view.day}` : `${view.day} 的清单`;
  $('task-back-today').classList.toggle('hidden', view.is_today);

  if (!view.tasks.length) {
    list.appendChild(el('li', 'muted', view.is_today
      ? (dash.today.immediate_enabled
        ? '今天还没有需要你处理的邮件。'
        : '即时摘要已暂停；恢复后新邮件会自动生成报告。')
      : '这一天没有未处理的任务了。'));
  } else {
    view.tasks.forEach((task) => list.appendChild(taskItem(task, 'done')));
  }

  const doneList = $('tasks-done');
  clear(doneList);
  const doneNote = $('tasks-done-note');
  doneNote.textContent = view.counts.done ? `${view.counts.done} 件` : '暂无';
  if (!view.done.length) {
    doneList.appendChild(el('li', 'muted', '还没有处理过的任务。点「✓ 处理好了」就会收进这里。'));
  } else {
    view.done.forEach((task) => doneList.appendChild(taskItem(task, 'open')));
  }

  const history = $('tasks-history');
  clear(history);
  const days = view.days || [];
  $('tasks-history-note').textContent = days.length ? `${days.length} 天` : '暂无';
  if (!days.length) {
    history.appendChild(el('p', 'help', '还没有处理记录。处理过任务之后，这里会按天列出。'));
    return;
  }
  days.forEach((row) => {
    const button = el('button', `history-day${row.day === view.day ? ' current' : ''}`);
    button.appendChild(document.createTextNode(row.day));
    button.appendChild(el('span', 'count', `已处理 ${row.done}/${row.total}`));
    button.addEventListener('click', () => loadTasksFor(row.day));
    history.appendChild(button);
  });
}

async function loadTasksFor(day) {
  try {
    taskView = await api(day ? `/api/tasks/day/${day}` : '/api/tasks');
    renderTasks();
    renderTaskSummary();
  } catch (error) {
    setStatus('status-note', `无法读取任务：${error.message}`, 'error');
  }
}

async function markTask(key, state, button) {
  const day = taskView ? taskView.day : '';
  button.disabled = true;
  button.textContent = state === 'done' ? '正在收起…' : '正在恢复…';
  try {
    taskView = await api(`/api/tasks/${key}`, {
      method: 'PUT',
      body: JSON.stringify({ state, day }),
    });
    renderTasks();
    renderTaskSummary();
    toast(state === 'done'
      ? '已收起。可在「已处理」里找回来'
      : '已放回待处理列表', 'ok');
  } catch (error) {
    button.disabled = false;
    button.textContent = state === 'done' ? '✓ 处理好了' : '恢复';
    toast(`${state === 'done' ? '收起' : '恢复'}失败：${error.message}`, 'error');
  }
}

function renderRecent() {
  const box = $('recent');
  clear(box);
  if (!dash.recent.length) {
    box.appendChild(el('li', 'muted', '今天还没有生成摘要。'));
    return;
  }
  dash.recent.forEach((row) => {
    const item = el('li');
    item.appendChild(el('div', 'task-action', row.subject));
    item.appendChild(el('div', 'help', `${row.sender || '未知发件人'} · ${row.received_display} · ${row.priority === 'high' ? '重要' : row.priority === 'low' ? '低优先级' : '一般'}`));
    box.appendChild(item);
  });
}

function renderDashboard() {
  renderAnnouncement();
  renderInstallHint();
  $('app-sub').textContent = `${dash.local_display} · ${state.user.email}`;
  renderProgress();
  renderHero();
  renderChannels();
  renderTaskSummary();
  renderTasks();
  renderRecent();
  showConnectionState();
}

/* --------------------------------------------------------------- sections */

function sectionFromHash() {
  let key = '';
  try { key = decodeURIComponent((location.hash || '').replace(/^#\/?/, '')).trim(); } catch (_) { key = ''; }
  return navItem(key) ? key : 'dashboard';
}

function openSection(name, { updateHash = true } = {}) {
  // An unknown key — a stale bookmark, or #/admin for a non-admin — falls back
  // to the dashboard rather than showing an empty screen.
  const key = navItem(name) ? name : 'dashboard';
  activeSection = key;

  const home = $('view-dashboard');
  if (home) home.classList.toggle('hidden', key !== 'dashboard');
  ['profile', 'appearance', 'security', 'mailbox', 'model', 'search', 'reports', 'admin'].forEach((other) => {
    const node = $(`section-${other}`);
    if (node) node.classList.toggle('hidden', other !== key);
  });

  const title = $('app-title');
  if (title) title.textContent = (navItem(key) || {}).title || 'CityU Mail Pilot';

  renderNav();
  setDrawer(false);
  // Switching used to leave the reader halfway down a page whose length had
  // just changed, which reads as the app jumping at random.
  window.scrollTo(0, 0);

  if (updateHash && location.hash !== `#/${key}`) location.hash = `#/${key}`;

  if (key === 'model' || key === 'search') renderKeySkipNotes();
  if (key === 'reports') loadReports();
  // The session list is about the account, not about reports, so it loads with
  // its own section -- opening the reports tab should not silently fetch it.
  if (key === 'security') loadSecurity();
  if (key === 'admin') { loadAdmin(); startMetrics(); } else { stopMetrics(); }
}

// Back/forward, a refresh and a pasted bookmark all arrive here. The guard
// avoids re-running a section's loaders for a hash we just wrote ourselves.
window.addEventListener('hashchange', () => {
  const key = sectionFromHash();
  // A hash that does not name a reachable section — #/admin for a non-admin, or
  // a stale bookmark — is corrected in place. Leaving it would put the address
  // bar and the screen in disagreement, and the next refresh would "jump".
  // replaceState rather than location.hash so the correction does not become a
  // history entry the user has to press Back through.
  const raw = (location.hash || '').replace(/^#\/?/, '');
  if (key !== raw) {
    try { history.replaceState(null, '', `#/${key}`); } catch (_) { /* file:// */ }
    if (key !== activeSection) openSection(key, { updateHash: false });
    return;
  }
  if (key !== activeSection) openSection(key, { updateHash: false });
});

const drawerCloseButton = $('drawer-close');
if (drawerCloseButton) drawerCloseButton.addEventListener('click', () => setDrawer(false));
const drawerScrimElement = $('drawer-scrim');
if (drawerScrimElement) drawerScrimElement.addEventListener('click', () => setDrawer(false));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') setDrawer(false);
});

/* -------------------------------------------------------------- appearance */

// The five looks are pure token swaps in index.html, so the picker only needs a
// label, a colour preview and the background each one is designed around.
const THEMES = [
  { id: 'paper', label: '纸感编辑', note: '暖白纸底、衬线标题', swatch: ['#f6f4ef', '#1f4d3d', '#33312c'] },
  { id: 'dusk', label: '深蓝玻璃', note: '深色、毛玻璃卡片', swatch: ['#0a1120', '#5ea1ff', '#eaf1fa'] },
  { id: 'harbour', label: '维港暖调', note: '米白赤陶、直角扁平', swatch: ['#fbf4ea', '#c2410c', '#2a2118'] },
  { id: 'night', label: '极简夜幕', note: '近黑底、等宽数字', swatch: ['#08090a', '#2dd4bf', '#e7ebee'] },
  { id: 'classic', label: '经典蓝白', note: '改版前的配色', swatch: ['#f3f6f9', '#1769aa', '#123b63'] },
];

const BACKGROUNDS = [
  { id: '', label: '跟随主题' },
  { id: 'paper', label: '纸纹' },
  { id: 'dusk', label: '夜空城市' },
  { id: 'harbour', label: '维港黄昏' },
  { id: 'night', label: '深夜' },
  { id: 'none', label: '纯色' },
  { id: 'custom', label: '我的照片' },
];

// A background does not need to be more than a couple of thousand pixels wide,
// and every pixel kept is a pixel stored in the database and copied into every
// backup. 2048 on the long edge with JPEG at 0.82 lands a phone photo around
// 200-400 KB, which is the range the server's 1.5 MB ceiling was sized for.
const BG_MAX_EDGE = 2048;
const BG_QUALITIES = [0.82, 0.7];

let backgroundImage = { present: false, rev: 0 };
let pendingBackground = null;  // { blob, width, height } waiting for "用作背景"

// Theme -> the colour the browser paints its own chrome (mobile address bar).
const THEME_COLORS = {
  paper: '#1f1e1b', dusk: '#0a1120', harbour: '#243036', night: '#0a0c0e', classic: '#123b63',
};

let appearance = { theme: 'paper', background: '' };

function applyAppearance(theme, background, persist = true) {
  const known = THEMES.some((item) => item.id === theme) ? theme : 'paper';
  const bg = BACKGROUNDS.some((item) => item.id === background) ? background : '';
  appearance = { theme: known, background: bg };

  document.documentElement.dataset.theme = known;
  let image;
  if (bg === 'none') {
    image = 'none';
  } else if (bg === 'custom') {
    // The revision is in the URL on purpose: without it a replaced photo would
    // keep being served from the browser cache under the same address, and the
    // user would see their old background and conclude the upload failed.
    image = backgroundImage.present
      ? `url("/api/appearance/background?v=${backgroundImage.rev}")`
      : '';
  } else if (bg) {
    image = `url("/bg-${bg}.png")`;
  } else {
    image = '';
  }
  if (image) {
    document.documentElement.style.setProperty('--bg-image', image);
  } else {
    document.documentElement.style.removeProperty('--bg-image');
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', THEME_COLORS[known] || THEME_COLORS.paper);

  if (persist) {
    // Cached for the next first paint (and for the logged-out screen); the
    // account copy on the server is what makes it follow the user to another
    // browser, so a failure here must not break the switch.
    try {
      localStorage.setItem('pilot.appearance.theme', known);
      localStorage.setItem('pilot.appearance.background', bg);
    } catch (error) { /* private mode */ }
  }
}

function markAppearanceChoice() {
  document.querySelectorAll('#theme-grid .theme-card').forEach((node) => {
    node.classList.toggle('on', node.dataset.theme === appearance.theme);
  });
  document.querySelectorAll('#bg-row .bg-chip').forEach((node) => {
    node.classList.toggle('on', node.dataset.bg === appearance.background);
  });
}

async function saveAppearance(theme, background) {
  applyAppearance(theme, background);
  markAppearanceChoice();
  const note = $('appearance-status');
  try {
    await api('/api/appearance', {
      method: 'PUT',
      body: JSON.stringify({ theme: appearance.theme, background: appearance.background }),
    });
    if (note) {
      note.textContent = '外观已保存，换设备登录也是这一套。';
      note.classList.remove('warn');
      note.style.display = 'block';
    }
  } catch (error) {
    if (note) {
      note.textContent = `外观已在本机生效，但没能保存到账户：${error.message}`;
      note.classList.add('warn');
      note.style.display = 'block';
    }
  }
}

function renderAppearance(theme, background, image) {
  // The server profile wins over whatever this browser had cached, so a choice
  // made on the phone shows up here too.
  if (image) backgroundImage = { present: Boolean(image.present), rev: image.rev || 0 };
  applyAppearance(theme || appearance.theme, background === undefined ? '' : background);

  const grid = $('theme-grid');
  if (grid && !grid.dataset.ready) {
    grid.dataset.ready = '1';
    THEMES.forEach((item) => {
      const card = el('button', 'theme-card');
      card.type = 'button';
      card.dataset.theme = item.id;
      const swatch = el('div', 'swatch');
      item.swatch.forEach((colour) => {
        const chip = el('i');
        chip.style.background = colour;
        swatch.appendChild(chip);
      });
      card.appendChild(swatch);
      card.appendChild(el('b', null, item.label));
      card.appendChild(el('small', null, item.note));
      card.addEventListener('click', () => saveAppearance(item.id, appearance.background));
      grid.appendChild(card);
    });
  }

  const row = $('bg-row');
  if (row && !row.dataset.ready) {
    row.dataset.ready = '1';
    BACKGROUNDS.forEach((item) => {
      const chip = el('button', 'bg-chip', item.label);
      chip.type = 'button';
      chip.dataset.bg = item.id;
      chip.addEventListener('click', () => saveAppearance(appearance.theme, item.id));
      row.appendChild(chip);
    });
  }
  wireBackgroundPhoto();
  markAppearanceChoice();
}

/* -------------------------------------------------- background photo upload */

function setBackgroundStatus(message, kind = '') {
  const node = $('bg-photo-status');
  if (!node) return;
  node.textContent = message || '';
  node.className = `status${message ? ' ' + kind : ''}`;
  node.style.display = message ? 'block' : 'none';
}

/**
 * Decode, correct the orientation, downscale and re-encode -- all locally.
 *
 * `imageOrientation: 'from-image'` is not a refinement, it is the whole ballgame
 * for phone photos. A camera held upright usually stores the pixels sideways and
 * records "display this rotated 90 degrees" in an EXIF tag. `drawImage` reads
 * raw pixels, and a canvas has no EXIF to carry that tag forward, so without
 * this option every portrait photo would be saved permanently on its side with
 * nothing left to say otherwise -- and it would look correct in the preview
 * right up until the user reloaded the page.
 *
 * Re-encoding is also where the metadata goes. The canvas holds pixels and
 * nothing else, so GPS coordinates, the camera serial number and the editing
 * history do not exist in the output. That is what lets the server refuse
 * anything that still carries them instead of trying to rewrite containers.
 */
async function reencodeBackground(file) {
  if (!file) throw new Error('没有选择文件。');
  if (!/^image\/(jpeg|png)$/.test(file.type || '')) {
    throw new Error('只支持 JPEG 或 PNG 图片。');
  }
  if (typeof createImageBitmap !== 'function') {
    throw new Error('这个浏览器版本太旧，无法安全地在本地处理照片。');
  }

  let source;
  try {
    source = await createImageBitmap(file, { imageOrientation: 'from-image' });
  } catch (first) {
    try {
      source = await createImageBitmap(file);
    } catch (second) {
      // The browser's own wording here is "The source image could not be
      // decoded", which tells the user nothing about what to do next. The
      // common case is a file named .png that is not one, so say that instead.
      throw new Error('这个文件不是能解码的图片，请换一张 JPEG 或 PNG。');
    }
  }

  const scale = Math.min(1, BG_MAX_EDGE / Math.max(source.width, source.height));
  const width = Math.max(1, Math.round(source.width * scale));
  const height = Math.max(1, Math.round(source.height * scale));
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  canvas.getContext('2d').drawImage(source, 0, 0, width, height);
  if (source.close) source.close();

  let blob = null;
  for (const quality of BG_QUALITIES) {
    blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
    if (blob && blob.size <= 1_400_000) break;
  }
  if (!blob) throw new Error('这个浏览器无法把图片重新编码。');
  return { blob, width, height };
}

function showBackgroundPreview(url, caption) {
  const box = $('bg-photo-preview');
  if (!box) return;
  clear(box);
  if (!url) {
    box.hidden = true;
    return;
  }
  const image = el('img');
  image.src = url;
  image.alt = '背景图预览';
  box.appendChild(image);
  box.appendChild(el('figcaption', null, caption));
  box.hidden = false;
}

function refreshBackgroundControls() {
  const remove = $('bg-photo-remove');
  const use = $('bg-photo-use');
  if (remove) remove.hidden = !backgroundImage.present;
  if (use) use.disabled = !pendingBackground;
}

function noteAppearance(message, warn = false) {
  const note = $('appearance-status');
  if (!note) return;
  note.textContent = message;
  note.classList.toggle('warn', warn);
  note.style.display = 'block';
}

function wireBackgroundPhoto() {
  const input = $('bg-photo-input');
  if (!input || input.dataset.ready) {
    refreshBackgroundControls();
    return;
  }
  input.dataset.ready = '1';

  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    setBackgroundStatus('正在本机处理照片…');
    try {
      pendingBackground = await reencodeBackground(file);
      const kb = Math.round(pendingBackground.blob.size / 1024);
      showBackgroundPreview(
        URL.createObjectURL(pendingBackground.blob),
        `${pendingBackground.width}×${pendingBackground.height} · 约 ${kb} KB · 相机信息已去除`);
      setBackgroundStatus('照片已在本机处理好，点「用作背景」保存。', 'ok');
    } catch (error) {
      pendingBackground = null;
      showBackgroundPreview(null);
      setBackgroundStatus(error.message, 'error');
    }
    refreshBackgroundControls();
  });

  const use = $('bg-photo-use');
  if (use) {
    use.addEventListener('click', async () => {
      if (!pendingBackground) return;
      use.disabled = true;
      setBackgroundStatus('正在上传…');
      try {
        const data = await api('/api/appearance/background', {
          method: 'PUT',
          raw: pendingBackground.blob,
          contentType: 'image/jpeg',
        });
        backgroundImage = { present: true, rev: data.rev };
        pendingBackground = null;
        input.value = '';
        showBackgroundPreview(null);
        applyAppearance(appearance.theme, 'custom');
        markAppearanceChoice();
        setBackgroundStatus(
          `背景已保存（${data.width}×${data.height}，${Math.round(data.size / 1024)} KB）。`, 'ok');
        noteAppearance('背景已保存，换设备登录也是这一套。');
      } catch (error) {
        setBackgroundStatus(`上传失败：${error.message}`, 'error');
      }
      refreshBackgroundControls();
    });
  }

  const remove = $('bg-photo-remove');
  if (remove) {
    remove.addEventListener('click', async () => {
      setBackgroundStatus('正在删除…');
      try {
        await api('/api/appearance/background', { method: 'DELETE' });
        backgroundImage = { present: false, rev: 0 };
        pendingBackground = null;
        showBackgroundPreview(null);
        applyAppearance(appearance.theme, '');
        markAppearanceChoice();
        setBackgroundStatus('照片已删除。', 'ok');
        noteAppearance('照片已从账户里删除。');
      } catch (error) {
        setBackgroundStatus(`删除失败：${error.message}`, 'error');
      }
      refreshBackgroundControls();
    });
  }
  refreshBackgroundControls();
}

/* ------------------------------------------------------------------ forms */

function fillSelect(id, items, selected) {
  const node = $(id);
  clear(node);
  if (!selected) {
    const placeholder = el('option', null, '请选择…');
    placeholder.value = '';
    placeholder.selected = true;
    node.appendChild(placeholder);
  }
  items.forEach((item) => {
    const option = el('option', null, item.label);
    option.value = item.id;
    if (item.id === selected) option.selected = true;
    node.appendChild(option);
  });
}

function fill() {
  const p = state.profile || {};
  $('school-email').value = p.school_email || '';
  $('school-email-mailbox').value = p.school_email || '';
  $('major').value = p.major || '';
  $('year').value = p.year_of_study || '';
  $('courses').value = (p.courses || []).join(', ');
  $('interests').value = (p.interests || []).join(', ');
  $('goals').value = (p.career_goals || []).join(', ');
  $('focus').value = (p.focus_topics || []).join(', ');
  $('less').value = (p.less_interested || []).join(', ');
  $('custom').value = p.custom_instructions || '';
  $('language').value = p.language || 'bilingual';
  $('timezone').value = p.timezone || 'Asia/Hong_Kong';
  $('daily-time').value = p.daily_time || '22:00';
  $('immediate').checked = !!p.immediate_enabled;
  $('daily').checked = !!p.daily_enabled;
  renderAppearance(p.theme, p.background, state.background_image);

  const m = state.mailbox;
  if (m) {
    $('mail-email').value = m.email;
    $('report-to').value = m.report_to;
    $('imap-host').value = m.imap_host;
    $('imap-port').value = m.imap_port;
    $('smtp-host').value = m.smtp_host;
    $('smtp-port').value = m.smtp_port;
  }
  fillSelect('model-provider', catalog.models, state.connections.model && state.connections.model.provider);
  fillSelect('search-provider', catalog.search, state.connections.search && state.connections.search.provider);
  if (state.connections.model) {
    $('model-name').value = state.connections.model.model || '';
    $('model-base').value = state.connections.model.base_url || '';
  }
  initModelGuidance();
  initMailbox();
  showConnectionState();
}

$('register').addEventListener('click', async () => {
  // Checked here for a clear message, and again on the server because a client
  // check is not consent. The server is the one that must refuse.
  if (!$('accept-terms').checked) {
    setStatus('auth-status', '请先勾选同意《服务条款》和《隐私政策》。', 'error');
    return;
  }
  try {
    const user = await api('/api/auth/register', {
      method: 'POST',
      body: JSON.stringify({
        email: $('auth-email').value,
        password: $('auth-password').value,
        invite_code: $('invite').value,
        accepted_terms: true,
      }),
    });
    setStatus('auth-status', `注册成功：${user.email}`, 'ok');
    await load();
  } catch (error) { setStatus('auth-status', error.message, 'error'); }
});

$('login').addEventListener('click', async () => {
  try {
    await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: $('auth-email').value, password: $('auth-password').value }),
    });
    await load();
  } catch (error) { setStatus('auth-status', error.message, 'error'); }
});

$('logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
  location.reload();
});

$('refresh').addEventListener('click', () => refreshDashboard({ notify: true }));

$('save-profile').addEventListener('click', async () => {
  try {
    await api('/api/profile', {
      method: 'PUT',
      body: JSON.stringify({
        school_email: $('school-email').value,
        major: $('major').value,
        year_of_study: $('year').value,
        courses: list($('courses').value),
        interests: list($('interests').value),
        career_goals: list($('goals').value),
        focus_topics: list($('focus').value),
        less_interested: list($('less').value),
        custom_instructions: $('custom').value,
        language: $('language').value,
        timezone: $('timezone').value,
        immediate_enabled: $('immediate').checked,
        daily_enabled: $('daily').checked,
        daily_time: $('daily-time').value,
      }),
    });
    setStatus('profile-status', '已保存。下一步去「邮箱设置」确认收信。', 'ok');
    await load();
  } catch (error) { setStatus('profile-status', error.message, 'error'); }
});

$('save-mailbox').addEventListener('click', async () => {
  try {
    await api('/api/profile', { method: 'PUT', body: JSON.stringify({ school_email: $('school-email-mailbox').value }) });
    await api('/api/mailbox', {
      method: 'PUT',
      body: JSON.stringify({
        email: $('mail-email').value,
        report_to: $('report-to').value,
        imap_host: $('imap-host').value,
        imap_port: +$('imap-port').value,
        smtp_host: $('smtp-host').value,
        smtp_port: +$('smtp-port').value,
        app_password: $('mail-password').value,
      }),
    });
    $('mail-password').value = '';
    setStatus('mailbox-status', '已加密保存。现在点「只读连接测试」确认能收信。', 'ok');
    await load();
  } catch (error) { setStatus('mailbox-status', error.message, 'error'); }
});

async function saveConnection(kind) {
  const model = kind === 'model';
  const provider = $(model ? 'model-provider' : 'search-provider').value;
  if (!provider) { setStatus(`${kind}-status`, '请先选择供应商。', 'error'); return; }
  const key = $(model ? 'model-key' : 'search-key').value;
  if (!key && state.connections[kind]) {
    setStatus(`${kind}-status`, '密钥已经保存过了（出于安全不会回显）。要更换就填入新的再保存。', 'warn');
    return;
  }
  if (!key) { setStatus(`${kind}-status`, '请填写 API key。', 'error'); return; }
  try {
    await api(`/api/connections/${kind}`, {
      method: 'PUT',
      body: JSON.stringify({
        provider,
        model: model ? $('model-name').value : '',
        base_url: model ? $('model-base').value : '',
        api_key: key,
        config: model && $('azure-version').value ? { api_version: $('azure-version').value } : {},
      }),
    });
    $(model ? 'model-key' : 'search-key').value = '';
    setStatus(`${kind}-status`, '已加密保存。建议点旁边的测试按钮确认可用。', 'ok');
    await load();
  } catch (error) { setStatus(`${kind}-status`, error.message, 'error'); }
}

$('save-model').addEventListener('click', () => saveConnection('model'));
$('save-search').addEventListener('click', () => saveConnection('search'));

async function test(target) {
  setStatus(`${target}-status`, '正在测试…');
  try {
    const value = await api(`/api/test/${target}`, { method: 'POST' });
    let message = target === 'search'
      ? `成功，返回 ${value.results.length} 个来源。`
      : `成功：${value.result || value.imap || 'ok'}`;
    if (target === 'mailbox' && value.uid_validity) message += `；UIDVALIDITY ${value.uid_validity}`;
    setStatus(`${target}-status`, message, 'ok');
    await refreshDashboard();
  } catch (error) { setStatus(`${target}-status`, error.message, 'error'); }
}

$('test-model').addEventListener('click', () => test('model'));
$('test-search').addEventListener('click', () => test('search'));
$('test-mailbox').addEventListener('click', verifyMailbox);

async function verifyMailbox() {
  setStatus('mailbox-status', '正在检查收信通路（只读，不会改动邮件）…');
  try {
    const value = await api('/api/mailbox/verify', { method: 'POST' });
    let message = '连接成功，收信通路正常。';
    if (value.uid_validity) message += ` UIDVALIDITY ${value.uid_validity}。`;
    setStatus('mailbox-status', message, 'ok');
    dash = value.dashboard;
    renderDashboard();
  } catch (error) {
    setStatus('mailbox-status', `检查失败：${error.message} 这不会丢邮件；请按提示修正后重试。`, 'error');
    await refreshDashboard();
  }
}

/* ------------------------------------------------------------------ reports */

// How many reports are listed before the reader asks for more. A report is a
// long document; thirty of them expanded filled several screens, so the list
// is paged and each body is built on first open.
const REPORTS_FIRST_PAGE = 5;
const REPORTS_MORE = 10;

let reportRows = [];
let reportShown = REPORTS_FIRST_PAGE;

async function loadReports({ notify = false } = {}) {
  setStatus('reports-status', '加载中…');
  try {
    reportRows = await api('/api/reports');
    reportShown = REPORTS_FIRST_PAGE;
    setStatus('reports-status', '');
    renderReports();
    if (notify) {
      toast(reportRows.length ? `报告已刷新：共 ${reportRows.length} 份` : '报告已刷新：目前还没有报告', 'ok');
    }
  } catch (error) {
    setStatus('reports-status', error.message, 'error');
    if (notify) toast(`刷新报告失败：${error.message}`, 'error');
  }
}

function renderReports() {
  const node = $('reports-list');
  clear(node);
  if (!reportRows.length) {
    node.appendChild(el('p', 'help', '还没有报告。收到新邮件后会自动生成。'));
    return;
  }
  const shown = reportRows.slice(0, reportShown);
  const list = el('div', 'report-list');
  shown.forEach((row) => list.appendChild(reportItem(row)));
  node.appendChild(list);

  if (reportRows.length > shown.length) {
    const rest = reportRows.length - shown.length;
    const row = el('div', 'report-more');
    row.appendChild(el('span', null, `共 ${reportRows.length} 份，已显示最近 ${shown.length} 份`));
    const more = el('button', 'secondary', `再显示 ${Math.min(rest, REPORTS_MORE)} 份`);
    more.addEventListener('click', () => { reportShown += REPORTS_MORE; renderReports(); });
    row.appendChild(more);
    node.appendChild(row);
  } else if (reportRows.length > REPORTS_FIRST_PAGE) {
    node.appendChild(el('div', 'report-more', `共 ${reportRows.length} 份，已全部显示`));
  }
}

function reportItem(row) {
  const details = el('details', 'report-item');
  const summary = el('summary');
  summary.appendChild(el('span', 'report-subject', row.subject));
  summary.appendChild(el('span', 'report-meta',
    `${reportStatusText(row.status)} · ${momentText(row.created_at)}`));
  details.appendChild(summary);

  // Built on first open: the body is a rendered markdown document, and doing
  // that for every report up front was work nobody had asked to see yet.
  const holder = el('div');
  details.appendChild(holder);
  details.addEventListener('toggle', () => {
    if (!details.open || holder.dataset.built) return;
    holder.dataset.built = '1';
    const body = el('div', 'report-body');
    renderMarkdown(body, row.body_markdown);
    holder.appendChild(body);
    holder.appendChild(el('div', 'help', `发送至 ${row.sent_to || '—'}`));
    const actions = el('div', 'actions');
    const good = el('button', 'secondary', '有用');
    const bad = el('button', 'secondary', '没用');
    good.addEventListener('click', () => feedback(row.id, 'useful'));
    bad.addEventListener('click', () => feedback(row.id, 'not_useful'));
    actions.appendChild(good);
    actions.appendChild(bad);
    holder.appendChild(actions);
  });
  return details;
}

const REPORT_STATUS_TEXT = { sent: '已发出', generated: '已生成', failed: '发送失败' };

function reportStatusText(status) {
  return REPORT_STATUS_TEXT[status] || status || '';
}

async function feedback(id, rating) {
  try {
    await api(`/api/reports/${id}/feedback`, { method: 'PUT', body: JSON.stringify({ rating, note: '' }) });
    setStatus('reports-status', '已记录你的反馈。', 'ok');
  } catch (error) { setStatus('reports-status', error.message, 'error'); }
}

$('load-reports').addEventListener('click', () => loadReports({ notify: true }));
$('task-back-today').addEventListener('click', () => loadTasksFor(''));
$('pause').addEventListener('click', async () => {
  if (confirm('暂停后不会再读取或发送邮件。继续吗？')) {
    await api('/api/account/status/paused', { method: 'PUT' });
    await load();
  }
});
$('resume').addEventListener('click', async () => {
  await api('/api/account/status/active', { method: 'PUT' });
  await load();
});
$('export-data').addEventListener('click', () => {
  // A plain navigation rather than fetch(): the browser's own download UI is the
  // honest way to hand over a file, and it keeps the payload out of JS memory.
  // The session cookie rides along, so no token is ever put in the URL.
  setStatus('reports-status', '正在准备导出…');
  window.location.assign('/api/account/export');
});
$('delete').addEventListener('click', async () => {
  if (confirm('这会永久删除账户、邮箱授权码、API key 和报告记录，且无法恢复。\n\n建议先点「导出我的数据」保存一份。确定要删除吗？')) {
    await api('/api/account/status/deleted', { method: 'PUT' });
    location.reload();
  }
});

/* -------------------------------------- mailbox onboarding (plain language) */

function mailList() { return (catalog && catalog.mailbox && catalog.mailbox.presets) || []; }
function mailPreset(id) { return mailList().find((preset) => preset.id === id) || null; }

function mailPresetForDomain(email) {
  const domain = String(email || '').split('@')[1];
  if (!domain) return 'custom';
  const hit = mailList().find((preset) => (preset.domains || []).indexOf(domain.toLowerCase()) >= 0);
  return hit ? hit.id : 'custom';
}

function renderGlossary() {
  const glossary = catalog && catalog.mailbox ? catalog.mailbox.glossary : null;
  const node = $('mail-jargon');
  if (!glossary || !node) return;
  clear(node);
  const dl = el('dl', 'jargon');
  ['imap', 'smtp', 'password'].forEach((key) => {
    const item = glossary[key];
    if (!item) return;
    dl.appendChild(el('dt', null, `${item.term}（${item.technical}）`));
    dl.appendChild(el('dd', null, `${item.plain} ${item.typical}`));
  });
  node.appendChild(dl);
}

function renderMailboxGuide(id) {
  const preset = mailPreset(id);
  const box = $('mail-howto');
  const caution = $('mail-caution');
  if (!box) return;
  clear(box);
  caution.classList.add('hidden');
  caution.textContent = '';
  if (!preset) return;
  box.appendChild(el('h3', null, `怎么拿到授权码 · ${preset.label}`));
  const ol = el('ol');
  (preset.steps || []).forEach((text) => ol.appendChild(el('li', null, text)));
  box.appendChild(ol);
  if (preset.help_url) {
    const wrap = el('div', 'help');
    const link = el('a', null, preset.help_label || preset.help_url);
    link.href = preset.help_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    wrap.appendChild(link);
    box.appendChild(wrap);
  }
  const hint = $('mail-password-help');
  if (hint) hint.textContent = preset.id === 'custom' ? '' : '步骤见上面「怎么拿到授权码」。';
  if (preset.caution) {
    caution.textContent = preset.caution;
    caution.classList.remove('hidden');
  }
}

function applyMailboxPreset(id, fillServers) {
  const preset = mailPreset(id);
  if (!preset) return;
  if (fillServers) {
    $('imap-host').value = preset.imap_host || '';
    $('imap-port').value = preset.imap_port || 993;
    $('smtp-host').value = preset.smtp_host || '';
    $('smtp-port').value = preset.smtp_port || 465;
  }
  const help = $('mail-provider-help');
  if (help) {
    help.textContent = preset.id === 'custom'
      ? '请按下面步骤，从邮箱设置里把两个服务器地址抄过来。'
      : '服务器和端口已自动填好——你只需要提供授权码。';
  }
  const summary = $('mail-advanced-summary');
  if (summary) summary.textContent = preset.imap_host ? '服务器地址和端口（已自动填好，通常不用改）' : '服务器地址和端口（需要你自己填）';
  renderMailboxGuide(id);
}

function renderForwardingWizard() {
  const email = String($('mail-email').value || '').trim();
  const target = $('forward-target');
  const copy = $('copy-forward-target');
  if (target) target.textContent = email || '请先填写下面的私人邮箱';
  if (copy) copy.disabled = !email;
}

async function copyForwardTarget() {
  const email = String($('mail-email').value || '').trim();
  if (!email) { setStatus('forward-status', '请先填写私人邮箱。', 'error'); return; }
  try {
    await navigator.clipboard.writeText(email);
    setStatus('forward-status', '已复制。现在打开 Outlook，粘贴到转发地址。', 'ok');
  } catch (_) {
    setStatus('forward-status', '浏览器不允许自动复制，请手动选中上面的地址复制。', 'error');
  }
}

function initMailbox() {
  if (!mailList().length) return;
  const saved = state && state.mailbox ? state.mailbox : null;
  const loginEmail = state && state.user ? state.user.email : '';
  if (!saved && !$('mail-email').value && loginEmail) $('mail-email').value = loginEmail;
  const current = mailPresetForDomain((saved && saved.email) || $('mail-email').value || loginEmail);
  fillSelect('mail-provider', mailList(), current);
  applyMailboxPreset(current, !(saved && saved.imap_host));
  renderGlossary();
  // Assigning these (instead of addEventListener) keeps re-renders from
  // stacking duplicate handlers, which would fire several saves per click.
  $('mail-provider').onchange = () => applyMailboxPreset($('mail-provider').value, true);
  $('mail-email').oninput = renderForwardingWizard;
  $('mail-email').onchange = () => {
    const guess = mailPresetForDomain($('mail-email').value);
    $('mail-provider').value = guess;
    applyMailboxPreset(guess, true);
    renderForwardingWizard();
  };
  $('copy-forward-target').onclick = copyForwardTarget;
  $('school-email-mailbox').oninput = () => { $('school-email').value = $('school-email-mailbox').value; };
  $('school-email').oninput = () => { $('school-email-mailbox').value = $('school-email').value; };
  renderForwardingWizard();
  if ($('report-to') && !$('report-to').value) {
    $('report-to').placeholder = `留空 = 发回 ${$('mail-email').value || '上面的邮箱'}`;
  }
}

/* ------------------------------------- native search: step 4 can be skipped */

function modelList() { return (catalog && catalog.models) || []; }

function currentModelId() {
  const select = $('model-provider');
  if (select && select.value) return select.value;
  return state && state.connections && state.connections.model ? state.connections.model.provider : '';
}

function modelHasNativeSearch() {
  const item = modelList().find((model) => model.id === currentModelId());
  return !!(item && item.native_search);
}

function modelLabel(id) {
  const item = modelList().find((model) => model.id === id);
  return item ? item.label : id;
}

function markModelOptions() {
  const select = $('model-provider');
  if (!select) return;
  Array.prototype.forEach.call(select.options, (option) => {
    const item = modelList().find((model) => model.id === option.value);
    if (item) option.textContent = item.native_search ? `${item.label}（自带联网搜索）` : item.label;
  });
}

function updateModelGuidance() {
  const native = modelHasNativeSearch();
  const help = $('model-provider-help');
  if (help) {
    help.textContent = native
      ? '这个供应商自带联网搜索，「联网搜索」那一页可以整段跳过。'
      : '这个供应商不自带联网搜索。想让建议经过联网核实，就去配置搜索 API；不配置也能正常出摘要。';
  }
  const note = $('search-native-note');
  if (note) {
    if (native) {
      note.textContent = `你选的「${modelLabel(currentModelId())}」自带联网搜索，这一页可以整段跳过。`;
      note.classList.remove('hidden');
    } else {
      note.classList.add('hidden');
      note.textContent = '';
    }
  }
}

function initModelGuidance() {
  markModelOptions();
  updateModelGuidance();
  const select = $('model-provider');
  if (select) select.onchange = updateModelGuidance;
}

/* ------------------------------------------------------------ account security */

async function loadSecurity() {
  try {
    const data = await api('/api/account/security');
    const node = $('security-sessions');
    if (node) {
      node.textContent = `当前登录邮箱：${data.email} · 已登录设备：${data.active_sessions} · 登录有效期 ${data.session_days} 天`;
    }
  } catch (error) {
    setStatus('security-status', `无法读取安全信息：${error.message}`, 'error');
  }
}

$('change-password').addEventListener('click', async () => {
  const current = $('cur-password').value;
  const next = $('new-password').value;
  if (!current || !next) { setStatus('security-status', '请填写当前密码和新密码。', 'error'); return; }
  if (next.length < 12) { setStatus('security-status', '新密码至少 12 位。', 'error'); return; }
  if (!confirm('修改密码后，其它设备上的登录会立即失效。继续吗？')) return;
  try {
    const data = await api('/api/account/password', {
      method: 'PUT', body: JSON.stringify({ current_password: current, new_password: next }),
    });
    $('cur-password').value = '';
    $('new-password').value = '';
    setStatus('security-status', `密码已修改，另外 ${data.revoked} 个设备上的登录已失效。`, 'ok');
    await loadSecurity();
  } catch (error) {
    setStatus('security-status', error.message, 'error');
  }
});

$('revoke-sessions').addEventListener('click', async () => {
  if (!confirm('退出所有设备？包括你正在用的这一台也会换成新会话，其它设备需要重新登录。')) return;
  try {
    const data = await api('/api/account/sessions/revoke', { method: 'POST' });
    setStatus('security-status', `已退出所有设备（吊销 ${data.revoked} 个会话），本机已换发新会话。`, 'ok');
    await loadSecurity();
  } catch (error) {
    setStatus('security-status', error.message, 'error');
  }
});

/* --------------------------------- saved connection state (keys never echo) */

function showConnectionState() {
  ['model', 'search'].forEach((kind) => {
    const node = $(`${kind}-saved`);
    if (!node) return;
    const connection = state && state.connections ? state.connections[kind] : null;
    if (connection && connection.platform) {
      // Without this the model form looks untouched and the user reasonably
      // concludes they must find a key before anything works, when in fact the
      // pilot is already paying and reports are already being generated.
      node.className = 'saved';
      node.textContent = `正在使用管理员提供的 key：${connection.provider}${connection.model ? ' · ' + connection.model : ''}`
        + '（内测期间你不用付费）。在下面填自己的 key 会覆盖它。';
    } else if (connection) {
      node.className = 'saved';
      node.textContent = `已配置：${connection.provider}${connection.model ? ' · ' + connection.model : ''}${connection.last_error ? '　上次出错：' + connection.last_error : ''}`;
    } else if (kind === 'search' && modelHasNativeSearch()) {
      node.className = 'saved';
      node.textContent = '不需要单独配置 —— 当前模型自带联网搜索。';
    } else {
      node.className = 'saved warn';
      node.textContent = kind === 'search'
        ? '尚未配置（可选，不配置也能出报告，只是没有联网核实来源）'
        : '尚未配置';
    }
  });
}

load();


/* ------------------------------------------------------------- admin console
 * Every value below is rendered with textContent / createElement, so a user
 * whose display data contains markup cannot inject anything into this page.
 */
function adminCell(row, label, value) {
  const cell = el('div');
  cell.appendChild(el('small', null, label));
  cell.appendChild(el('div', null, value === null || value === undefined || value === '' ? '—' : String(value)));
  return cell;
}

function adminStamp(value) {
  // Operator-facing, and about other people's accounts, so the zone is named.
  return momentText(value, { seconds: true, withZone: true, fallback: '从未' });
}

function renderAdminHealth(health) {
  const box = $('admin-health');
  clear(box);
  [
    ['注册用户', `${health.users} / ${health.max_users}`],
    ['启用中', `${health.active_users} 人`],
    ['已暂停', `${health.paused_users} 人`],
    ['待处理队列', `${health.pending_messages} 封`],
    ['失败报告', `${health.failed_reports} 份`],
    ['收信在跑', `${health.mailboxes_polled_recently} / ${health.mailboxes} 个邮箱`],
  ].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    box.appendChild(cell);
  });
  if (health.stale_mailboxes > 0) {
    // Name the mailbox. The threshold depends on the provider (Gmail is only
    // polled every 15 minutes because Google asks for that), so the message no
    // longer quotes a fixed number of minutes, and an operator should not have
    // to open every account to find out which one is meant.
    const who = (health.stale_mailbox_emails || []).filter(Boolean);
    setStatus('admin-status',
      `有 ${health.stale_mailboxes} 个邮箱超过各自的轮询间隔仍没有取信记录`
      + (who.length ? `：${who.join('、')}` : '')
      + '。请检查该用户的收信通路。', 'warn');
  } else {
    setStatus('admin-status', `后台已就绪 · 版本 ${health.version} · 检查时间 ${adminStamp(health.checked_at)}`, 'ok');
  }
}

const SETUP_GAP_TEXT = {
  no_mailbox: '没配私人转发邮箱',
  unreachable: '配了邮箱但从没连通成功',
};

/* ---- the per-account lights ---------------------------------------------
   Which parts of an account have been *proven* to work. The verdict is computed
   server-side in `Database.verification_lights` and only drawn here: the rule
   needs the error columns, and a frontend that re-derived it from the same
   timestamps would show a green light for a key whose test just failed -- every
   one of those timestamps is written on failure too. */
const LIGHT_ORDER = ['mailbox', 'model', 'search', 'report'];

function renderLights(row) {
  const wrap = el('div', 'lights');
  const found = {};
  (row.lights || []).forEach((light) => { found[light.key] = light; });
  LIGHT_ORDER.forEach((key) => {
    const light = found[key];
    if (!light) return;
    const node = el('span', `light ${light.ok ? 'ok' : 'bad'}`);
    node.appendChild(el('i', 'dot'));
    node.appendChild(el('b', null, light.label));
    const why = light.ok ? '通了' : (light.detail || '不通');
    node.appendChild(el('span', 'why', why.length > 40 ? `${why.slice(0, 40)}…` : why));
    // The full text always in the tooltip: the visible part is trimmed for
    // width, and a trimmed provider error is usually the worthless half.
    node.title = `${light.label}：${light.detail || ''}`
      + (light.at ? `（${adminStamp(light.at)}）` : '');
    wrap.appendChild(node);
  });
  return wrap;
}

/* The operator's memo about an account. Its own endpoint rather than a field on
   the settings form, and the label says out loud that the user cannot see it --
   an operator who assumed the opposite would write something they would not
   want read back to them. */
function adminNoteEditor(row) {
  const wrap = el('div', 'adminnote');
  const box = el('textarea');
  const fieldId = `admin-note-${row.id}`;
  box.id = fieldId;
  box.rows = 2;
  box.maxLength = 500;
  box.value = row.admin_note || '';
  box.placeholder = '只有管理员看得到。例如：授权码填错过一次；同学介绍来的；2026-09 起因毕业停用。';
  const label = el('label', null, '管理员备注（不会出现在用户自己的页面、导出或任何邮件里）');
  label.htmlFor = fieldId;
  wrap.appendChild(label);
  wrap.appendChild(box);
  const bar = el('div', 'row');
  const save = el('button', 'secondary', '保存备注');
  const state = el('span', 'help');
  save.addEventListener('click', async () => {
    save.disabled = true;
    state.textContent = '保存中…';
    try {
      await api(`/api/admin/users/${encodeURIComponent(row.id)}/note`, {
        method: 'PUT', body: JSON.stringify({ note: box.value }),
      });
      // Refreshed rather than patched locally: `adminData.users` is what the
      // panel re-renders from when it is reopened, so a note that only lived in
      // this textarea would appear to revert on the next visit.
      await loadAdmin();
      toast('备注已保存', 'ok');
    } catch (error) {
      state.textContent = '';
      save.disabled = false;
      toast(`备注保存失败：${error.message}`, 'error');
    }
  });
  bar.appendChild(save);
  bar.appendChild(state);
  wrap.appendChild(bar);
  return wrap;
}

function renderAdminUsers(users) {
  const box = $('admin-users');
  clear(box);
  if (!users.length) { box.appendChild(el('p', 'help', '还没有注册用户。')); return; }
  // Unfinished signups first: they are the only rows on this panel that need
  // somebody to do something, and on a list sorted by registration date they
  // were the ones you had to go looking for.
  const ordered = users.slice().sort((a, b) => {
    const gap = Number(Boolean(b.setup_gap)) - Number(Boolean(a.setup_gap));
    return gap !== 0 ? gap : String(a.created_at).localeCompare(String(b.created_at));
  });
  ordered.forEach((row) => {
    const item = el('article', 'report');
    const head = el('div', 'spread');
    const title = el('div');
    title.appendChild(el('strong', null, row.email));
    const badges = el('div', 'help');
    badges.textContent = `状态：${row.status} · 注册于 ${adminStamp(row.created_at)}`
      + (row.setup_gap ? ` · 未配完：${SETUP_GAP_TEXT[row.setup_gap] || row.setup_gap}` : '');
    if (row.setup_gap) badges.classList.add('warn');
    title.appendChild(badges);
    head.appendChild(title);
    const actions = el('div', 'row');
    if (row.status === 'active') {
      const pause = el('button', 'secondary', '暂停');
      pause.addEventListener('click', () => adminSetStatus(row.id, 'paused', row.email));
      actions.appendChild(pause);
    } else {
      const resume = el('button', 'secondary', '恢复');
      resume.addEventListener('click', () => adminSetStatus(row.id, 'active', row.email));
      actions.appendChild(resume);
    }
    const remove = el('button', 'danger', '删除');
    remove.addEventListener('click', () => adminSetStatus(row.id, 'deleted', row.email));
    actions.appendChild(remove);
    head.appendChild(actions);
    item.appendChild(head);
    item.appendChild(renderLights(row));

    const grid = el('div', 'chaingrid');
    grid.appendChild(adminCell(row, '学校邮箱', row.school_email));
    grid.appendChild(adminCell(row, '专业 / 年级', [row.major, row.year_of_study].filter(Boolean).join(' · ')));
    grid.appendChild(adminCell(row, '转发邮箱', row.mailbox_email));
    grid.appendChild(adminCell(row, '报告发往', row.report_to));
    grid.appendChild(adminCell(row, '收信上次轮询', adminStamp(row.last_polled_at)));
    grid.appendChild(adminCell(row, '只读验证', adminStamp(row.last_verified_at)));
    grid.appendChild(adminCell(row, 'AI 模型', row.model_provider ? `${row.model_provider}${row.model_name ? ' · ' + row.model_name : ''}` : '未配置'));
    grid.appendChild(adminCell(row, '联网搜索', row.search_provider || '未配置'));
    grid.appendChild(adminCell(row, '邮件 / 报告', `${row.message_count} / ${row.report_count}`));
    grid.appendChild(adminCell(row, '队列 / 失败', `${row.queue_depth} / ${row.failed_reports}`));
    grid.appendChild(adminCell(row, '最近报告', adminStamp(row.last_report_at)));
    grid.appendChild(adminCell(row, '每日简报', row.daily_enabled ? `${row.daily_time || '22:00'}（${row.timezone || ''}）` : '已关闭'));
    item.appendChild(grid);

    const problems = [row.mailbox_error, row.last_verify_error, row.model_error, row.search_error].filter(Boolean);
    if (problems.length) {
      item.appendChild(el('div', 'caution', `最近错误：${problems.join(' | ').slice(0, 400)}`));
    }
    item.appendChild(adminNoteEditor(row));
    box.appendChild(item);
  });
}

/* ---- per-user settings editor -------------------------------------------
   The operator can fix a classmate's configuration without touching the
   database. Two rules shape this form:
     * only the fields that actually changed are sent, because the endpoint is a
       selective patch and "send everything" is how a fix turns into data loss;
     * stored secrets are never rendered. A key field shows "已配置 / 未配置"
       and an empty box means "leave it alone".                          */

function adminField(label, node, hint) {
  const wrap = el('div');
  wrap.appendChild(el('label', null, label));
  node.id = node.id || `admin-f-${Math.random().toString(36).slice(2, 9)}`;
  wrap.appendChild(node);
  if (hint) wrap.appendChild(el('div', 'help', hint));
  return wrap;
}

function adminText(value, placeholder) {
  const input = el('input');
  input.type = 'text';
  input.value = value == null ? '' : String(value);
  if (placeholder) input.placeholder = placeholder;
  return input;
}

function adminSelect(items, selected) {
  const select = el('select');
  items.forEach((item) => {
    const option = el('option', null, item.label);
    option.value = item.id;
    if (item.id === selected) option.selected = true;
    select.appendChild(option);
  });
  return select;
}

function adminSecret(label, configured, placeholder) {
  const input = el('input');
  input.type = 'password';
  input.autocomplete = 'new-password';
  input.placeholder = placeholder;
  return adminField(label, input, configured ? '已配置。留空保持不变，填写则替换。' : '尚未配置；填写后才会生效。');
}

function renderAdminEditor(row, { collapsible = false } = {}) {
  // Inside the "修改用户设置" panel the outer <details> is already the thing you
  // opened, so nesting a second disclosure just to reach the form would be a
  // click for nothing. Standalone use can still collapse it.
  const details = collapsible ? el('details', 'advanced') : el('div', 'editor');
  if (collapsible) details.appendChild(el('summary', null, '修改该用户的设置'));
  const body = el('div', 'body');
  const inputs = {};

  const section = (text) => body.appendChild(el('h4', null, text));

  section('个人资料');
  const profileGrid = el('div', 'grid2');
  inputs.school_email = adminText(row.school_email, 'student@my.cityu.edu.hk');
  inputs.major = adminText(row.major);
  inputs.year_of_study = adminText(row.year_of_study);
  inputs.timezone = adminText(row.timezone || 'Asia/Hong_Kong');
  profileGrid.appendChild(adminField('学校邮箱', inputs.school_email));
  profileGrid.appendChild(adminField('专业', inputs.major));
  profileGrid.appendChild(adminField('年级', inputs.year_of_study));
  profileGrid.appendChild(adminField('时区', inputs.timezone));
  body.appendChild(profileGrid);

  section('简报与开关');
  const scheduleGrid = el('div', 'grid2');
  inputs.daily_time = adminText(row.daily_time || '22:00');
  inputs.daily_time.type = 'time';
  scheduleGrid.appendChild(adminField('每日简报时间', inputs.daily_time));
  const switches = el('div', 'stack');
  inputs.immediate_enabled = el('input'); inputs.immediate_enabled.type = 'checkbox';
  inputs.immediate_enabled.checked = !!row.immediate_enabled;
  inputs.daily_enabled = el('input'); inputs.daily_enabled.type = 'checkbox';
  inputs.daily_enabled.checked = !!row.daily_enabled;
  const immediateLabel = el('label');
  immediateLabel.appendChild(inputs.immediate_enabled);
  immediateLabel.appendChild(document.createTextNode(' 收到新邮件立即发送摘要'));
  const dailyLabel = el('label');
  dailyLabel.appendChild(inputs.daily_enabled);
  dailyLabel.appendChild(document.createTextNode(' 每天发送简报'));
  switches.appendChild(immediateLabel);
  switches.appendChild(dailyLabel);
  scheduleGrid.appendChild(switches);
  body.appendChild(scheduleGrid);

  section('AI 模型');
  const modelGrid = el('div', 'grid2');
  inputs.model_provider = adminSelect([{ id: '', label: '（不修改）' }, ...(catalog.models || [])],
                                      row.model_provider || '');
  inputs.model_name = adminText(row.model_name);
  inputs.model_base_url = adminText('');
  modelGrid.appendChild(adminField('供应商', inputs.model_provider));
  modelGrid.appendChild(adminField('模型名', inputs.model_name, '例如 deepseek-chat（不要用推理型模型）'));
  modelGrid.appendChild(adminField('接口地址', inputs.model_base_url, '留空使用供应商默认地址'));
  body.appendChild(modelGrid);
  const modelSecret = adminSecret('模型 API key', Boolean(row.model_provider), '留空 = 不改');
  inputs.model_key = modelSecret.querySelector('input');
  body.appendChild(modelSecret);

  section('联网搜索');
  inputs.search_provider = adminSelect([{ id: '', label: '（不修改）' }, ...(catalog.search || [])],
                                       row.search_provider || '');
  body.appendChild(adminField('供应商', inputs.search_provider));
  const searchSecret = adminSecret('搜索 API key', Boolean(row.search_provider), '留空 = 不改');
  inputs.search_key = searchSecret.querySelector('input');
  body.appendChild(searchSecret);

  section('邮箱');
  inputs.report_to = adminText(row.report_to);
  body.appendChild(adminField('报告发往', inputs.report_to, '只改收件地址不会影响收信游标。'));
  const mailboxSecret = adminSecret('邮箱授权码', Boolean(row.mailbox_email), '留空 = 不改；填写会重新建立收信连接');
  inputs.mailbox_key = mailboxSecret.querySelector('input');
  body.appendChild(mailboxSecret);

  const status = el('div', 'saved');
  status.style.display = 'none';
  const save = el('button', null, '保存修改');
  save.type = 'button';
  save.addEventListener('click', () => adminSaveSettings(row, inputs, status, save));
  const actions = el('div', 'actions');
  actions.appendChild(save);
  body.appendChild(actions);
  body.appendChild(status);
  details.appendChild(body);
  return details;
}

async function adminSaveSettings(row, inputs, status, save) {
  const payload = {};
  const same = (value, original) => String(value == null ? '' : value).trim() === String(original == null ? '' : original).trim();
  if (!same(inputs.school_email.value, row.school_email)) payload.school_email = inputs.school_email.value.trim();
  if (!same(inputs.major.value, row.major)) payload.major = inputs.major.value.trim();
  if (!same(inputs.year_of_study.value, row.year_of_study)) payload.year_of_study = inputs.year_of_study.value.trim();
  if (!same(inputs.timezone.value, row.timezone || 'Asia/Hong_Kong')) payload.timezone = inputs.timezone.value.trim();
  if (!same(inputs.daily_time.value, row.daily_time || '22:00')) payload.daily_time = inputs.daily_time.value;
  if (inputs.daily_enabled.checked !== !!row.daily_enabled) payload.daily_enabled = inputs.daily_enabled.checked;
  if (inputs.immediate_enabled.checked !== !!row.immediate_enabled) payload.immediate_enabled = inputs.immediate_enabled.checked;
  if (!same(inputs.report_to.value, row.report_to)) payload.report_to = inputs.report_to.value.trim();

  const modelProvider = inputs.model_provider.value;
  const modelName = inputs.model_name.value.trim();
  const modelBase = inputs.model_base_url.value.trim();
  const providerChanged = modelProvider && modelProvider !== (row.model_provider || '');
  if (providerChanged) payload.model_provider = modelProvider;
  if (modelProvider && !same(modelName, row.model_name)) payload.model_name = modelName;
  if (modelProvider && modelBase) payload.model_base_url = modelBase;
  const modelKey = inputs.model_key ? inputs.model_key.value.trim() : '';
  if (modelKey) {
    payload.model_api_key = modelKey;
    if (!modelProvider && row.model_provider) payload.model_provider = row.model_provider;
  }

  const searchProvider = inputs.search_provider.value;
  if (searchProvider && searchProvider !== (row.search_provider || '')) payload.search_provider = searchProvider;
  const searchKey = inputs.search_key ? inputs.search_key.value.trim() : '';
  if (searchKey) {
    payload.search_api_key = searchKey;
    if (!searchProvider && row.search_provider) payload.search_provider = row.search_provider;
  }

  const mailboxKey = inputs.mailbox_key ? inputs.mailbox_key.value.trim() : '';
  if (mailboxKey) payload.mailbox_app_password = mailboxKey;

  status.style.display = 'block';
  if (!Object.keys(payload).length) {
    status.className = 'saved warn';
    status.textContent = '没有检测到改动。';
    return;
  }
  save.disabled = true;
  try {
    const data = await api(`/api/admin/users/${encodeURIComponent(row.id)}/settings`, {
      method: 'PUT', body: JSON.stringify(payload),
    });
    const receipt = `已保存：${data.changed.join('、')}。密钥只显示字段名，不会回显内容。`;
    adminData.users = data.users;
    // The save response carries a fresh audit list; store it as well as render
    // it. `renderAdminPanels` only paints the audit panel when it is already
    // open, and the operator's next move is usually to open that panel and
    // confirm the change they just made -- which would otherwise paint the list
    // fetched when the console was first loaded, i.e. without their own edit.
    // The collapsed summary would say "最近 N 条" while the open panel listed
    // N-1, which reads as "my change was not recorded".
    if (data.audit) adminData.audit = data.audit;
    renderAdminPanels(data);
    renderEditTarget(receipt);
    setStatus('admin-status', `已修改 ${row.email} 的设置。`, 'ok');
    loadMailSummary();
    loadUsageSummary();
    if (PANEL_LOADED.users) renderAdminUsers(data.users);
  } catch (error) {
    status.className = 'saved warn';
    status.textContent = `保存失败：${error.message}`;
  } finally {
    save.disabled = false;
  }
}

/* ---- collapsible admin panels -------------------------------------------
   Native <details>/<summary> (the pattern GitHub Primer documents for
   disclosure): hidden by default, keyboard accessible, no JS required to open.
   Two rules keep collapsing from hiding anything that matters:
     * the summary line always carries the current numbers;
     * heavy data is fetched the first time a panel is opened, not on page load.
   The live metrics poller only runs while its own panel is open.          */

const PANEL_LOADED = {};

function panelNote(id, text, tone) {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.className = `panel-note${tone ? ' ' + tone : ''}`;
}

function panelIsOpen(id) {
  const node = $(id);
  return Boolean(node && node.open);
}

function wirePanel(id, onOpen) {
  const node = $(id);
  if (!node) return;
  node.addEventListener('toggle', () => {
    if (!node.open) {
      if (id === 'panel-metrics') stopMetrics();
      return;
    }
    try {
      onOpen();
    } catch (error) {
      setStatus('admin-status', `打开面板失败：${error.message}`, 'error');
    }
  });
}

/* ---- the sentinel's own verdict ------------------------------------------
   Read from `alert_state` rather than re-evaluated: the sentinel already ran
   those checks five minutes ago and the console is opened far more often than
   that -- and re-running them here would mean a network call for the
   certificate on every page load. The tier is printed on every row because
   "why is this one quiet?" is the entire question this panel answers. */
const ALERT_TIER_TEXT = {
  mail: { label: '立刻发邮件', tone: 'bad' },
  digest: { label: '每天汇总一封', tone: 'warn' },
  panel: { label: '只在这里显示', tone: '' },
};
const ALERT_SEVERITY_TEXT = { critical: '严重', warning: '提醒', info: '信息' };

function renderAdminAlerts(alerts) {
  const box = $('admin-alerts');
  if (!box) return;
  clear(box);
  const open = (alerts || []).filter((row) => row.open);
  if (!open.length) {
    box.appendChild(el('p', 'help', '现在没有异常。'));
    return;
  }
  open.forEach((row) => {
    const item = el('article', 'report');
    const head = el('div', 'spread');
    const title = el('div');
    title.appendChild(el('strong', null, row.title || row.key));
    const tier = ALERT_TIER_TEXT[row.tier] || { label: row.tier, tone: '' };
    const badges = el('div', 'help');
    badges.textContent = `${ALERT_SEVERITY_TEXT[row.severity] || row.severity} · ${tier.label}`
      + ` · 首次发现 ${adminStamp(row.first_seen_at)}`
      + (row.last_sent_at ? ` · 上次提醒 ${adminStamp(row.last_sent_at)}` : '');
    if (tier.tone === 'bad') badges.classList.add('warn');
    title.appendChild(badges);
    head.appendChild(title);

    const actions = el('div', 'row');
    const button = el('button', row.acknowledged ? 'secondary' : null,
                      row.acknowledged ? '恢复提醒' : '已知晓，别再提醒');
    button.addEventListener('click', () => adminAcknowledgeAlert(row.key, !row.acknowledged));
    actions.appendChild(button);
    head.appendChild(actions);
    item.appendChild(head);

    if (row.detail) item.appendChild(el('p', 'help', row.detail));
    if (row.acknowledged) {
      item.appendChild(el('div', 'caution',
        '已知晓：不再为这条发邮件。问题清掉之后会自动恢复提醒，所以它盖不住以后的新问题。'));
    }
    box.appendChild(item);
  });
}

async function adminAcknowledgeAlert(key, acknowledge) {
  try {
    const data = await api(`/api/admin/alerts/${encodeURIComponent(key)}/acknowledge`, {
      method: acknowledge ? 'POST' : 'DELETE',
      body: acknowledge ? JSON.stringify({}) : undefined,
    });
    if (adminData) adminData.alerts = data.alerts || [];
    renderAdminAlerts(data.alerts);
    renderAdminPanels(adminData || {});
    toast(acknowledge ? '这条不再发邮件了' : '这条恢复提醒', 'ok');
  } catch (error) {
    toast(`操作失败：${error.message}`, 'error');
  }
}

function renderAdminPanels(data) {
  const users = data.users || [];
  const active = users.filter((row) => row.status === 'active').length;
  const paused = users.filter((row) => row.status === 'paused').length;
  panelNote('panel-edit-note', `${users.length} 个用户`);
  // The collapsed row has to carry the number that needs acting on, or the
  // panel has to be opened on every visit to find out whether anything does.
  const stalled = users.filter((row) => row.setup_gap).length;
  panelNote('panel-users-note',
    `${users.length} 人 · ${active} 启用 / ${paused} 暂停`
    + (stalled ? ` · ${stalled} 人没配完` : ''),
    stalled ? 'warn' : '');
  panelNote('panel-invites-note', `${(data.invites || []).length} 个可用`);
  panelNote('panel-audit-note', `最近 ${(data.audit || []).length} 条`);
  // The collapsed row carries the count that costs something: how many of these
  // will actually reach the inbox. A number that only ever said "3" tells the
  // operator nothing about whether they are about to be interrupted.
  const alerts = data.alerts || [];
  const openAlerts = alerts.filter((row) => row.open);
  const mailing = openAlerts.filter((row) => row.tier === 'mail' && !row.acknowledged).length;
  panelNote('panel-alerts-note',
    openAlerts.length ? `${openAlerts.length} 条 · ${mailing} 条会发邮件` : '一切正常',
    mailing ? 'bad' : (openAlerts.length ? 'warn' : ''));
  if (PANEL_LOADED.alerts) renderAdminAlerts(alerts);
  const signupCounts = data.signup_counts || {};
  panelNote('panel-signups-note',
    `${signupCounts.pending || 0} 待处理 · ${signupCounts.invited || 0} 已发码`);
  renderEditPicker(users);
  panelNote('panel-admins-note', `${(data.admins || []).length} 人可管理`);
  if (PANEL_LOADED.users) renderAdminUsers(users);
  if (PANEL_LOADED.admins) renderAdminRoster(data.admins || []);
  if (PANEL_LOADED.invites) renderAdminInvites(data.invites);
  if (PANEL_LOADED.signups) renderAdminSignups(data.signups || [], signupCounts);
  if (PANEL_LOADED.audit) renderAdminAudit(data.audit);
}

function renderAdminRoster(admins) {
  const box = $('admin-roster');
  if (!box) return;
  clear(box);
  if (!admins.length) {
    box.appendChild(el('p', 'help', '还没有管理员。'));
    return;
  }
  admins.forEach((row) => {
    const item = el('article', 'report');
    const head = el('div', 'spread');
    const title = el('div');
    title.appendChild(el('strong', null, row.email));
    title.appendChild(el('div', 'help',
      row.source === 'env'
        ? '来自服务器环境变量 INFE_PILOT_ADMIN_EMAILS —— 在后台不可移除'
        : `在后台授予${row.status && row.status !== 'active' ? '（账号当前：' + row.status + '）' : ''}`));
    head.appendChild(title);
    if (row.removable) {
      const actions = el('div', 'row');
      const button = el('button', 'danger', '收回管理员');
      button.addEventListener('click', () => adminRevoke(row.id, row.email));
      actions.appendChild(button);
      head.appendChild(actions);
    }
    item.appendChild(head);
    box.appendChild(item);
  });
}

function adminGrant() {
  const email = ($('admin-grant-email').value || '').trim();
  const password = $('admin-grant-password').value || '';
  if (!email) { setStatus('admins-status', '请填写要授予的邮箱。', 'error'); return; }
  if (!password) { setStatus('admins-status', '请重新输入你的登录密码。', 'error'); return; }
  adminGrantOrRevoke('/api/admin/admins', { email, password }, `已授予 ${email} 管理员权限。`);
}

function adminRevoke(userId, email) {
  // Re-authentication through a prompt rather than a second form: revocation is
  // rare, and the password is the one thing an attacker at an unattended browser
  // does not have.
  const password = prompt(`收回 ${email} 的管理员权限。\n\n请重新输入你自己的登录密码：`, '');
  if (password === null) return;
  adminGrantOrRevoke(`/api/admin/admins/${encodeURIComponent(userId)}/revoke`, { password },
    `已收回 ${email} 的管理员权限。`);
}

async function adminGrantOrRevoke(path, body, done) {
  setStatus('admins-status', '正在提交…');
  try {
    const data = await api(path, { method: 'POST', body: JSON.stringify(body) });
    adminData.admins = data.admins || [];
    renderAdminRoster(adminData.admins);
    if (data.audit) {
      adminData.audit = data.audit;
      if (PANEL_LOADED.audit) renderAdminAudit(data.audit);
    }
    panelNote('panel-admins-note', `${adminData.admins.length} 人可管理`);
    $('admin-grant-email').value = '';
    $('admin-grant-password').value = '';
    setStatus('admins-status', done, 'ok');
  } catch (error) {
    setStatus('admins-status', error.message, 'error');
  }
}

function renderEditPicker(users) {
  const select = $('edit-user');
  if (!select) return;
  const current = select.value;
  clear(select);
  const placeholder = el('option', null, '选择要修改的用户…');
  placeholder.value = '';
  select.appendChild(placeholder);
  users.forEach((row) => {
    const option = el('option', null, `${row.email}（${row.status}）`);
    option.value = row.id;
    select.appendChild(option);
  });
  select.value = users.some((row) => row.id === current) ? current : '';
  renderEditTarget();
}

function renderEditTarget(message, tone) {
  const select = $('edit-user');
  const box = $('admin-editor');
  if (!select || !box) return;
  clear(box);
  const row = (adminData.users || []).find((item) => item.id === select.value);
  if (!row) {
    box.appendChild(el('p', 'help', '先在上面选一个用户。'));
    return;
  }
  box.appendChild(renderAdminEditor(row));
  if (message) {
    const status = box.querySelector('.saved');
    if (status) {
      status.className = tone ? `saved ${tone}` : 'saved';
      status.textContent = message;
      status.style.display = 'block';
    }
  }
}

/* ---- every mail, and what happened to it -------------------------------
   One row per incoming mail, collapsed until clicked. The columns answer the
   operator's actual question — processed? delivered? — and nothing here reads
   the message or report body, so the console stays a delivery monitor rather
   than a mailbox reader.                                               */

const MAIL_STATE_TEXT = {
  sent: '已下发',
  failed: '失败',
  skipped: '已跳过',
  generated: '已生成未发出',
  pending: '处理中',
};

let mailBoard = { messages: [], total: 0, counts: {}, offset: 0, limit: 50 };
let adminData = { users: [], invites: [], audit: [], announcements: [], health: {} };

function mailMoment(value) {
  return momentText(value, { seconds: true, withZone: true });
}

function mailDuration(seconds) {
  if (seconds == null) return '—';
  if (seconds < 90) return `${Math.round(seconds)} 秒`;
  return `${Math.round(seconds / 60)} 分钟`;
}

function mailDetailRow(list, term, value) {
  if (value === undefined || value === null || value === '') return;
  list.appendChild(el('dt', null, term));
  list.appendChild(el('dd', null, String(value)));
}

function renderMailRow(row) {
  const details = el('details', 'mailrow');
  const summary = el('summary');
  const when = el('div', 'when', mailMoment(row.received_at));
  const title = el('div', 'title');
  title.appendChild(el('div', null, row.subject || '(无主题)'));
  title.appendChild(el('div', 'who', `${row.sender_name || row.sender_address || '未知发件人'} · ${row.user_email}`));
  const state = el('span', `mailstate ${row.delivery}`, MAIL_STATE_TEXT[row.delivery] || row.delivery);
  summary.appendChild(when);
  summary.appendChild(title);
  summary.appendChild(state);
  details.appendChild(summary);

  const body = el('div', 'maildetail');
  const list = el('dl');
  mailDetailRow(list, '发件人地址', row.sender_address);
  mailDetailRow(list, '收到的账号', row.user_email);
  mailDetailRow(list, '收信时间', mailMoment(row.received_at));
  mailDetailRow(list, '处理状态', row.status);
  if (row.delivery === 'sent') {
    mailDetailRow(list, '报告主题', row.report_subject);
    mailDetailRow(list, '发往', row.sent_to);
    mailDetailRow(list, '发出时间', mailMoment(row.sent_at));
    mailDetailRow(list, '端到端耗时', mailDuration(row.latency_seconds));
    if (!row.report_id) {
      // Migrated from the previous single-user service: delivered, but no
      // report record was ever kept for it.
      mailDetailRow(list, '说明', '迁移前由旧服务处理，没有留存报告记录');
    }
  }
  mailDetailRow(list, '跳过原因', row.skip_reason);
  mailDetailRow(list, '最近错误', row.last_error || row.report_error);
  mailDetailRow(list, '重试次数', row.attempts);
  mailDetailRow(list, '下次重试', row.next_attempt_at ? mailMoment(row.next_attempt_at) : '');
  mailDetailRow(list, '重要度', row.importance);
  mailDetailRow(list, 'IMAP UID', row.imap_uid);
  mailDetailRow(list, '去重用 Message-ID', row.message_key);
  body.appendChild(list);
  details.appendChild(body);
  return details;
}

function renderMailBoard() {
  const box = $('admin-messages');
  clear(box);
  const counts = $('mail-counts');
  clear(counts);
  [
    ['共收到', `${mailBoard.counts.all == null ? '—' : mailBoard.counts.all} 封`],
    ['已下发', `${mailBoard.counts.sent == null ? '—' : mailBoard.counts.sent} 封`],
    ['没发出去', `${mailBoard.counts.undelivered == null ? '—' : mailBoard.counts.undelivered} 封`],
    ['失败', `${mailBoard.counts.failed == null ? '—' : mailBoard.counts.failed} 封`],
    ['已跳过', `${mailBoard.counts.skipped == null ? '—' : mailBoard.counts.skipped} 封`],
    ['当前显示', `${mailBoard.messages.length} / ${mailBoard.total}`],
  ].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    counts.appendChild(cell);
  });

  const list = el('div', 'maillist');
  if (!mailBoard.messages.length) {
    list.appendChild(el('p', 'help', '这个筛选条件下没有邮件。'));
  } else {
    mailBoard.messages.forEach((row) => list.appendChild(renderMailRow(row)));
  }
  box.appendChild(list);
  $('mail-more-wrap').style.display = mailBoard.messages.length < mailBoard.total ? 'block' : 'none';
}

function mailQuery(offset) {
  const status = $('mail-filter').value || 'all';
  const userId = $('mail-user').value || '';
  const parts = [`status=${encodeURIComponent(status)}`, `limit=${mailBoard.limit}`, `offset=${offset}`];
  if (userId) parts.push(`user_id=${encodeURIComponent(userId)}`);
  return `/api/admin/messages?${parts.join('&')}`;
}

function renderMailSummary(counts, total) {
  const bad = Number(counts.undelivered || 0);
  const failed = Number(counts.failed || 0);
  const tone = failed ? 'bad' : (bad ? 'warn' : '');
  panelNote('panel-mail-note',
    `共 ${total} 封 · 已下发 ${counts.sent || 0} · 没发出去 ${bad} · 已跳过 ${counts.skipped || 0}`,
    tone);
}

async function loadMailSummary() {
  if (!state || !state.is_admin) return;
  try {
    const data = await api('/api/admin/messages?limit=1');
    mailBoard.counts = data.counts || {};
    mailBoard.total = data.total || 0;
    renderMailSummary(mailBoard.counts, mailBoard.total);
  } catch (error) {
    panelNote('panel-mail-note', '统计加载失败', 'bad');
  }
}

function renderUsageSummary(grand) {
  const unpriced = Number(grand.unpriced_calls || 0);
  panelNote('panel-usage-note',
    `${grand.calls || 0} 次调用 · ${Number(grand.total_tokens || 0).toLocaleString('zh-CN')} tokens · `
    + `${money(grand.cost, grand.currency)}${unpriced ? ` · ${unpriced} 次未计价` : ''}`,
    unpriced ? 'warn' : '');
}

async function loadUsageSummary() {
  if (!state || !state.is_admin) return;
  try {
    const days = $('usage-days').value || '30';
    const data = await api(`/api/admin/usage?days=${encodeURIComponent(days)}`);
    usageBoard.grand = data.grand_total || {};
    renderUsageSummary(usageBoard.grand);
  } catch (error) {
    panelNote('panel-usage-note', '统计加载失败', 'bad');
  }
}

async function loadMailBoard({ append = false, notify = false } = {}) {
  if (!state || !state.is_admin) return;
  const offset = append ? mailBoard.messages.length : 0;
  try {
    const data = await api(mailQuery(offset));
    mailBoard.counts = data.counts || {};
    mailBoard.total = data.total || 0;
    renderMailSummary(mailBoard.counts, mailBoard.total);
    mailBoard.messages = append ? mailBoard.messages.concat(data.messages) : data.messages;
    if (!append && data.users) {
      const select = $('mail-user');
      const current = select.value;
      clear(select);
      const all = el('option', null, '所有用户');
      all.value = '';
      select.appendChild(all);
      data.users.forEach((item) => {
        const option = el('option', null, item.email);
        option.value = item.id;
        select.appendChild(option);
      });
      select.value = current || '';
    }
    renderMailBoard();
    if (notify) toast(`邮件列表已刷新：共 ${mailBoard.total} 封`, 'ok');
  } catch (error) {
    setStatus('admin-status', `无法加载邮件列表：${error.message}`, 'error');
    if (notify) toast(`刷新邮件列表失败：${error.message}`, 'error');
  }
}

$('mail-filter').addEventListener('change', () => loadMailBoard());
$('mail-user').addEventListener('change', () => loadMailBoard());
$('mail-refresh').addEventListener('click', () => loadMailBoard({ notify: true }));
$('mail-more').addEventListener('click', () => loadMailBoard({ append: true }));

/* ---- per-user token usage and cost -------------------------------------
   Collapsed until clicked, like the mail board. Cost is an estimate from the
   provider's published rates; anything we have no price for is counted in the
   tokens column and shown as "价格未配置" instead of being costed at zero. */

let usageBoard = { users: [], days: 30, grand: {}, prices: [], known: [], currencyNote: '' };

function money(value, currency) {
  if (value == null) return '—';
  const symbol = (currency || 'USD') === 'USD' ? '$' : '';
  const digits = Math.abs(value) >= 1 ? 3 : 6;
  return `${symbol}${Number(value).toFixed(digits)}${symbol ? '' : ' ' + (currency || '')}`;
}

function tokenText(value) {
  if (value == null) return '—';
  return Number(value).toLocaleString('zh-CN');
}

function usageRow(user) {
  const details = el('details', 'userow');
  const summary = el('summary');
  const who = el('div', 'who');
  who.appendChild(el('strong', null, user.email));
  who.appendChild(el('div', 'help', `${user.calls} 次调用 · 最近 ${mailMoment(user.last_call_at)}`));
  const cost = el('div', 'cost', money(user.cost, user.currency));
  const tokens = el('div', 'tokens', `${tokenText(user.total_tokens)} tokens`);
  const unpriced = el('div', 'tokens', user.unpriced_calls ? `${user.unpriced_calls} 次未计价` : '');
  summary.appendChild(who);
  summary.appendChild(tokens);
  summary.appendChild(unpriced);
  summary.appendChild(cost);
  details.appendChild(summary);

  const body = el('div', 'usebreak');
  const input = Number(user.input_tokens || 0);
  const cached = Number(user.cached_input_tokens || 0);
  body.appendChild(el('div', 'help',
    `输入 ${tokenText(input)}（其中 ${tokenText(cached)} 命中缓存） · 输出 ${tokenText(user.output_tokens)}`
    + `（其中推理 ${tokenText(user.reasoning_tokens)}） · 合计 ${tokenText(user.total_tokens)} tokens`));

  if (user.models && user.models.length) {
    body.appendChild(el('h4', null, '按模型'));
    body.appendChild(usageTable(
      ['模型', '调用', '输入', '缓存命中', '输出', '推理', '花费'],
      user.models.map((row) => [
        `${row.provider} / ${row.model}`,
        tokenText(row.calls), tokenText(row.input_tokens), tokenText(row.cached_input_tokens),
        tokenText(row.output_tokens), tokenText(row.reasoning_tokens),
        row.unpriced_calls ? `${money(row.cost, user.currency)}（${row.unpriced_calls} 次未计价）` : money(row.cost, user.currency),
      ])));
  }
  if (user.daily && user.daily.length) {
    body.appendChild(el('h4', null, '按天（香港时间）'));
    body.appendChild(usageTable(
      ['日期', '调用', '输入', '缓存命中', '输出', '推理', '花费'],
      user.daily.map((row) => [
        row.day, tokenText(row.calls), tokenText(row.input_tokens), tokenText(row.cached_input_tokens),
        tokenText(row.output_tokens), tokenText(row.reasoning_tokens), money(row.cost, user.currency),
      ])));
  }
  if (!user.calls) {
    body.appendChild(el('p', 'help', '这个时间段内没有调用记录。'));
  }
  details.appendChild(body);
  return details;
}

function usageTable(headers, rows) {
  const table = el('table');
  const head = el('tr');
  headers.forEach((text) => head.appendChild(el('th', null, text)));
  table.appendChild(el('thead')).appendChild(head);
  const body = el('tbody');
  rows.forEach((cells) => {
    const tr = el('tr');
    cells.forEach((cell) => tr.appendChild(el('td', null, cell)));
    body.appendChild(tr);
  });
  table.appendChild(body);
  return table;
}

function renderUsageBoard() {
  const box = $('admin-usage');
  clear(box);
  const totals = $('usage-totals');
  clear(totals);
  const grand = usageBoard.grand || {};
  [
    ['期内调用', `${grand.calls == null ? '—' : grand.calls} 次`],
    ['期内 token', tokenText(grand.total_tokens)],
    ['其中缓存命中', tokenText(grand.cached_input_tokens)],
    ['其中推理', tokenText(grand.reasoning_tokens)],
    ['期内花费（估算）', money(grand.cost, grand.currency)],
    ['未计价调用', `${grand.unpriced_calls || 0} 次`],
  ].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    totals.appendChild(cell);
  });

  if (!usageBoard.users.length) {
    box.appendChild(el('p', 'help', '还没有用户。'));
  } else {
    usageBoard.users.forEach((user) => box.appendChild(usageRow(user)));
  }
  if (usageBoard.currencyNote) {
    box.appendChild(el('div', 'help', usageBoard.currencyNote));
  }
  renderPriceEditor();
}

function renderPriceEditor() {
  const box = $('usage-price-editor');
  if (!box) return;
  clear(box);
  const details = el('details', 'advanced');
  details.appendChild(el('summary', null, '价格设置（不在表里的模型不会被计费）'));
  const body = el('div', 'body');
  body.appendChild(el('p', 'help',
    '内置价目来自供应商公开页面（DeepSeek 官方价目表，读取于 2026-09-14）。价格随时会变，'
    + '这里可以覆盖成你自己的价格；改动只影响之后的调用，已有记录保留当时的价格。'));

  const rows = usageBoard.prices || [];
  if (rows.length) {
    body.appendChild(el('h4', null, '当前覆盖'));
    body.appendChild(usageTable(
      ['模型', '缓存命中/1M', '缓存未命中/1M', '输出/1M', '货币', ''],
      rows.map((row) => [`${row.provider} / ${row.model}`,
        Number(row.input_cache_hit).toFixed(4), Number(row.input_cache_miss).toFixed(4),
        Number(row.output).toFixed(4), row.currency,
        (() => {
          const remove = el('button', 'danger', '删除');
          remove.type = 'button';
          remove.addEventListener('click', () => savePrice(row.provider, row.model, true));
          const holder = el('div');
          holder.appendChild(remove);
          return holder;
        })()])));
  } else {
    body.appendChild(el('p', 'help', '还没有设置覆盖价格，当前使用内置价目表。'));
  }

  const known = usageBoard.known || [];
  if (known.length) {
    body.appendChild(el('h4', null, '内置价目（只读参考）'));
    body.appendChild(usageTable(
      ['模型', '缓存命中/1M', '缓存未命中/1M', '输出/1M', '货币'],
      known.map((row) => [`${row.provider} / ${row.model}`,
        Number(row.input_cache_hit).toFixed(4), Number(row.input_cache_miss).toFixed(4),
        Number(row.output).toFixed(4), row.currency || 'USD'])));
  }

  body.appendChild(el('h4', null, '新增 / 覆盖一个价格'));
  body.appendChild(el('div', 'help', '单位：每 100 万 token 的价格（不是每 1000）。'));
  const grid = el('div', 'pricegrid');
  const fields = {};
  [['provider', '供应商（如 deepseek）', 'deepseek'],
   ['model', '模型名（如 deepseek-chat）', 'deepseek-chat'],
   ['input_cache_hit', '缓存命中输入 /1M', '0.003'],
   ['input_cache_miss', '缓存未命中输入 /1M', '0.15'],
   ['output', '输出 /1M', '0.6'],
   ['currency', '货币', 'USD']].forEach(([key, label, placeholder]) => {
    const input = el('input');
    input.type = 'text';
    input.placeholder = placeholder;
    fields[key] = input;
    const wrap = el('div');
    wrap.appendChild(el('label', null, label));
    wrap.appendChild(input);
    grid.appendChild(wrap);
  });
  body.appendChild(grid);
  const status = el('div', 'saved');
  status.style.display = 'none';
  const save = el('button', null, '保存价格');
  save.type = 'button';
  save.addEventListener('click', async () => {
    const payload = {
      provider: fields.provider.value.trim(),
      model: fields.model.value.trim(),
      input_cache_hit: Number(fields.input_cache_hit.value || 0),
      input_cache_miss: Number(fields.input_cache_miss.value || 0),
      output: Number(fields.output.value || 0),
      currency: fields.currency.value.trim() || 'USD',
    };
    if (!payload.provider || !payload.model) {
      status.className = 'saved warn';
      status.textContent = '供应商和模型名都要填。';
      status.style.display = 'block';
      return;
    }
    save.disabled = true;
    try {
      await api('/api/admin/prices', { method: 'PUT', body: JSON.stringify(payload) });
      status.className = 'saved';
      status.textContent = `已保存 ${payload.provider} / ${payload.model} 的价格，之后的新调用按新价计算。`;
      status.style.display = 'block';
      await loadUsage();
      const reopened = document.querySelector('#usage-price-editor details.advanced');
      if (reopened) reopened.open = true;
    } catch (error) {
      status.className = 'saved warn';
      status.textContent = `保存失败：${error.message}`;
      status.style.display = 'block';
    } finally {
      save.disabled = false;
    }
  });
  const actions = el('div', 'actions');
  actions.appendChild(save);
  body.appendChild(actions);
  body.appendChild(status);
  details.appendChild(body);
  if (usageBoard.priceEditorOpen) details.open = true;
  details.addEventListener('toggle', () => { usageBoard.priceEditorOpen = details.open; });
  box.appendChild(details);
}

async function savePrice(provider, model, remove) {
  try {
    await api('/api/admin/prices', {
      method: 'PUT', body: JSON.stringify({ provider, model, remove: true }),
    });
    await loadUsage();
  } catch (error) {
    setStatus('admin-status', `价格删除失败：${error.message}`, 'error');
  }
}

async function loadUsage({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  const days = $('usage-days').value || '30';
  try {
    const data = await api(`/api/admin/usage?days=${encodeURIComponent(days)}`);
    usageBoard.users = data.users || [];
    usageBoard.grand = data.grand_total || {};
    usageBoard.prices = data.prices || [];
    usageBoard.known = data.known_prices || [];
    usageBoard.currencyNote = data.currency_note || '';
    usageBoard.days = data.days;
    renderUsageSummary(usageBoard.grand);
    renderUsageBoard();
    if (notify) toast(`用量已刷新：最近 ${usageBoard.days} 天`, 'ok');
  } catch (error) {
    setStatus('admin-status', `无法加载 token 统计：${error.message}`, 'error');
    if (notify) toast(`刷新用量失败：${error.message}`, 'error');
  }
}

$('usage-days').addEventListener('change', () => loadUsage());
$('usage-refresh').addEventListener('click', () => loadUsage({ notify: true }));

/* ---- broadcasts -------------------------------------------------------- */

function renderAnnouncements(rows) {
  const box = $('admin-announcements');
  if (!box) return;
  clear(box);
  const list = rows || [];
  const active = list.filter((row) => row.active);
  panelNote('panel-broadcast-note', active.length
    ? `正在显示：${active[0].title}`
    : `共 ${list.length} 条 · 当前没有生效的`, active.length ? 'warn' : '');

  if (!list.length) {
    box.appendChild(el('p', 'help', '还没有发过公告。'));
    return;
  }
  box.appendChild(el('h4', null, '历史公告'));
  list.forEach((row) => {
    const item = el('article', 'report');
    const head = el('div', 'spread');
    const title = el('div');
    title.appendChild(el('strong', null, row.title));
    title.appendChild(el('div', 'help',
      `${ANNOUNCEMENT_LABEL[row.tone] || row.tone} · 发布于 ${adminStamp(row.created_at)}`
      + (row.active ? ' · 正在显示' : ` · 已撤下 ${adminStamp(row.withdrawn_at)}`)
      + (row.is_public ? ` · 已在官网布告栏（${adminStamp(row.public_at)} 贴出）` : '')));
    head.appendChild(title);
    if (row.active) {
      const actions = el('div', 'row');
      // The board toggle is offered only for a notice that is still up: posting
      // a withdrawn one would put text back on the public web after the operator
      // took it down, which is the one thing "撤下" must be trusted not to do.
      const board = el('button', 'secondary',
        row.is_public ? '从布告栏撤下' : '贴到布告栏');
      board.addEventListener('click', () => toggleAnnouncementBoard(row));
      actions.appendChild(board);
      const withdraw = el('button', 'secondary', '撤下');
      withdraw.addEventListener('click', () => withdrawAnnouncement(row));
      actions.appendChild(withdraw);
      head.appendChild(actions);
    }
    item.appendChild(head);
    item.appendChild(el('div', 'help', row.body.slice(0, 200)));
    if (row.email_total) {
      item.appendChild(el('div', 'help',
        `邮件：共 ${row.email_total} 人 · 已发 ${row.email_sent} · 失败 ${row.email_failed}`
        + (row.email_failed ? '（失败的会在后台自动重试）' : '')));
    } else {
      item.appendChild(el('div', 'help', '仅站内广播，没有发邮件。'));
    }
    item.appendChild(el('div', 'help', `已有 ${row.dismissed} 人点过「我知道了」。`));
    box.appendChild(item);
  });
}

async function toggleAnnouncementBoard(row) {
  const next = !row.is_public;
  if (next && !confirm(`把这条贴到官网布告栏？\n\n${row.title}\n\n`
    + '布告栏在 / 首页，没登录的人、搜索引擎、路过的访客都看得到。')) return;
  try {
    const data = await api(`/api/admin/announcements/${encodeURIComponent(row.id)}/board`,
      { method: 'PUT', body: JSON.stringify({ public: next }) });
    renderAnnouncements(data.announcements);
    setStatus('admin-status', next
      ? `已把「${row.title}」贴到官网布告栏，刷新首页就能看到。`
      : `已把「${row.title}」从布告栏撤下。`, 'ok');
  } catch (error) {
    setStatus('admin-status', `布告栏操作失败：${error.message}`, 'error');
  }
}

async function withdrawAnnouncement(row) {
  if (!confirm(`撤下这条公告？所有用户下次打开网页就看不到了：\n\n${row.title}`)) return;
  try {
    const data = await api(`/api/admin/announcements/${encodeURIComponent(row.id)}/withdraw`,
      { method: 'PUT', body: JSON.stringify({}) });
    renderAnnouncements(data.announcements);
    setStatus('admin-status', `已撤下公告「${row.title}」。`, 'ok');
  } catch (error) {
    setStatus('admin-status', `撤下失败：${error.message}`, 'error');
  }
}

$('broadcast-publish').addEventListener('click', async () => {
  const title = $('broadcast-title').value.trim();
  const body = $('broadcast-body').value.trim();
  const status = $('broadcast-status');
  const button = $('broadcast-publish');
  const withEmail = $('broadcast-delivery').value === 'email';
  const toBoard = $('broadcast-public').checked;
  status.style.display = 'block';
  if (!title || !body) {
    status.className = 'saved warn';
    status.textContent = '标题和内容都要填。';
    return;
  }
  if (withEmail && !confirm(`发布并同时给每个用户的私人邮箱发一封邮件？\n\n${title}`)) return;
  if (toBoard && !confirm(`同时贴到官网布告栏？\n\n${title}\n\n`
    + '布告栏在 / 首页，没登录的人、搜索引擎、路过的访客都看得到。')) return;
  button.disabled = true;
  try {
    const data = await api('/api/admin/announcements', {
      method: 'POST',
      body: JSON.stringify({
        title, body, tone: $('broadcast-tone').value,
        deliver_email: withEmail, public: toBoard,
      }),
    });
    status.className = 'saved';
    // The scope is spelled out every time, and the board is a separate clause
    // ("另外") rather than folded into it: the two audiences are different, and
    // a receipt reading "仅站内广播" while the notice also went onto the open web
    // would be the operator's last word on what happened being wrong.
    status.textContent = '已发布'
      + (withEmail ? '：站内广播 + 已排队给每个用户发一封邮件（这里不会等）。'
                   : '：仅站内广播，用户下次打开网页就会看到。')
      + (toBoard ? '另外已贴到官网布告栏，刷新首页即可看到。' : '');
    $('broadcast-title').value = '';
    $('broadcast-body').value = '';
    $('broadcast-public').checked = false;
    renderAnnouncements(data.announcements);
    if (state) { try { await refreshDashboard(); } catch (e) {} }
  } catch (error) {
    status.className = 'saved warn';
    status.textContent = `发布失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
});

function renderAdminSignups(signups, counts) {
  panelNote('panel-signups-note', `${counts.pending || 0} 待处理 · ${counts.invited || 0} 已发码`);
  const box = $('admin-signups');
  if (!box) return;
  clear(box);
  if (!signups.length) {
    box.appendChild(el('p', 'help', '还没有人从网站申请。'));
    return;
  }
  signups.forEach((row) => {
    const item = el('article', 'report');
    const head = el('div', 'spread');
    const title = el('div');
    title.appendChild(el('strong', null, row.email));
    const badge = row.status === 'pending' ? '待处理' : row.status === 'invited' ? '已发邀请码' : '已婉拒';
    title.appendChild(el('div', 'help',
      `${badge} · 申请于 ${adminStamp(row.created_at)}${row.decided_at ? ' · 处理于 ' + adminStamp(row.decided_at) : ''}`));
    head.appendChild(title);
    const actions = el('div', 'row');
    if (row.status === 'pending') {
      const invite = el('button', null, '发邀请码');
      invite.addEventListener('click', () => decideSignup(row.id, 'invited'));
      const decline = el('button', 'secondary', '婉拒');
      decline.addEventListener('click', () => decideSignup(row.id, 'declined'));
      actions.appendChild(invite);
      actions.appendChild(decline);
    } else {
      const reopen = el('button', 'secondary', '重新处理');
      reopen.addEventListener('click', () => decideSignup(row.id, 'pending'));
      actions.appendChild(reopen);
    }
    head.appendChild(actions);
    item.appendChild(head);
    if (row.note) item.appendChild(el('div', 'help', `留言：${row.note}`));
    // What became of the invite e-mail, next to the decision that sent it. The
    // send result used to exist only in the response to that one click, so a day
    // later nobody could say whether an applicant had been mailed at all.
    if (row.status === 'invited') {
      let mail;
      if (row.invite_sent_at) mail = `邀请码邮件：已投递给邮件服务器 · ${adminStamp(row.invite_sent_at)}`;
      else if (row.invite_send_error) mail = `邀请码邮件：发送失败 — ${row.invite_send_error}`;
      else mail = '邀请码邮件：未发送（当时选了不发，或还没尝试）';
      const line = el('div', 'help', mail);
      if (row.invite_send_error) line.style.color = 'var(--bad)';
      item.appendChild(line);
      if (row.invite_message_id) {
        item.appendChild(el('div', 'help', `Message-ID：${row.invite_message_id}`));
      }
      if (row.invite_used_by) {
        const used = el('div', 'help',
          `已被使用注册${row.redeemer_email ? '（' + row.redeemer_email + '）' : ''} —— 邮件确实到达过的最强证据。`);
        used.style.color = 'var(--ok-ink)';
        item.appendChild(used);
      } else if (row.invite_sent_at) {
        item.appendChild(el('div', 'help',
          '尚未被使用。刚发出属正常；超过三天还没用，先问他有没有收到（多半在垃圾邮件箱）。'));
      }
    }
    box.appendChild(item);
  });
}

async function decideSignup(requestId, status) {
  try {
    const data = await api(`/api/admin/signups/${encodeURIComponent(requestId)}`, {
      method: 'POST', body: JSON.stringify({ status }),
    });
    adminData.signups = data.signups;
    adminData.signup_counts = data.signup_counts;
    adminData.invites = data.invites;
    renderAdminSignups(data.signups || [], data.signup_counts || {});
    renderAdminInvites(data.invites || []);
    if (data.code) {
      // Shown once and never stored in the clear, so it is put in front of the
      // operator rather than logged or kept anywhere.
      // Rendered into this panel, not the invites one: that panel is collapsed
      // while the operator is working here, so a code placed there would be
      // written somewhere they cannot see.
      const box = $('signups-invite-result');
      clear(box);
      const line = el('div', 'status ok');
      line.appendChild(document.createTextNode(`给 ${data.signup.email} 的邀请码（只显示这一次）：`));
      const code = el('code', null, data.code);
      line.appendChild(code);
      const copy = el('button', 'secondary', '复制');
      copy.addEventListener('click', () => {
        navigator.clipboard.writeText(data.code).then(
          () => toast('邀请码已复制', 'ok'),
          () => toast('复制失败，请手动选中', 'error'));
      });
      line.appendChild(copy);
      box.appendChild(line);
      // Whether it was delivered is the operator's business: the code is shown
      // either way, but a silent send failure would leave them assuming the
      // applicant got an e-mail that never left.
      if (data.emailed) {
        const sent = el('div', 'help', `已同时发到 ${data.signup.email}。`);
        box.appendChild(sent);
        toast(`邀请码已生成并发送给 ${data.signup.email}`, 'ok');
      } else {
        const failed = el('div', 'help', '邮件没能发出去（见下），请手动把上面的码发给对方。');
        box.appendChild(failed);
        if (data.email_error) box.appendChild(el('div', 'task-archived', data.email_error));
        toast('邀请码已生成，但邮件发送失败——请手动转达', 'warn');
      }
    } else {
      clear($('signups-invite-result'));
      toast(status === 'declined' ? '已婉拒' : '已恢复为待处理', 'ok');
    }
  } catch (error) {
    toast(`操作失败：${error.message}`, 'error');
  }
}

function renderAdminInvites(invites) {
  const box = $('admin-invites');
  clear(box);
  if (!invites.length) { box.appendChild(el('p', 'help', '还没有邀请码。')); return; }
  const list = el('ul', 'activity');
  invites.forEach((row) => {
    const item = el('li');
    item.appendChild(el('div', 'task-action', row.label || '（无备注）'));
    item.appendChild(el('div', 'help', `${row.state === 'available' ? '可用' : row.state === 'used' ? '已使用' : '已过期'}`
      + ` · 到期 ${adminStamp(row.expires_at)}`
      + (row.used_by_email ? ` · 使用者 ${row.used_by_email}` : '')));
    if (row.state === 'available') {
      const revoke = el('button', 'secondary', '撤销');
      revoke.addEventListener('click', () => adminExpireInvite(row.label));
      const wrap = el('div', 'actions');
      wrap.appendChild(revoke);
      item.appendChild(wrap);
    }
    list.appendChild(item);
  });
  box.appendChild(list);
}

/* ------------------------------------------------------- server metrics */

// "Real time" here means a 3-second poll that only runs while the admin tab is
// open and the page is visible, and that keeps its own history in the browser.
// No server-side state, no websocket to babysit through the reverse proxy, and
// nothing to leak if an operator leaves the tab open overnight.
const METRICS_INTERVAL_MS = 3000;
const METRICS_HISTORY = 40;
const metricsHistory = { cpu: [], memory: [] };
let metricsTimer = null;

function sparkline(values, ceiling) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 100 30');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('aria-hidden', 'true');
  if (values.length < 2) return svg;
  const top = ceiling || Math.max(...values, 1);
  const step = 100 / (values.length - 1);
  const points = values
    .map((value, index) => `${(index * step).toFixed(1)},${(30 - Math.min(1, value / top) * 28 - 1).toFixed(1)}`)
    .join(' ');
  const line = document.createElementNS(ns, 'polyline');
  line.setAttribute('points', points);
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'currentColor');
  line.setAttribute('stroke-width', '1.6');
  line.setAttribute('vector-effect', 'non-scaling-stroke');
  svg.appendChild(line);
  return svg;
}

function metricCard(label, value, options = {}) {
  const card = el('div', `metriccard${options.level ? ' ' + options.level : ''}`);
  card.appendChild(el('small', null, label));
  card.appendChild(el('b', null, value));
  if (options.spark) {
    // The <svg> needs the .spark wrapper: that is what carries the height and
    // the theme colour, and without it the chart renders as a zero-height box.
    const box = el('div', 'spark');
    box.appendChild(sparkline(options.spark, options.ceiling));
    card.appendChild(box);
  }
  if (typeof options.percent === 'number') {
    const bar = el('div', 'bar');
    const fill = el('i');
    fill.style.width = `${Math.max(0, Math.min(100, options.percent))}%`;
    bar.appendChild(fill);
    card.appendChild(bar);
  }
  return card;
}

function levelFor(percent) {
  if (typeof percent !== 'number') return '';
  if (percent >= 90) return 'bad';
  if (percent >= 75) return 'warn';
  return '';
}

function humanDuration(seconds) {
  if (typeof seconds !== 'number' || !isFinite(seconds)) return '—';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days} 天 ${hours} 小时`;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return `${minutes} 分 ${Math.floor(seconds % 60)} 秒`;
  return `${Math.round(seconds)} 秒`;
}

function numberOrDash(value, suffix = '', digits = 0) {
  if (typeof value !== 'number' || !isFinite(value)) return '—';
  return `${value.toFixed(digits)}${suffix}`;
}

function renderMetrics(snapshot) {
  const host = snapshot.host || {};
  const memory = host.memory || {};
  const disk = host.disk || {};
  const process = snapshot.process || {};
  const app = snapshot.application || {};

  const cpu = host.cpu_percent;
  const memPercent = memory.percent;
  if (typeof cpu === 'number') {
    metricsHistory.cpu.push(cpu);
    if (metricsHistory.cpu.length > METRICS_HISTORY) metricsHistory.cpu.shift();
  }
  if (typeof memPercent === 'number') {
    metricsHistory.memory.push(memPercent);
    if (metricsHistory.memory.length > METRICS_HISTORY) metricsHistory.memory.shift();
  }

  const grid = $('metrics-host');
  clear(grid);
  const load = Array.isArray(host.load) ? host.load.join(' / ') : '—';
  const cards = [
    ['CPU', numberOrDash(cpu, '%', 1), {
      percent: cpu, level: levelFor(cpu), spark: metricsHistory.cpu, ceiling: 100,
    }],
    ['内存', numberOrDash(memPercent, '%', 1), {
      percent: memPercent, level: levelFor(memPercent), spark: metricsHistory.memory, ceiling: 100,
    }],
    ['磁盘 /', numberOrDash(disk.percent, '%', 1), { percent: disk.percent, level: levelFor(disk.percent) }],
    ['负载 1/5/15', load, {}],
    ['网络 ↓ / ↑', `${numberOrDash(host.network && host.network.rx_kbps, '', 1)} / ${numberOrDash(host.network && host.network.tx_kbps, '', 1)} KB/s`, {}],
    ['主机运行', humanDuration(host.uptime_seconds), {}],
    ['Web 进程内存', numberOrDash(process.rss_mb, ' MB', 1), {}],
    ['Web 线程 / fd', `${process.threads == null ? '—' : process.threads} / ${process.open_files == null ? '—' : process.open_files}`, {}],
  ];
  cards.forEach(([label, value, options]) => grid.appendChild(metricCard(label, value, options)));
  $('metrics-stamp').textContent = `${host.cpu_count || '—'} 核 · ${host.platform || ''} · ${adminStamp(snapshot.collected_at)}`;
  // The number the operator needs most must be readable while collapsed.
  panelNote('panel-metrics-note',
    `CPU ${numberOrDash(cpu, '%', 1)} · 内存 ${numberOrDash(memPercent, '%', 1)}`
    + ` · 磁盘 ${numberOrDash(disk.percent, '%', 1)}`
    + ` · 队列 ${app.queue == null ? '—' : app.queue}`,
    levelFor(cpu) || levelFor(memPercent) || levelFor(disk.percent) || '');

  const appBox = $('metrics-app');
  clear(appBox);
  [
    ['近 1 小时邮件', `${app.messages_1h == null ? '—' : app.messages_1h} 封`],
    ['近 24 小时邮件', `${app.messages_24h == null ? '—' : app.messages_24h} 封`],
    ['近 24 小时已发', `${app.sent_24h == null ? '—' : app.sent_24h} 份`],
    ['最近一封耗时', app.latest_latency_seconds == null ? '—' : humanDuration(app.latest_latency_seconds)],
    ['24h 中位耗时', app.median_latency_seconds == null ? '—' : humanDuration(app.median_latency_seconds)],
    ['待处理队列', `${app.queue == null ? '—' : app.queue} 封`],
    ['失败邮件', `${app.failed == null ? '—' : app.failed} 封`],
    ['非本校跳过', `${app.skipped_24h == null ? '—' : app.skipped_24h} 封`],
    ['数据库大小', numberOrDash(app.database_size_mb, ' MB', 2)],
  ].forEach(([label, value]) => {
    const cell = el('div');
    cell.appendChild(el('small', null, label));
    cell.appendChild(el('b', null, value));
    appBox.appendChild(cell);
  });

  const notes = [];
  if (app.median_latency_seconds == null) notes.push('还没有已发送的报告，暂时算不出端到端耗时。');
  else {
    // The mean is shown last on purpose: one backlog (a batch of old mails sent
    // in one go) drags it to many hours while the median stays at "minutes".
    const mean = app.average_latency_seconds == null ? '—' : humanDuration(app.average_latency_seconds);
    notes.push(`耗时口径 = 收到信 → 报告发出。「最近一封」看当下速度，24 小时窗口里 ${app.latency_samples} 份的中位 ${humanDuration(app.median_latency_seconds)}、P90 ${humanDuration(app.p90_latency_seconds)}、平均 ${mean}；一次性补发积压邮件会把平均值拉高。`);
  }
  if (app.last_poll_at) notes.push(`最近一次轮询：${adminStamp(app.last_poll_at)}`);
  if (app.wal_size_mb) notes.push(`WAL ${app.wal_size_mb} MB`);
  if (host.cpu_percent == null) notes.push('这台机器读不到 /proc，CPU 与内存需要 Linux。');
  $('metrics-note').textContent = notes.join(' · ');
}

async function loadMetrics({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  if (document.hidden) return;
  try {
    renderMetrics(await api('/api/admin/metrics'));
    if (notify) toast('服务器指标已刷新', 'ok');
  } catch (error) {
    if (metricsTimer) stopMetrics();
    $('metrics-note').textContent = `无法读取服务器指标：${error.message}`;
    if (notify) toast(`刷新指标失败：${error.message}`, 'error');
  }
}

function startMetrics() {
  stopMetrics();
  loadMetrics();
  metricsTimer = setInterval(loadMetrics, METRICS_INTERVAL_MS);
}

function stopMetrics() {
  if (metricsTimer) clearInterval(metricsTimer);
  metricsTimer = null;
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && activeSection === 'admin' && !metricsTimer) startMetrics();
  if (document.hidden) stopMetrics();
});

async function loadAdmin({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  setStatus('admin-status', '加载中…');
  try {
    const data = await api('/api/admin/users');
    adminData = data;
    renderAdminHealth(data.health);
    renderAdminPanels(data);
    // Cheap aggregate calls so the collapsed summaries can carry real numbers;
    // the full lists are only fetched when a panel is opened.
    await Promise.all([loadMailSummary(), loadUsageSummary(), loadCapacity(), loadAgent()]);
    if (panelIsOpen('panel-mail')) await loadMailBoard();
    if (panelIsOpen('panel-usage')) await loadUsage();
    if (panelIsOpen('panel-metrics')) startMetrics();
    if (notify) toast(`管理数据已刷新：${(data.users || []).length} 个账号`, 'ok');
  } catch (error) {
    setStatus('admin-status', `无法加载管理数据：${error.message}`, 'error');
    if (notify) toast(`刷新管理数据失败：${error.message}`, 'error');
  }
}

async function adminSetStatus(userId, status, email) {
  const body = {};
  if (status === 'deleted') {
    // Irreversible: require the account e-mail to be typed. Reversible actions
    // (pause/resume) keep a single click, because extra friction on a phone
    // keyboard mostly teaches people to copy-paste instead of reading.
    const typed = prompt(
      `删除是不可恢复操作，会清除该用户的邮箱授权码、API key 和报告记录。\n\n请输入该用户的完整邮箱以确认：\n${email}`,
      '',
    );
    if (typed === null) return;
    body.confirm_email = typed.trim();
  } else {
    const wording = status === 'paused' ? '暂停该用户（停止收信与发信）' : '恢复该用户';
    if (!confirm(`${wording}：${email}？`)) return;
  }
  try {
    const data = await api(`/api/admin/users/${encodeURIComponent(userId)}/status/${status}`, {
      method: 'PUT', body: JSON.stringify(body),
    });
    renderAdminUsers(data.users);
    await loadAdmin();
  } catch (error) {
    setStatus('admin-status', error.message, 'error');
  }
}

function renderAdminAudit(entries) {
  const box = $('admin-audit');
  if (!box) return;
  clear(box);
  if (!entries || !entries.length) {
    box.appendChild(el('p', 'help', '还没有管理操作记录。'));
    return;
  }
  const labels = {
    user_status_active: '恢复用户', user_status_paused: '暂停用户', user_status_deleted: '删除用户',
    invite_created: '生成邀请码', invite_revoked: '撤销邀请码',
    password_changed: '修改密码', signed_out_all_devices: '退出所有设备',
  };
  const list = el('ul', 'activity');
  entries.forEach((entry) => {
    const item = el('li');
    const action = labels[entry.action] || entry.action;
    item.appendChild(el('div', 'task-action', `${action}${entry.target_email ? ' · ' + entry.target_email : ''}`));
    item.appendChild(el('div', 'help', `${adminStamp(entry.created_at)} · 操作者 ${entry.actor_email || '-'}${entry.detail ? ' · ' + entry.detail : ''}`));
    list.appendChild(item);
  });
  box.appendChild(list);
}

async function adminExpireInvite(label) {
  if (!confirm(`撤销邀请码「${label}」？撤销后该码无法再注册。`)) return;
  try {
    const data = await api(`/api/admin/invites/${encodeURIComponent(label)}`, { method: 'DELETE' });
    renderAdminInvites(data.invites);
    setStatus('admin-status', `已撤销 ${data.retired} 个邀请码。`, 'ok');
  } catch (error) {
    setStatus('admin-status', error.message, 'error');
  }
}

$('admin-refresh').addEventListener('click', () => loadAdmin({ notify: true }));
// Arrow, not the bare function: addEventListener passes the Event as the first
// argument, and renderEditTarget's first parameter is the receipt text.
$('edit-user').addEventListener('change', () => renderEditTarget());

wirePanel('panel-edit', () => { PANEL_LOADED.users = true; renderEditPicker(adminData.users || []); });

/* ---- pilot capacity ----------------------------------------------------- */

/* How many accounts the pilot admits used to be an environment variable, which
 * meant editing a 0600 root-owned file over SSH and restarting the service. It
 * now lives in the database so it can be changed here. The recommendation next
 * to it is computed from measurements and names the limit that produced it, so
 * an operator who disagrees can see the arithmetic rather than take a number on
 * faith. */

let capacityState = null;

async function loadCapacity({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  try {
    capacityState = await api('/api/admin/capacity');
    renderCapacity();
    if (notify) toast('名额评估已刷新', 'ok');
  } catch (error) {
    setStatus('admin-status', `无法评估名额：${error.message}`, 'error');
    if (notify) toast(`刷新名额失败：${error.message}`, 'error');
  }
}

function renderCapacity() {
  const data = capacityState;
  if (!data) return;
  const input = $('capacity-input');
  // Never overwrite what the operator is in the middle of typing.
  if (document.activeElement !== input) input.value = data.current;

  const tight = data.recommended <= data.current;
  panelNote('panel-capacity-note', `${data.current} 个 · 建议 ${data.recommended}`,
            tight ? 'warn' : '');
  $('capacity-source').textContent = data.source === 'settings'
    ? '当前名额来自这里的设置。'
    : '当前名额来自服务器的环境默认值（还没有在后台改过）。';

  const box = $('capacity-advice');
  clear(box);
  const grid = el('div', 'metrics');
  const add = (label, value) => {
    const cell = el('div');
    cell.appendChild(el('div', 'help', label));
    cell.appendChild(el('b', null, value));
    grid.appendChild(cell);
  };
  const bindingLabel = { generation: '模型生成速度', cpu: 'CPU', memory: '内存',
                         disk: '磁盘', single_box: '单机试点上限' };
  const confidenceLabel = { high: '高', medium: '中', low: '低（数据还不够）' };
  add('建议名额', String(data.recommended));
  add('限制来自', bindingLabel[data.binding] || data.binding);
  add('置信度', confidenceLabel[data.confidence] || data.confidence);
  // Labelled as the interval it actually is: see the note on
  // Database.recent_volume — it bounds the per-report cost from above rather
  // than measuring generation, and calling it "每份报告耗时" would be a claim
  // the data does not support.
  add('报告间隔（上界）', `${data.measured.report_seconds} 秒`
      + (data.measured.report_seconds_from_data ? '' : '（默认值）'));
  add('每人每天来信', `${data.measured.mails_per_user_day} 封`);
  add('每天可生成', `${data.measured.reports_per_day} 份`);
  box.appendChild(grid);

  const lines = [`判断依据：${data.binding_reason}`];
  if (data.load.length) lines.push(`当前负载：${data.load.join(' · ')}`);
  data.notes.forEach((note) => lines.push(note));
  $('capacity-explain').textContent = lines.join('\n');

  $('capacity-adopt').disabled = data.recommended === data.current;
}

async function saveCapacity(value, { reset = false } = {}) {
  try {
    const body = reset ? { reset: true } : { max_users: value };
    const data = await api('/api/admin/capacity', { method: 'PUT', body: JSON.stringify(body) });
    toast(reset ? `已恢复环境默认：${data.max_users} 个` : `名额已改为 ${data.max_users} 个`, 'ok');
    await loadCapacity();
    await loadAdmin();
  } catch (error) {
    toast(`修改名额失败：${error.message}`, 'error');
  }
}

wirePanel('panel-capacity', () => { loadCapacity(); });
$('capacity-refresh').addEventListener('click', () => loadCapacity({ notify: true }));
$('capacity-save').addEventListener('click', () => {
  const value = Number($('capacity-input').value);
  if (!Number.isInteger(value) || value < 1) {
    toast('名额需要是 1 以上的整数', 'error');
    return;
  }
  saveCapacity(value);
});
$('capacity-adopt').addEventListener('click', () => {
  if (capacityState) saveCapacity(capacityState.recommended);
});
$('capacity-reset').addEventListener('click', () => saveCapacity(null, { reset: true }));

$('admin-grant').addEventListener('click', adminGrant);

/* ---- AI 运维助手 ---------------------------------------------------------
   Read-only by construction, and that is visible in this code: the model's
   answer is rendered as text and nothing else. This panel cannot make the model
   do anything, the recipient is never the model's choice, and links never
   survive into the report (the server strips them before they are stored).

   The only paid action here is the explicit button, and it goes through the
   same daily budget and cooldown as the automatic path in the sentinel.      */

const AGENT_STATUS_TEXT = {
  ok: '已分析', reused: '沿用上次', skipped: '未分析', failed: '分析失败',
};

/* The analysis, laid out as the sections it was asked for.
 *
 * The server renders the same structure into the alert e-mail; this is the
 * console's copy. The reports arrive as one text blob and used to be dropped
 * into a single <div> -- ten of them, each ~2000 characters of run-on prose,
 * which is what "太凌乱" was about.
 *
 * The parser is deliberately forgiving. Two of the first three production
 * reports drifted off the template (one dropped the 【】 marks entirely), so
 * anything unrecognised falls back to the raw text rather than to nothing: the
 * operator must always be able to read what they paid for.
 */
const ANALYSIS_SECTIONS = ['结论', '依据', '可能的原因', '建议', '怎么验证', '看到的'];
const ANALYSIS_ACTION = '建议动作';

function parseAnalysis(text) {
  const heads = ANALYSIS_SECTIONS.concat([ANALYSIS_ACTION])
    .sort((a, b) => b.length - a.length)
    .map((h) => h.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('|');
  const head = new RegExp(`^\\s*[【\\[]?\\s*(${heads})\\s*[】\\]]?\\s*[:：]?\\s*(.*)$`);
  const sections = [];
  let seen = false;
  String(text || '').split('\n').forEach((raw) => {
    const line = raw.replace(/\s+$/, '');
    const match = head.exec(line);
    if (match) {
      seen = true;
      if (match[1] === ANALYSIS_ACTION) return;   // shown as a labelled button instead
      const rest = (match[2] || '').trim();
      const section = { head: match[1], items: rest ? [rest] : [] };
      sections.push(section);
      return;
    }
    const item = line.replace(/^\s*(?:[-*•·]|\d+[.、)])\s*/, '').trim();
    if (!item) return;
    if (sections.length) sections[sections.length - 1].items.push(item);
  });
  if (!seen) return [];
  return sections.filter((section) => section.items.length);
}

function renderAnalysis(holder, text) {
  const sections = parseAnalysis(text);
  if (!sections.length) {
    holder.appendChild(el('div', 'analysis-raw', text || '（空）'));
    return;
  }
  sections.forEach((section) => {
    const block = el('div', 'analysis-section');
    block.appendChild(el('div', 'analysis-head', section.head));
    const list = el('ul', 'analysis-items');
    section.items.forEach((item) => list.appendChild(el('li', null, item)));
    block.appendChild(list);
    holder.appendChild(block);
  });
}

function renderAgent(data) {
  const on = Boolean(data.enabled);
  const budget = data.budget || {};
  const reports = data.reports || [];
  const limits = data.limits || {};

  const toggle = $('agent-toggle');
  if (toggle) {
    toggle.textContent = on ? '关闭助手' : '开启助手';
    toggle.className = on ? 'ghost' : '';
    toggle.disabled = false;
  }
  const run = $('agent-run');
  if (run) {
    // Disabled rather than allowed to fail: the button spends money, and a
    // button that looks live but always errors is how an operator learns to
    // distrust the panel.
    run.disabled = !(on && data.has_model_key && (budget.remaining || 0) > 0);
  }
  panelNote('panel-agent-note',
    `${on ? '已开启' : '已关闭'} · 24 小时 ${budget.used || 0}/${budget.limit || 0} 次`,
    on ? '' : '');

  const stateBox = $('agent-state');
  if (stateBox) {
    const lines = [on
      ? '助手已开启：出现新异常时会自动分析，并把结论附在同一封告警邮件里。'
      : '助手已关闭：不会调用模型，也就不产生费用。'];
    if (!data.has_model_key) lines.push('没有可用的实例级模型 key，暂时无法分析。');
    if ((budget.remaining || 0) <= 0) lines.push('24 小时额度已用完，之后自动恢复。');
    lines.push(`每次告警最多分析 ${limits.per_mail} 条；同一异常 ${limits.cooldown_hours} 小时内只调用一次；`
      + '额度用尽时会跳过分析，原始告警照常发出。');
    stateBox.textContent = lines.join('\n');
  }

  const box = $('agent-reports');
  if (!box) return;
  // Which rows the operator had open. Confirming an action re-renders this
  // whole list, and a fresh <details> defaults to closed -- so the row they
  // were reading (the one they just pressed a button in) would fold itself shut
  // under them, and the "已确认" that replaced the button lands off-screen.
  const wasOpen = new Set();
  box.querySelectorAll('details[data-key]').forEach((node) => {
    if (node.open) wasOpen.add(node.dataset.key);
  });
  clear(box);
  if (!reports.length) {
    box.appendChild(el('p', 'help', '还没有分析记录。'));
    return;
  }
  // Which action the assistant may name, and what each one means. It travels
  // from the server so a button can only ever say something the server would
  // accept -- the label and the key come from the same catalogue.
  const catalogue = {};
  (data.actions || []).forEach((entry) => { catalogue[entry.key] = entry; });
  // A suggestion already waiting for the worker. Without this the second press
  // is a 422 toast -- a button that looks live and always fails, which is how
  // the operator learns not to trust the panel.
  const queued = {};
  (data.action_log || []).forEach((entry) => {
    if (entry.status === 'requested') queued[entry.report_id] = entry;
  });

  reports.forEach((row) => {
    const item = el('details', 'report-item');
    item.dataset.key = row.id;
    if (wasOpen.has(row.id)) item.open = true;
    const summary = el('summary');
    summary.appendChild(el('strong', null, row.title || row.finding_key));
    summary.appendChild(el('span', 'help',
      ` ${adminStamp(row.created_at)} · ${row.model || '—'} · ${row.total_tokens || 0} tokens`
      + (row.action ? ` · 建议：${(catalogue[row.action] || {}).label || row.action}` : '')));
    item.appendChild(summary);
    const body = el('div', 'report-body');
    renderAnalysis(body, row.text);
    item.appendChild(body);

    // The proposal, and the only way it can happen. The assistant *named* this;
    // nothing runs until somebody presses here, and the request carries no
    // action name -- the server reads it back off the report.
    const entry = row.action ? catalogue[row.action] : null;
    if (entry) {
      const bar = el('div', 'adminnote');
      bar.appendChild(el('label', null, `助手建议：${entry.label}`));
      bar.appendChild(el('div', 'help', entry.detail || ''));
      if (queued[row.id]) {
        // Answered in place, not by removing the row: "I asked for this and it
        // is on its way" has to stay readable, including after a reload.
        bar.appendChild(el('div', 'help', '已确认，等待 worker 执行。'));
      } else {
        const buttons = el('div', 'row');
        const confirmButton = el('button', null, '确认执行');
        confirmButton.addEventListener('click', () => agentConfirmAction(row.id, confirmButton));
        buttons.appendChild(confirmButton);
        bar.appendChild(buttons);
      }
      item.appendChild(bar);
    }
    box.appendChild(item);
  });

  const logBox = $('agent-actions');
  if (logBox) {
    clear(logBox);
    const log = data.action_log || [];
    if (log.length) {
      logBox.appendChild(el('div', 'help', '最近确认过的动作：'));
      log.forEach((entry) => {
        const line = `${adminStamp(entry.requested_at)} · ${entry.action} · `
          + (entry.status === 'done' ? '已完成' : entry.status === 'failed' ? '失败' : '等待 worker 执行')
          + (entry.result ? ` · ${entry.result}` : '');
        logBox.appendChild(el('div', 'help', line));
      });
    }
  }
}

async function agentConfirmAction(reportId, button) {
  // The action name is deliberately not sent: the server reads it off the
  // report, so this button can only ever confirm what was actually proposed.
  button.disabled = true;
  try {
    await api(`/api/admin/agent/reports/${encodeURIComponent(reportId)}/act`, {
      method: 'POST', body: JSON.stringify({}),
    });
    toast('已确认，等待 worker 执行', 'ok');
  } catch (error) {
    button.disabled = false;
    toast(`确认失败：${error.message}`, 'error');
  }
  await loadAgent();
}

async function loadAgent({ notify = false } = {}) {
  if (!state || !state.is_admin) return;
  try {
    renderAgent(await api('/api/admin/agent'));
    if (notify) toast('助手状态已刷新', 'ok');
  } catch (error) {
    panelNote('panel-agent-note', '加载失败', 'bad');
    if (notify) toast(`无法读取助手状态：${error.message}`, 'error');
  }
}

async function agentToggle() {
  const button = $('agent-toggle');
  const turningOn = button.textContent.includes('开启');
  button.disabled = true;
  try {
    await api('/api/admin/agent', { method: 'PUT', body: JSON.stringify({ enabled: turningOn }) });
    toast(turningOn ? '助手已开启' : '助手已关闭', 'ok');
    await loadAgent();
  } catch (error) {
    toast(`修改失败：${error.message}`, 'error');
    button.disabled = false;
  }
}

async function agentRun() {
  const button = $('agent-run');
  button.disabled = true;
  setStatus('agent-state', '正在分析…（会调用一次模型，请稍候）');
  try {
    const data = await api('/api/admin/agent/analyze', { method: 'POST', body: JSON.stringify({}) });
    const count = (data.analyses || []).length;
    toast(data.findings ? `已分析 ${count} 条异常` : '现在没有异常，无需分析', 'ok');
  } catch (error) {
    toast(`分析失败：${error.message}`, 'error');
  }
  await loadAgent();
}

wirePanel('panel-signups', () => {
  PANEL_LOADED.signups = true;
  renderAdminSignups(adminData.signups || [], adminData.signup_counts || {});
});
wirePanel('panel-users', () => { PANEL_LOADED.users = true; renderAdminUsers(adminData.users || []); });
wirePanel('panel-admins', () => { PANEL_LOADED.admins = true; renderAdminRoster(adminData.admins || []); });
wirePanel('panel-broadcast', () => { renderAnnouncements(adminData.announcements || []); });
wirePanel('panel-invites', () => {
  PANEL_LOADED.invites = true;
  renderAdminInvites(adminData.invites || []);
});
wirePanel('panel-audit', () => { PANEL_LOADED.audit = true; renderAdminAudit(adminData.audit || []); });
wirePanel('panel-mail', () => { if (!mailBoard.messages.length) loadMailBoard(); });
wirePanel('panel-usage', () => { loadUsage(); });
wirePanel('panel-metrics', () => { startMetrics(); });
wirePanel('panel-agent', () => { loadAgent(); });
wirePanel('panel-alerts', () => { PANEL_LOADED.alerts = true; renderAdminAlerts(adminData.alerts || []); });
$('agent-toggle').addEventListener('click', agentToggle);
$('agent-run').addEventListener('click', agentRun);
$('agent-refresh').addEventListener('click', () => loadAgent({ notify: true }));
$('metrics-refresh').addEventListener('click', () => loadMetrics({ notify: true }));
$('admin-invite').addEventListener('click', async () => {
  const label = $('invite-label').value.trim() || 'pilot';
  const days = Number($('invite-days').value) || 7;
  try {
    const data = await api('/api/admin/invites', { method: 'POST', body: JSON.stringify({ label, days }) });
    const box = $('admin-invite-result');
    clear(box);
    box.appendChild(el('div', 'note', `邀请码（只显示这一次，请立刻复制给对方）：${data.code}`));
    $('invite-label').value = '';
    renderAdminInvites(data.invites);
    setStatus('admin-status', `已生成邀请码（备注 ${data.label}，${data.days} 天有效）。`, 'ok');
  } catch (error) {
    setStatus('admin-status', error.message, 'error');
  }
});

$('install-dismiss').addEventListener('click', () => {
  try { localStorage.setItem(INSTALL_DISMISSED_KEY, '1'); } catch (error) { /* private mode */ }
  const box = $('install-hint');
  if (box) box.classList.add('hidden');
  toast('好的，以后不再提示', 'ok');
});
