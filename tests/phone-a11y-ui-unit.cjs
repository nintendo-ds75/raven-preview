/* What axe-core flagged as serious at 390px, and a cut-off sentence on the
 * sign-in page, pinned at the source so they do not come back. Measured
 * in Chromium with axe-core 4.10 on every page of the app and on /brief. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const read = file => fs.readFileSync(path.join(__dirname, '..', 'web', file), 'utf8');
const css = read('style.css'), app = read('app.js');

// 1. link-name: the brand link keeps its name when the narrow layout hides its words.
const narrowStart = css.indexOf('@media(max-width:760px)');
assert(narrowStart > 0, 'the narrow layout rule exists');
const narrow = css.slice(narrowStart, css.indexOf('@media(max-width:600px)', narrowStart));
for (const match of narrow.matchAll(/^\s*([^{}\n]+)\{display:none\}/gm)) {
  const selectors = match[1].split(',').map(s => s.trim());
  assert(!selectors.includes('.brand span'), 'display:none removes the brand link’s only text from its accessible name');
}
assert.match(narrow, /\.brand span\{[^}]*clip:rect\(0,0,0,0\)/, 'the brand’s words are hidden visually, not from assistive technology');
for (const file of ['index.html', 'brief.html']) {
  assert.match(read(file), /<a class="brand" href="[^"]*"><img [^>]*alt=""[^>]*><span>Raven<\/span>/, `${file}: the brand link’s name is its text`);
}

// 2. The sign-in page’s help text is not clamped to two lines.
assert.match(css, /\.context\{[^}]*-webkit-line-clamp:2/, 'the app clamps .context (the premise of this check)');
assert.match(css, /\.login \.context\{[^}]*-webkit-line-clamp:unset[^}]*\}/, 'the sign-in page shows its help text whole');

// 3. scrollable-region-focusable: a table wider than the screen can be scrolled from the keyboard.
const wraps = [...app.matchAll(/<div class="table-wrap"[^>]*>/g)].map(m => m[0]);
assert(wraps.length >= 4, `expected the tasks, ownership, authority and deliveries tables; found ${wraps.length}`);
for (const wrap of wraps) {
  assert.match(wrap, /tabindex="0"/, `keyboard-reachable: ${wrap}`);
  assert.match(wrap, /role="region"/, `a named region, so tabindex on a div is not a bare stop: ${wrap}`);
  assert.match(wrap, /aria-label="[^"]+"/, `named: ${wrap}`);
}
assert.match(css, /\.table-wrap:focus-visible/, 'focus on a scrolling table is visible');

console.log('Phone accessibility: brand link name, sign-in help text, keyboard-scrollable tables pass.');
