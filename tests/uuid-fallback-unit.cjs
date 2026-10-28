// ravenUuid must produce a v4-shaped id with and without crypto.randomUUID,
// because a self-hosted Raven reached over plain http on a LAN address has
// no secure context and therefore no crypto.randomUUID.
const assert = require('node:assert');
const fs = require('node:fs');
const vm = require('node:vm');
const src = fs.readFileSync(require('node:path').join(__dirname, '..', 'web', 'presentation.js'), 'utf8');
const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
function load(crypto) {
  const ctx = {globalThis: null, console, document: {}, window: {}};
  ctx.globalThis = ctx; ctx.crypto = crypto;
  vm.createContext(ctx);
  vm.runInContext(src + '\nthis.__uuid = ravenUuid;', ctx);
  return ctx.__uuid;
}
const withNative = load({randomUUID: () => '11111111-2222-4333-8444-555555555555'});
assert.equal(withNative(), '11111111-2222-4333-8444-555555555555');
const withValues = load({getRandomValues: a => { for (let i = 0; i < a.length; i++) a[i] = (i * 37) & 255; return a; }});
assert.match(withValues(), V4);
const none = load(undefined);
const a = none(), b = none();
assert.match(a, V4); assert.match(b, V4); assert.notEqual(a, b);
console.log('uuid fallback: native, getRandomValues and Math.random paths all produce v4 ids');
