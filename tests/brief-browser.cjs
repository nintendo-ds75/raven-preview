/* The task page a Slack DM links to (/brief#rvn_…), every control on it,
 * clicked in Chromium at 1440px and at 390px, with what each changed read
 * back from the store. Each width gets a world of its own (a fresh
 * temporary database seeded by tests/brief_browser_server.py), so a
 * control that changes the record is used once per width on the same
 * starting state. Sign-in is on; Slack is a fake that records what it
 * would post; no model is called. Any page error or console error fails the run, except the HTTP refusals
 * a step expects and names (a link that is not valid, a hand-on to a
 * person Raven does not know).
 *
 *   node tests/brief-browser.cjs
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const python = process.env.BRIDGE_PYTHON || 'python3';
const artifacts = path.join(root, 'test-results');
const WIDTHS = {
  desktop: {viewport: {width: 1440, height: 900}},
  phone: {viewport: {width: 390, height: 844}, isMobile: true, hasTouch: true, deviceScaleFactor: 2},
};
const OPTION = 'Drop excess and count it in a metric';
const OTHER_OPTION = 'Requeue with backoff, capped';
const PASSWORD = 'brief-browser-password-1';

// Errors from every page. A refusal a step expects is named for that step
// (its status and path) and must happen; anything else fails the run.
const errors = [];
let expected = [];
function watch(page, label) {
  page.on('pageerror', e => errors.push(`${label}: pageerror: ${e.message}`));
  page.on('console', m => {
    if (m.type() !== 'error') return;
    const url = m.location()?.url || '';
    let where = '';
    try { where = new URL(url).pathname; } catch {}
    const hit = expected.find(x => m.text().includes(`status of ${x.status}`) && where === x.path);
    if (hit) { hit.seen += 1; return; }
    errors.push(`${label}: console: ${m.text()} ${url}`);
  });
}
async function expecting(status, pathname, fn) {
  const entry = {status, path: pathname, seen: 0};
  expected.push(entry);
  try {
    await fn();
    await new Promise(r => setTimeout(r, 250));
  } finally { expected = expected.filter(x => x !== entry); }
  assert(entry.seen > 0, `expected an HTTP ${status} on ${pathname}`);
}

async function startWorld(temp, name) {
  const db = path.join(temp, `${name}.db`);
  const proc = spawn(python, [path.join(root, 'tests', 'brief_browser_server.py'), db], {cwd: root});
  let stderr = '';
  proc.stderr.on('data', d => { stderr += d; });
  const lines = readline.createInterface({input: proc.stdout});
  const state = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('World startup timeout\n' + stderr)), 30000);
    lines.on('line', line => { try { const s = JSON.parse(line); clearTimeout(timer); resolve(s); } catch {} });
    proc.once('exit', code => { clearTimeout(timer); reject(new Error(`World exited ${code}\n${stderr}`)); });
  });
  state.proc = proc;
  state.ctl = async p => {
    const r = await fetch(state.control + p);
    const value = await r.json();
    if (!r.ok) throw new Error(`control ${p}: ${value.error}`);
    return value;
  };
  // The API as the page calls it, from outside any browser.
  state.api = async (token, p, body) => {
    const r = await fetch(state.base + p, {method: body ? 'POST' : 'GET', headers: {'X-Raven-Link': token,
      ...(body ? {'Content-Type': 'application/json'} : {})}, body: body ? JSON.stringify(body) : undefined});
    return {status: r.status, body: await r.json().catch(() => ({}))};
  };
  return state;
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-brief-browser-'));
  fs.mkdirSync(artifacts, {recursive: true});
  const local = process.env.BRIDGE_BROWSER || '/opt/pw-browsers/chromium';
  const browser = await chromium.launch({headless: true, ...(fs.existsSync(local) ? {executablePath: local} : {})});
  const worlds = [];
  const covered = [];
  try {
    for (const [width, opts] of Object.entries(WIDTHS)) {
      const W = await startWorld(temp, width);
      worlds.push(W);
      const narrow = width === 'phone';
      const contexts = [];
      async function open(label, cookies = []) {
        const context = await browser.newContext(opts);
        contexts.push(context);
        if (cookies.length) await context.addCookies(cookies);
        const page = await context.newPage();
        watch(page, `${width} ${label}`);
        return page;
      }
      async function brief(page, token) {
        await page.goto('about:blank');
        await page.goto(`${W.base}/brief#${token}`);
        await page.waitForSelector('.brief-hero', {timeout: 10000});
      }
      async function step(name, fn, page) {
        try { await fn(); }
        catch (error) {
          if (page) await page.screenshot({path: path.join(artifacts, `brief-${width}-failed.png`), fullPage: true}).catch(() => {});
          error.message = `[${width}] ${name}: ${error.message}`;
          throw error;
        }
        if (page && narrow) {
          const wide = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
          assert(wide <= 0, `[${width}] ${name}: the page scrolls sideways by ${wide}px`);
        }
        covered.push(`${width}: ${name}`);
        console.log(`ok  ${width.padEnd(7)} ${name}`);
      }
      const toast = (page, re) => page.waitForFunction(source => {
        const t = document.querySelector('#toast');
        return t && !t.hidden && new RegExp(source).test(t.textContent);
      }, re.source, {timeout: 8000});
      const kicker = page => page.locator('.brief-kicker').innerText();
      const visibleCount = (page, selector) => page.evaluate(sel =>
        [...document.querySelectorAll(sel)].filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden').length, selector);
      // The page scrolls smoothly: wait until the element's top is below
      // the sticky header and inside the window.
      const scrolledTo = (page, selector) => page.waitForFunction(sel => {
        const box = document.querySelector(sel)?.getBoundingClientRect();
        const header = document.querySelector('.brief-top').getBoundingClientRect().bottom;
        return Boolean(box) && box.top >= header - 1 && box.top < innerHeight / 2;
      }, selector, {timeout: 5000});
      const N = W.nodes, L = W.links;

      // ---------------- a link that does not open ----------------
      const lost = await open('error page');
      await step('error page: a cut-off link offers no resend, and "Sign in" opens the sign-in page', async () => {
        await expecting(401, '/api/brief', async () => {
          await lost.goto(`${W.base}/brief#rvn_cut-off`);
          await lost.waitForSelector('.brief-empty');
        });
        assert.match(await lost.locator('.brief-empty').innerText(), /This link looks incomplete/);
        assert.equal(await lost.locator('[data-action="resend"]').count(), 0);
        await Promise.all([lost.waitForURL(/\/auth\/login/), lost.getByRole('link', {name: 'Sign in to Raven'}).click()]);
        await lost.waitForSelector('form[action="/auth/password"]');
      }, lost);
      await step('error page: a page opened without its link says so', async () => {
        await lost.goto(`${W.base}/brief`);
        await lost.waitForSelector('.brief-empty');
        assert.match(await lost.locator('.brief-empty').innerText(), /the link is missing/);
        assert.equal(await lost.locator('[data-action="resend"]').count(), 0);
      }, lost);
      await step('error page: an expired link asks Slack for a new one, and the new link pasted in opens', async () => {
        const before = (await W.ctl('/messages')).length;
        await expecting(401, '/api/brief', async () => {
          await lost.goto('about:blank');
          await lost.goto(`${W.base}/brief#${L.expired}`);
          await lost.waitForSelector('.brief-empty');
        });
        const resend = lost.locator('[data-action="resend"]');
        assert.equal(await resend.innerText(), 'Send me a new link in Slack');
        await resend.click();
        await lost.waitForSelector('.brief-resent');
        assert.match(await lost.locator('.brief-resent').innerText(), /a new one is on its way/);
        assert.equal(await resend.isDisabled(), true);
        assert.equal(await resend.innerText(), 'Link requested');
        const sent = (await W.ctl('/messages')).slice(before);
        assert.equal(sent.length, 1, JSON.stringify(sent));
        assert.equal(sent[0].channel, 'DUCAL');
        const fresh = sent[0].text.match(/\/brief#(rvn_[A-Za-z0-9_-]+)/)[1];
        assert.notEqual(fresh, L.expired);
        assert.equal((await W.api(fresh, '/api/brief')).body.viewer.name, 'Priya Raman');
        // Pasted into the tab that shows the error: only the fragment moves.
        await lost.evaluate(t => { location.hash = t; }, fresh);
        await lost.waitForSelector('.brief-hero');
        assert.match(await lost.locator('.brief-top').innerText(), /Priya Raman/);
      }, lost);

      // ---------------- Priya ----------------
      const cal = await open('Priya');
      await brief(cal, L.priya);
      await step('the page opens as the person the link names, on the decision it names', async () => {
        assert.equal(await kicker(cal), 'Waiting on you');
        assert.match(await cal.locator('#focus-question').innerText(), /When a tenant exceeds its limit/);
        assert.match(await cal.locator('.brief-top').innerText(), /Priya Raman/);
        assert.equal(await cal.locator('#note-form').count(), 1);
      }, cal);
      await step('a link opens only its own task (API)', async () => {
        const other = await W.api(L.priya, `/api/brief?focus=${W.prior_node}`);
        assert.equal(other.body.focus.node_id, N.remote);
        const answer = await W.api(L.priya, '/api/brief/answer', {decision_id: W.prior_node, expected_updated_at: 'x', answer: 'No'});
        assert.equal(answer.status, 400);
      });
      await step('skip link moves focus without dropping the link from the address', async () => {
        await cal.keyboard.press('Tab');
        await cal.keyboard.press('Enter');
        assert.equal(new URL(cal.url()).hash, `#${L.priya}`);
        assert.equal(await cal.evaluate(() => document.activeElement?.id), 'brief-main');
      }, cal);
      await step('viewer menu: opens, closes on a click elsewhere, on Escape, and on its own summary', async () => {
        const menu = cal.locator('.brief-viewer-menu');
        const pop = cal.locator('.brief-viewer-pop');
        await cal.locator('.brief-viewer-menu > summary').click();
        assert.equal(await pop.isVisible(), true);
        assert.match(await pop.innerText(), /This link is yours\.[\s\S]*Please don’t forward it/);
        // A click elsewhere on the page, below the popover.
        await cal.mouse.click(20, (await cal.viewportSize()).height - 40);
        assert.equal(await menu.evaluate(el => el.open), false);
        await cal.locator('.brief-viewer-menu > summary').click();
        await cal.keyboard.press('Escape');
        assert.equal(await menu.evaluate(el => el.open), false);
        await cal.locator('.brief-viewer-menu > summary').click();
        await cal.locator('.brief-viewer-menu > summary').click();
        assert.equal(await menu.evaluate(el => el.open), false);
      }, cal);
      await step('"Show more" on a long quote shows the rest', async () => {
        const quote = cal.locator('#focus .brief-found blockquote');
        assert.equal(await quote.evaluate(el => el.classList.contains('is-clamped')), true);
        const before = await quote.evaluate(el => el.getBoundingClientRect().height);
        assert(await quote.evaluate(el => el.scrollHeight > el.clientHeight + 1), 'a clamped quote hides nothing');
        await cal.locator('#focus .brief-found [data-action="more"]').click();
        assert.equal(await quote.evaluate(el => el.classList.contains('is-clamped')), false);
        assert(await quote.evaluate(el => el.getBoundingClientRect().height) > before);
        assert.equal(await cal.locator('#focus .brief-found [data-action="more"]').count(), 0);
        // A quote long enough to be clamped at one width and not the other
        // offers "Show more" only where the clamp hides something.
        const offered = await cal.evaluate(() => {
          const saved = data.focus.found[0].quote;
          data.focus.found[0].quote = saved.slice(0, 420);
          render();
          const q = document.querySelector('#focus .brief-found blockquote');
          const out = {more: document.querySelectorAll('#focus .brief-found [data-action="more"]').length,
                       hides: q.scrollHeight > q.clientHeight + 1};
          data.focus.found[0].quote = saved;
          render();
          return out;
        });
        assert.deepEqual(offered, narrow ? {more: 1, hides: true} : {more: 0, hides: false});
      }, cal);
      await step('"Show earlier events" opens the whole history', async () => {
        const more = cal.locator('[data-action="history-all"]');
        const hidden = Number((await more.innerText()).match(/Show (\d+) earlier/)[1]);
        const shown = await cal.locator('.brief-history > li').count();
        await more.click();
        assert.equal(await cal.locator('.brief-history > li').count(), shown + hidden);
        assert.equal(await cal.locator('[data-action="history-all"]').count(), 0);
        assert.match(await cal.locator('.brief-history').innerText(), /Task started/);
      }, cal);
      const NOTE = `Tenant ids arrive as the X-Scope-OrgID header (${width}).`;
      await step('note form adds a note as the person, and the coding agent reads it', async () => {
        await cal.fill('#note-text', NOTE);
        await cal.locator('#note-form button[type=submit]').click();
        await toast(cal, /^Added\./);
        const li = cal.locator('.brief-notes > li').filter({hasText: NOTE});
        await li.waitFor();
        assert.equal(await cal.inputValue('#note-text'), '');
        const notes = await W.ctl('/notes');
        const saved = notes.find(n => n.text === NOTE);
        assert.equal(saved.by, 'Priya Raman');
        assert.equal(saved.source, 'page');
        assert((await W.ctl('/tree-notes')).includes(NOTE));
      }, cal);
      await step('withdraw on a note takes it back, and the agent is told', async () => {
        const li = cal.locator('.brief-notes > li').filter({hasText: NOTE});
        await li.locator('[data-action="withdraw-note"]').click();
        await toast(cal, /^Withdrawn\./);
        await cal.locator('.brief-notes > li.is-withdrawn').filter({hasText: NOTE}).waitFor();
        assert.match(await li.innerText(), /Withdrawn by its author\./);
        assert.equal(await li.locator('[data-action="withdraw-note"]').count(), 0);
        const notes = await W.ctl('/notes');
        const id = notes.find(n => n.text === NOTE).id;
        assert(notes.some(n => n.source === 'withdrawn' && n.withdraws === id));
      }, cal);
      await step('an option fills the answer box, another replaces it, and editing unpicks it', async () => {
        await cal.locator(`.brief-option[data-value="${OPTION}"]`).click();
        assert.equal(await cal.inputValue('#answer-text'), OPTION);
        assert.equal(await cal.locator('.brief-option.is-picked').count(), 1);
        await cal.locator(`.brief-option[data-value="${OTHER_OPTION}"]`).click();
        assert.equal(await cal.inputValue('#answer-text'), OTHER_OPTION);
        assert.equal(await cal.locator('.brief-option.is-picked').getAttribute('data-value'), OTHER_OPTION);
        await cal.locator('#answer-text').press('End');
        await cal.locator('#answer-text').type(', at most 30s');
        assert.equal(await cal.locator('.brief-option.is-picked').count(), 0);
        assert.equal(await cal.locator('input[name="answer-option"]:checked').count(), 0);
        await cal.fill('#answer-text', '');
      }, cal);

      // ---------------- read only ----------------
      const val = await open('Val');
      await brief(val, L.val);
      await step('read-only standing: no answer form and no note box for a viewer', async () => {
        assert.equal(await kicker(val), 'You were asked about');
        assert.match(await val.locator('.brief-cannot').innerText(), /You can read this, but not answer it/);
        assert.equal(await val.locator('#answer-form').count(), 0);
        assert.equal(await val.locator('#handon-form').count(), 0);
        assert.equal(await val.locator('#note-form').count(), 0);
        assert.match(await val.locator('.brief-chat').innerText(), /You have read-only access\./);
      }, val);

      const ada = await open('Ada');
      await brief(ada, L.ada);
      await step('header "Sign in" for a person the link cannot make an account for (an administrator)', async () => {
        assert.equal(await ada.locator('[data-action="account"]').count(), 0);
        assert.equal(await ada.locator('#focus').count(), 0);
        await Promise.all([ada.waitForURL(/\/auth\/login/), ada.locator('.brief-top a.brief-top-link').click()]);
        await ada.waitForSelector('form[action="/auth/password"]');
      }, ada);

      // ---------------- Priya decides ----------------
      await brief(cal, L.priya);
      await step('"Answer this" opens another decision on the task, keeping the address', async () => {
        await cal.locator(`#d-${N.handon} [data-action="focus"]`).click();
        await cal.waitForFunction(() => /token bucket/.test(document.querySelector('#focus-question')?.textContent || ''));
        assert.equal(await kicker(cal), 'Waiting on you');
        assert.equal(new URL(cal.url()).hash, `#${L.priya}`);
        assert.equal(await cal.evaluate(() => document.activeElement?.id), 'focus-question');
        assert.equal(await cal.locator('[data-action="focus-asked"]').count(), 1);
      }, cal);
      await step('hand-on form: picking a person from the list hands the decision to them', async () => {
        await cal.locator('#handon > summary').click();
        await cal.selectOption('#handon-person', W.ids.tomas);
        assert.equal(await cal.locator('#handon-other').isVisible(), false);
        await cal.fill('#handon-note', 'Tomas knows the limiter.');
        await cal.locator('#handon-form button[type=submit]').click();
        await toast(cal, /^Handed to Tomas Novak, for this question only/);
        await cal.waitForFunction(() => document.querySelector('.brief-kicker')?.textContent === 'You handed this on');
        assert.match(await cal.locator('.brief-cannot').innerText(), /You handed this on to Tomas Novak/);
        const d = await W.ctl(`/decision?id=${N.handon}`);
        assert.equal(d.owner_name, 'Tomas Novak');
        assert.equal(d.status, 'pending');
      }, cal);
      await step('"Back to the decision you were asked about" returns to it', async () => {
        await cal.locator('[data-action="focus-asked"]').click();
        await cal.waitForFunction(() => /exceeds its limit/.test(document.querySelector('#focus-question')?.textContent || ''));
        assert.equal(await cal.locator('[data-action="focus-asked"]').count(), 0);
        assert.equal(await cal.locator(`#d-${N.handon} [data-action="focus"]`).count(), 0);
      }, cal);
      const WHY = `A dropped sample is counted per tenant (${width}).`;
      await step('answer form (waiting on you): records the decision as the person', async () => {
        await cal.locator(`.brief-option[data-value="${OPTION}"]`).click();
        await cal.fill('#answer-why', WHY);
        await cal.getByRole('button', {name: 'Record my decision'}).click();
        await toast(cal, /^Recorded as Priya Raman's answer/);
        await cal.waitForFunction(() => document.querySelector('.brief-kicker')?.textContent === 'You decided this');
        assert.match(await cal.locator('#focus .brief-block.is-answer').innerText(), new RegExp(`Your answer[\\s\\S]*${OPTION}`));
        const d = await W.ctl(`/decision?id=${N.remote}`);
        assert.equal(d.answer, OPTION);
        assert.equal(d.rationale, WHY);
        assert.equal(d.answered_by, 'Priya Raman');
        assert.notEqual(d.status, 'pending');
        assert(d.signatures.includes('Priya Raman'));
      }, cal);
      await step('a prediction is labelled a guess; "Answer this" answers a decision without options; a stale answer is refused and the page catches up', async () => {
        await cal.locator(`#d-${N.remote2} [data-action="focus"]`).click();
        await cal.waitForFunction(() => /write handler/.test(document.querySelector('#focus-question')?.textContent || ''));
        assert.match(await cal.locator('#focus .brief-block.is-guess').innerText(), /How you decided before · a guess, not approved[\s\S]*Retry-After/);
        assert.equal(await cal.locator('.brief-options').count(), 0);
        await cal.fill('#answer-text', 'No: drop at the queue instead.');
        // Meanwhile they answer it in another tab.
        const other = await cal.context().newPage();
        watch(other, `${width} Priya, second tab`);
        await brief(other, L.priya);
        await other.locator(`#d-${N.remote2} [data-action="focus"]`).click();
        await other.waitForFunction(() => /write handler/.test(document.querySelector('#focus-question')?.textContent || ''));
        await other.fill('#answer-text', 'Yes, 429 with Retry-After.');
        await other.getByRole('button', {name: 'Record my decision'}).click();
        await toast(other, /^Recorded as Priya Raman's answer/);
        await other.close();
        // The first tab's answer is refused as stale, and the page shows
        // the decision as it is now, keeping what they typed.
        await expecting(400, '/api/brief/answer', async () => {
          await cal.getByRole('button', {name: 'Record my decision'}).click();
          await toast(cal, /^This decision changed while you were reading it/);
        });
        await cal.waitForFunction(() => document.querySelector('.brief-kicker')?.textContent === 'You decided this');
        assert.match(await cal.locator('#focus .brief-block.is-answer').innerText(), /Yes, 429 with Retry-After\./);
        assert.equal(await cal.inputValue('#answer-text'), 'No: drop at the queue instead.');
        const d = await W.ctl(`/decision?id=${N.remote2}`);
        assert.equal(d.answered_by, 'Priya Raman');
        assert.equal(d.answer, 'Yes, 429 with Retry-After.');
        await cal.locator('[data-action="focus-asked"]').click();
        await cal.waitForFunction(() => /exceeds its limit/.test(document.querySelector('#focus-question')?.textContent || ''));
      }, cal);
      await step('"Correct your answer" saves a correction as the person', async () => {
        assert.equal(await cal.locator('#answer-form').isVisible(), false);
        await cal.locator('.brief-correct > summary').click();
        await cal.fill('#answer-text', 'Drop excess, count it per tenant, and alert on it.');
        await cal.getByRole('button', {name: 'Save correction'}).click();
        await toast(cal, /^Corrected and signed by Priya Raman/);
        await cal.waitForFunction(() => /alert on it/.test(document.querySelector('#focus .brief-block.is-answer')?.textContent || ''));
        const d = await W.ctl(`/decision?id=${N.remote}`);
        assert.equal(d.answer, 'Drop excess, count it per tenant, and alert on it.');
        assert.equal(d.answered_by, 'Priya Raman');
      }, cal);
      await step('"The decision above" goes back to the decision card', async () => {
        await cal.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
        await cal.locator('[data-action="to-focus"]').click();
        await scrolledTo(cal, '#focus');
      }, cal);
      await step('"Correct my answer" opens a decision the person answered', async () => {
        const button = cal.locator(`#d-${N.remote2} [data-action="focus"]`);
        assert.equal(await button.innerText(), 'Correct my answer');
        await button.click();
        await cal.waitForFunction(() => /write handler/.test(document.querySelector('#focus-question')?.textContent || ''));
        assert.equal(await kicker(cal), 'You decided this');
        assert.equal(await cal.locator('.brief-correct > summary').innerText(), 'Correct your answer');
      }, cal);
      await step('"Review & sign" opens what the agent settled, and "Sign off" signs it as the person', async () => {
        const button = cal.locator(`#d-${N.settled} [data-action="focus"]`);
        assert.equal(await button.innerText(), 'Review & sign');
        await button.click();
        await cal.waitForFunction(() => /default to unlimited/.test(document.querySelector('#focus-question')?.textContent || ''));
        assert.equal(await kicker(cal), 'Sign-off wanted from you');
        // A reason goes with a correction: a plain sign-off keeps none.
        assert.equal(await cal.locator('#answer-why').isVisible(), false);
        await cal.fill('#answer-text', 'No.');
        assert.equal(await cal.locator('#answer-why').isVisible(), true);
        // The button says what it records.
        assert.equal(await cal.locator('#answer-form button[type=submit]').innerText(), 'Correct and sign');
        await cal.fill('#answer-text', '');
        assert.equal(await cal.locator('#answer-why').isVisible(), false);
        assert.equal(await cal.locator('#answer-form button[type=submit]').innerText(), 'Sign off');
        await cal.getByRole('button', {name: 'Sign off', exact: true}).click();
        await toast(cal, /^Signed off by Priya Raman/);
        await cal.waitForFunction(() => document.querySelector('.brief-kicker')?.textContent === 'You decided this');
        const d = await W.ctl(`/decision?id=${N.settled}`);
        assert(d.signatures.includes('Priya Raman'));
        assert.equal(Boolean(d.authorized), true);
        assert.equal(d.answer, 'Yes: unlimited unless a limit is configured.');
      }, cal);

      // ---------------- Rafael hands on ----------------
      const bry = await open('Rafael');
      await brief(bry, L.rafael);
      await step('hand-on form: a listed person Raven cannot message is explained and cannot be sent', async () => {
        await bry.locator('#handon > summary').click();
        const listed = bry.locator('#handon-person option[value^="unknown:"]');
        assert.match(await listed.innerText(), /@kmarsh · not in Raven yet/);
        await bry.selectOption('#handon-person', await listed.getAttribute('value'));
        assert.match(await bry.locator('#handon-help').innerText(), /@kmarsh is in CODEOWNERS for \/tsdb\/ but has no Raven person yet/);
        assert.equal(await bry.locator('#handon-form button[type=submit]').isDisabled(), true);
      }, bry);
      await step('hand-on form: "Someone else…" with a name Raven does not know is refused, and nothing changes', async () => {
        await bry.selectOption('#handon-person', 'other');
        assert.equal(await bry.evaluate(() => document.activeElement?.id), 'handon-other');
        assert.equal(await bry.locator('#handon-help').isVisible(), false);
        assert.equal(await bry.locator('#handon-form button[type=submit]').isDisabled(), false);
        await bry.fill('#handon-other', 'kmarsh');
        await expecting(400, '/api/brief/refer', async () => {
          await bry.locator('#handon-form button[type=submit]').click();
          await bry.waitForSelector('#handon-error:not([hidden])');
        });
        assert.match(await bry.locator('#handon-error').innerText(), /kmarsh is in CODEOWNERS for \/tsdb\/ but has no Raven person yet/);
        assert.equal((await W.ctl(`/decision?id=${N.tsdb}`)).owner_name, 'Rafael Ortega');
      }, bry);
      await step('hand-on form: "Someone else…" with a GitHub login hands it to that person', async () => {
        await bry.fill('#handon-other', 'sokafor');
        await bry.fill('#handon-note', 'Sam wrote the watcher cache.');
        await bry.locator('#handon-form button[type=submit]').click();
        await toast(bry, /^Handed to Sam Okafor/);
        await bry.waitForFunction(() => document.querySelector('.brief-kicker')?.textContent === 'You handed this on');
        assert.match(await bry.locator('.brief-cannot').innerText(), /You handed this on to Sam Okafor\. It now waits on them\./);
        assert.equal((await W.ctl(`/decision?id=${N.tsdb}`)).owner_name, 'Sam Okafor');
      }, bry);

      // ---------------- Sam makes an account (Raven has no email for them) ----------------
      const samToken = (await W.ctl(`/dm-link?person=sam&node=tsdb`)).token;
      assert(samToken, 'the hand-on sent Sam no link');
      assert((await W.ctl('/messages')).some(m => m.channel === 'DUJES' && m.text.includes('Sam wrote the watcher cache')),
        'the hand-on note did not reach Sam');
      const jes = await open('Sam');
      await brief(jes, samToken);
      await step('account: one invitation per width (header link on a phone, side note on a desktop)', async () => {
        assert.equal(await visibleCount(jes, '[data-action="account"]'), 1);
        assert.equal(await jes.locator(narrow ? '.brief-top [data-action="account"]' : '.brief-join [data-action="account"]').isVisible(), true);
      }, jes);
      await step('account dialog: "Not now" closes it', async () => {
        await jes.locator('[data-action="account"]:visible').click();
        await jes.waitForSelector('#brief-modal[open]');
        assert.equal(await jes.locator('#account-email').isVisible(), true);
        await jes.getByRole('button', {name: 'Not now'}).click();
        assert.equal(await jes.locator('#brief-modal').evaluate(el => el.open), false);
      }, jes);
      await step('account form: asks for an email when Raven has none, and makes the login', async () => {
        await jes.locator('[data-action="account"]:visible').click();
        await jes.waitForSelector('#brief-modal[open]');
        // An address another person has is refused, in the dialog.
        await jes.fill('#account-email', W.emails.priya);
        await jes.fill('#account-password', PASSWORD);
        await expecting(400, '/api/brief/account', async () => {
          await jes.locator('#account-form button[type=submit]').click();
          await jes.waitForSelector('#account-error:not([hidden])');
        });
        assert.match(await jes.locator('#account-error').innerText(), /belongs to another person/);
        assert.equal((await W.ctl('/account?person=sam')).claimed, false);
        await jes.fill('#account-email', 'sam@example.org');
        await Promise.all([jes.waitForURL(new RegExp(`/#runs/${W.task}$`)), jes.locator('#account-form button[type=submit]').click()]);
        await jes.waitForSelector('.task-heading h1');
        assert.deepEqual(await W.ctl('/account?person=sam'), {claimed: true, email: 'sam@example.org'});
      }, jes);

      // ---------------- Zoe adds their signature ----------------
      const zoe = await open('Zoe');
      await brief(zoe, L.zoe);
      await step('answer form (signed by someone else): "Add my signature" signs as the person', async () => {
        assert.equal(await kicker(zoe), 'You were asked about');
        await zoe.getByRole('button', {name: 'Add my signature'}).click();
        await toast(zoe, /^Signed off by Zoe Reviewer/);
        await zoe.waitForFunction(() => document.querySelector('.brief-kicker')?.textContent === 'You decided this');
        const d = await W.ctl(`/decision?id=${N.ui}`);
        assert.deepEqual(d.signatures, ['Mei Lin', 'Zoe Reviewer']);
        assert.equal(d.answered_by, 'Mei Lin');
      }, zoe);

      // ---------------- Priya makes an account and uses the app ----------------
      await brief(cal, L.priya);
      await step('account form: makes the login with the address Raven has, and lands on the task in the app', async () => {
        assert.equal(await visibleCount(cal, '[data-action="account"]'), 1);
        await cal.locator('[data-action="account"]:visible').click();
        await cal.waitForSelector('#brief-modal[open]');
        assert.equal(await cal.locator('#account-email').count(), 0);
        // The other way in: GitHub sign-in, which goes to GitHub (followed
        // here only as far as its redirect; nothing reaches GitHub).
        const github = cal.locator('#account-form a[href="/auth/github"]');
        assert.equal(await github.innerText(), 'use GitHub instead');
        const away = await fetch(`${W.base}/auth/github`, {redirect: 'manual'});
        assert.equal(away.status, 302);
        assert.match(away.headers.get('location'), /^https:\/\/github\.com\/login\/oauth\/authorize\?.*client_id=brief-browser-client/);
        assert.match(await cal.locator('#brief-modal').innerText(), /pr…@example\.org/);
        await cal.fill('#account-password', PASSWORD);
        await Promise.all([cal.waitForURL(new RegExp(`/#runs/${W.task}$`)), cal.locator('#account-form button[type=submit]').click()]);
        await cal.waitForSelector('.task-heading h1');
        assert.equal((await W.ctl('/account?person=priya')).claimed, true);
      }, cal);
      await step('app: "Open task page" opens the task page as the signed-in person', async () => {
        const ask = cal.locator('[data-action="task-brief"]');
        assert.equal(await ask.innerText(), 'Open task page');
        const before = (await W.ctl('/links?person=priya')).length;
        await Promise.all([cal.waitForURL(/\/brief#rvn_/), ask.click()]);
        await cal.waitForSelector('.brief-hero');
        assert.match(await cal.locator('.brief-top').innerText(), /Priya Raman/);
        assert.equal(await cal.locator('#note-form').count(), 1);
        // On the decision the app picked for them: the first they answered.
        assert.match(await cal.locator('#focus-question').innerText(), /exceeds its limit/);
        const links = await W.ctl('/links?person=priya');
        assert.equal(links.length, before + 1);
        assert.equal(links[links.length - 1].notification_id, '');
      }, cal);
      await step('"Open in Raven" (the person has an account) opens the task in the app', async () => {
        assert.equal(await cal.locator('[data-action="account"]').count(), 0);
        const openApp = cal.getByRole('link', {name: 'Open in Raven'});
        assert.equal(await openApp.isVisible(), true);
        await Promise.all([cal.waitForURL(new RegExp(`/#runs/${W.task}$`)), openApp.click()]);
        await cal.waitForSelector('.task-heading h1');
        // In a browser not signed in, the same link says "Sign in to Raven":
        // sign-in lands on the inbox, not on this task.
        const out = await open('Priya signed out');
        await brief(out, L.priya);
        assert.equal(await out.getByRole('link', {name: 'Open in Raven'}).count(), 0);
        assert.equal(await out.getByRole('link', {name: 'Sign in to Raven'}).getAttribute('href'), '/auth/login');
      }, cal);
      await step('account login works: signed out, the person signs in at /auth/login', async () => {
        const fresh = await open('Priya signing in');
        await fresh.goto(`${W.base}/auth/login`);
        await fresh.fill('form[action="/auth/password"] input[name=email]', W.emails.priya);
        await fresh.fill('form[action="/auth/password"] input[name=password]', PASSWORD);
        await Promise.all([fresh.waitForURL(u => !u.pathname.startsWith('/auth/')), fresh.locator('form[action="/auth/password"] button[type=submit]').click()]);
        const me = await fresh.evaluate(async () => (await (await fetch('/api/me')).json()).me);
        assert.equal(me.name, 'Priya Raman');
      });

      // ---------------- the admin turns the link off, then back to static ----------------
      const admin = await open('admin', [{name: W.cookie_name, value: W.admin_cookie, domain: '127.0.0.1', path: '/'}]);
      await step('app: "Task page in messages" off hides the app\'s button; static brings back "Open task page"', async () => {
        await admin.goto(`${W.base}/#owners`);
        await admin.waitForSelector('#brief-mode');
        assert.equal(await admin.inputValue('#brief-mode'), 'static');
        assert.deepEqual(await admin.locator('#brief-mode option').evaluateAll(options => options.map(o => o.value)), ['off', 'static']);
        await admin.selectOption('#brief-mode', 'off');
        await admin.waitForFunction(() => /Task links are off/.test(document.querySelector('#toast')?.textContent || ''));
        assert.equal((await W.ctl('/setting?key=brief_mode')).mode, 'off');
        await cal.reload();
        await cal.waitForSelector('.task-heading h1');
        assert.equal(await cal.locator('[data-action="task-brief"]').count(), 0);
        await admin.waitForSelector('#brief-mode');
        await admin.selectOption('#brief-mode', 'static');
        await admin.waitForFunction(() => /static task page/.test(document.querySelector('#toast')?.textContent || ''));
        assert.equal((await W.ctl('/setting?key=brief_mode')).mode, 'static');
        await cal.reload();
        await cal.waitForSelector('.task-heading h1');
        const ask = cal.locator('[data-action="task-brief"]');
        assert.equal(await ask.innerText(), 'Open task page');
        await Promise.all([cal.waitForURL(/\/brief#rvn_/), ask.click()]);
        await cal.waitForSelector('#note-form');
      }, cal);

      for (const context of contexts) await context.close();
    }
    assert.deepEqual(errors, []);
    console.log(`Task page checks passed: ${covered.length} steps across 1440px and 390px.`);
  } finally {
    await browser.close();
    for (const W of worlds) {
      if (W.proc.exitCode === null) { const exited = once(W.proc, 'exit'); W.proc.kill('SIGTERM'); await exited; }
    }
    fs.rmSync(temp, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); if (errors.length) console.error(errors.join('\n')); process.exitCode = 1; });
