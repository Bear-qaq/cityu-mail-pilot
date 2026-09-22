// Where is Playwright, and which engine do the suites drive?
//
// Every browser suite used to open with
// `require('./pw')` -- the scratch directory this
// project happened to install it into once, on one laptop. That is a fact about
// a machine, not about the software: it is the single reason the checks could
// not run on a CI runner or in a contributor's checkout, and the failure it
// produced ("Cannot find module '/tmp/pw/...'") told the reader nothing they
// could act on.
//
// Now the suites ask this file, and it looks in the two places that are real:
//
//   * a normal `node_modules` -- what `npm install` produces at the repo root,
//     which is also what every CI runner does;
//   * the `/tmp/pw` scratch install that AGENTS.md documented first, kept
//     working so the change does not break the machine it was written on.
//
// If neither is there it prints how to fix it. An unhelpful failure is worse
// than a helpful one -- this project has spent enough rounds on tools whose
// error message pointed at the product instead of at themselves.
//
// The second question is the engine. `PILOT_BROWSER=webkit` makes every suite
// drive WebKit; the default stays chromium, because CI and the machines that
// were green on it must not change behaviour because a switch was added. Two
// bugs in this project's history were Safari-only -- a canvas that carried
// metadata the guard refused, and a toast that never appeared -- and a chromium
// run is structurally unable to see either, which is the whole reason the
// switch exists. A misspelt value is an *error*, never a fallback: a silent
// fallback would print twenty green lines that claim to be Safari's.
const fs = require('fs');
const path = require('path');

// One canonical name per engine; `chrome`/`safari` are what people actually
// type, and refusing them would only teach the wrong spelling.
const ENGINES = {
  chromium: 'chromium',
  chrome: 'chromium',
  webkit: 'webkit',
  safari: 'webkit',
  firefox: 'firefox',
};
const requested = (process.env.PILOT_BROWSER || 'chromium').trim().toLowerCase();
const browserName = ENGINES[requested];
if (!browserName) {
  console.error(`不认识的 PILOT_BROWSER=${requested}（可用：chromium / webkit / firefox，默认 chromium）。`);
  console.error('拼错不回落：否则一次号称「Safari 全绿」的运行其实跑的是 chromium。');
  process.exit(2);
}

// A hermetic install (`PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install webkit`)
// puts the browser under node_modules, and Playwright only looks there when the
// variable is set again at launch time. Repeat it -- but only for the engine
// that actually lives there: a checkout with a hermetic WebKit and a normally
// cached Chromium must not send the Chromium runs to the wrong directory.
const hermetic = path.join(__dirname, '..', 'node_modules', 'playwright-core', '.local-browsers');
if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync(hermetic)) {
  if (fs.readdirSync(hermetic).some((name) => name.startsWith(`${browserName}-`))) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = '0';
  }
}

const CANDIDATES = [
  'playwright',
  path.join('/tmp', 'pw', 'node_modules', 'playwright'),
];

let playwright = null;
const missing = [];
for (const candidate of CANDIDATES) {
  try {
    playwright = require(candidate);
    break;
  } catch (err) {
    // Only "this candidate is not installed" is ignorable. A module that is
    // present but broken (a missing transitive dependency, a syntax error)
    // must surface as itself -- sorting that out is a different job.
    if (err && err.code === 'MODULE_NOT_FOUND' && String(err.message).includes(candidate)) {
      missing.push(candidate);
      continue;
    }
    throw err;
  }
}

if (!playwright) {
  console.error(`找不到 Playwright（找过：${missing.join('、')}）。`);
  console.error('装法：npm install --no-save playwright@1.63.0 && npx playwright install chromium');
  console.error('      WebKit 还要：npx playwright install webkit（PILOT_BROWSER=webkit 时才需要）');
  process.exit(2);
}

// `browserType` is the selected engine; `browserName` is what to print in a
// verdict so a log cannot be mistaken for the other engine's. Everything else
// (`webkit`, `devices`, ...) is still here, because a suite may legitimately
// name one engine on purpose -- background_photo_check does, for the Safari
// leg -- and it should not have to re-require the module to do that.
module.exports = Object.assign({}, playwright, { browserType: playwright[browserName], browserName });
