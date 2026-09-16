// Where is Playwright?
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
const path = require('path');

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
  process.exit(2);
}

module.exports = playwright;
