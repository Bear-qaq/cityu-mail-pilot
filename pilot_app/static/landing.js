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
 *  2. Submit the pilot application without a page reload, falling back to a
 *     normal form POST when scripting is unavailable so the form still works.
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

  // -- 2. the application form -------------------------------------------
  var form = document.getElementById('signup-form');
  if (!form) return;

  var status = document.getElementById('signup-status');
  var button = document.getElementById('signup-submit');

  function say(message, kind) {
    status.textContent = message;
    status.className = 'on ' + kind;
  }

  form.addEventListener('submit', function (event) {
    var email = (document.getElementById('signup-email').value || '').trim();
    var note = (document.getElementById('signup-note').value || '').trim();
    if (!email) return; // let the browser's own validation speak

    event.preventDefault();
    button.disabled = true;
    button.textContent = '提交中…';

    fetch('/api/signup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email, note: note }),
    }).then(function (response) {
      return response.json().then(function (body) { return { ok: response.ok, body: body }; });
    }).then(function (result) {
      if (!result.ok) {
        say(result.body && result.body.detail ? result.body.detail : '提交失败，请稍后再试。', 'bad');
      } else if (result.body.already) {
        say('我们已经收到过这个邮箱的申请，正在处理中。', 'ok');
        form.reset();
      } else {
        say('申请已收到。我们审核后会发邀请码到这个邮箱，请留意收件箱。', 'ok');
        form.reset();
      }
    }).catch(function () {
      say('网络不通，提交失败。请稍后再试。', 'bad');
    }).then(function () {
      button.disabled = false;
      button.textContent = '提交申请';
    });
  });
})();
