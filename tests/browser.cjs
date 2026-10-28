/* Runs against an isolated temporary database; never changes the user's workspace. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const {once} = require('node:events');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-browser-'));
  const db = path.join(temp, 'test.db');
  const root = path.resolve(__dirname, '..');
  const python = process.env.BRIDGE_PYTHON || 'python3';
  // These checks exercise an existing workspace, not the first-run account flow.
  const initialized = spawnSync(python, ['-c',
    'import sys; from bridge.store import Store; s = Store(sys.argv[1]); s.graph.set_setting("workspace_name", "Browser test workspace"); s.add_record({"repo":"test/platform","provider":"generic","namespace":"browser-fixture","kind":"doc","external_id":"scope","ref":"BROWSER-SCOPE","title":"Synthetic repository scope","body":"An invented repository registered for the browser regression."})', db],
    {cwd:root, encoding:'utf8'});
  assert.equal(initialized.status, 0, initialized.stderr);
  // The UI checks are deterministic: the model rungs stay off whatever
  // backend the machine has (a key or a logged-in claude CLI).
  const server = spawn(python, ['-m', 'bridge', '--port', '0', '--demo', '--db', db],
                       {cwd:root, env:{...process.env, BRIDGE_SEMANTIC:'0', BRIDGE_LIVE:'0'}});
  let browser;
  const artifacts = path.join(root, 'test-results');
  fs.mkdirSync(artifacts, {recursive:true});
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Server startup timeout')), 10000);
      let output = '';
      server.stdout.on('data', data => {
        output += data;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
      server.once('error', reject);
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`Server exited: ${code}`)); });
      server.stderr.on('data', () => {});
    });
    browser = await chromium.launch({headless:true, ...(process.env.BRIDGE_BROWSER ? {executablePath:process.env.BRIDGE_BROWSER} : {})});
    const context = await browser.newContext({viewport:{width:1440,height:1050}, permissions:['clipboard-read','clipboard-write']});
    const page = await context.newPage();
    const errors = [];
    // Asking Raven a question by hand lives in the Tasks page's one "More" menu.
    const askAQuestion = async () => {
      await page.goto(`${url}/#runs`);
      await page.locator('#page-actions details.more-menu > summary').click();
      await page.getByRole('button', {name:'Ask a question',exact:true}).click();
    };
    // The Settings link says "Settings, setup incomplete" while setup has gaps.
    const nav = name => page.getByRole('link', {name:new RegExp(`^${name}(,|$)`)});
    const tab = name => page.getByRole('button', {name:new RegExp(`^${name}`)});
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    await page.goto(url);
    await page.waitForSelector('.decision-card');
    // The app opens on the inbox: three open questions and one node the demo agent settled itself, waiting for sign-off.
    assert.equal(await page.title(), 'Raven · Inbox');
    assert.deepEqual(await page.locator('nav[aria-label="Workspace navigation"] .nav-text').allInnerTexts(), ['Inbox', 'Tasks', 'People', 'Settings']);
    assert.equal(await page.locator('#manual-actions, .breadcrumb, #eyebrow, #page-description, #model-strip').count(), 0);
    assert.equal(await page.locator('.decision-card').count(), 4);
    assert.equal(await page.locator('#inbox-count').textContent(), '4');
    // Every card carries one status sentence that names a person or a state, and one button.
    for (const card of await page.locator('.decision-card').all()) {
      assert.equal(await card.locator('.pill').count(), 1);
      assert.match(await card.locator('.pill').innerText(), /^(Waiting on .+|Sign-off wanted from .+|No owner yet|Needs a second look)$/);
      assert.equal(await card.getByRole('button', {name:/^(Answer|Sign|View)\b/}).count(), 1);
    }
    assert.doesNotMatch(await page.locator('#app').textContent(), /Waiting on undefined|New judgment|Across your workspace|Sample workspace/);
    await page.screenshot({path:path.join(artifacts,'desktop.png'),fullPage:true});

    // The seeded canvas task shows its kickoff verdict and its tree; the whole row is the link.
    await nav('Tasks').click();
    assert.equal(await page.locator('#app table').count(), 0);
    await page.locator('a.task-row', {hasText:'Move the nightly backup job'}).click();
    await page.waitForSelector('.task-summary');
    assert.match(await page.locator('#app').textContent(), /Requested by Priya Natarajan/);
    assert.equal(await page.locator('#task-waiting h2').innerText(), 'Waiting');
    assert.equal(await page.locator('#task-decided h2').innerText(), 'Decided');
    assert.doesNotMatch(await page.locator('#app').textContent(), /Not authorized|Workflow details|Recorded finding|Local workspace/);
    const taskUrl = page.url();
    // A deep link may return the tree before the account. Never interpret
    // that missing auth state as permission to show editing controls.
    const delayed = await browser.newPage();
    let releaseAccount;
    const accountReady = new Promise(resolve => { releaseAccount = resolve; });
    await delayed.route('**/api/state', async route => { await accountReady; await route.continue(); });
    await delayed.goto(taskUrl);
    await delayed.locator('#app').getByText(/Loading workspace/).waitFor();
    assert.equal(await delayed.locator('#note-form').count(), 0);
    releaseAccount();
    await delayed.waitForSelector('.task-summary');
    assert.equal(await delayed.locator('#note-form').count(), 1);
    await delayed.close();
    await page.getByRole('button', {name:'Decisions (2)',exact:true}).click();
    assert.equal(await page.locator('#app .tree-node').count(), 2);
    assert.match(await page.locator('#app .tree-node').nth(1).textContent(), /Level 1 · added by a person/);
    // The note field for the agent is always open under Notes.
    assert.equal(await page.locator('#note-form label[for="note-text"]').innerText(), 'Note for the agent');
    await page.fill('#note-text', 'The backup window moves to 02:00 UTC next week.');
    // Polling must not erase a draft, including after focus leaves the field.
    await page.locator('#app h1').click();
    await page.waitForTimeout(5500);
    assert.equal(await page.inputValue('#note-text'), 'The backup window moves to 02:00 UTC next week.');
    await page.getByRole('button', {name:'Add note',exact:true}).click();
    await page.locator('.task-note').filter({hasText:'The backup window moves to 02:00 UTC next week.'}).waitFor();
    await page.reload();
    await page.waitForSelector('.task-summary');
    assert.equal(page.url(), taskUrl);
    assert.match(await page.locator('#app').textContent(), /The backup window moves to 02:00 UTC next week/);
    await page.screenshot({path:path.join(artifacts,'task-overview.png'),fullPage:true});
    await page.getByRole('button', {name:'History',exact:true}).click();
    assert.match(await page.locator('.task-timeline').textContent(), /The backup window moves to 02:00 UTC next week/);
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path:path.join(artifacts,'task-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:1000});
    await nav('Inbox').click();

    // A node the agent settled sits under All open until a person signs it.
    const settled = page.locator('#app .decision-card').filter({hasText:'Should the nightly backup keep its 30-day retention'});
    await settled.waitFor();
    assert.match(await settled.textContent(), /The agent decided/);
    assert.match(await settled.textContent(), /Sign-off wanted from Alex Morgan/);
    await settled.getByRole('button', {name:/^Sign\b/}).click();
    await page.waitForSelector('#modal[open] #correct-form');
    assert.equal(await page.locator('#modal #answer-form').count(), 0);
    // The retrieval string is kept one level down, behind Details.
    assert.equal(await page.locator('#decision-details').evaluate(d => d.open), false);
    await page.locator('#decision-details > summary').click();
    assert.match(await page.locator('#decision-details').innerText(), /Evidence: settled by the agent/);
    await page.getByRole('button', {name:/^Sign as Alex Morgan$/}).click();
    await page.waitForFunction(() => document.querySelector('#modal').textContent.includes('Signed by Alex Morgan'));
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => document.querySelectorAll('#app .decision-card').length === 3);
    assert.equal(await settled.count(), 0);
    assert.equal(await page.locator('#inbox-count').textContent(), '3');

    // Suggestions can be inspected, but remain unapproved until a human records an answer.
    const credits = page.locator('#app .decision-card').filter({hasText:'Should unused prepaid credits expire at the end of the month?'});
    assert.equal((await credits.locator('.context-box .label').textContent()).trim(), 'Suggested answer · from an earlier decision');
    await credits.getByRole('button', {name:/^Answer\b/}).click();
    await page.waitForFunction(() => document.querySelector('#modal-title').textContent === 'Should unused prepaid credits expire at the end of the month?');
    // The suggestion sits beside the answer box, never inside it: a person
    // must choose to use it.
    assert.equal(await page.locator('#answer').inputValue(), '');
    assert.match(await page.locator('#modal .context-box').first().textContent(), /Roll unused credits/);
    await page.getByRole('button', {name:'Use this answer',exact:true}).click();
    assert.match(await page.locator('#answer').inputValue(), /Roll unused credits/);
    await page.getByRole('button', {name:'Open the earlier decision',exact:true}).first().click();
    await page.waitForFunction(() => document.querySelector('#modal-title').textContent === 'Should unused prepaid credits expire?');
    await page.keyboard.press('Escape');

    // Handing a question on teaches Raven nothing unless the person says they decide it from now on.
    // Measured live: a docs hand-on taught security authority nobody chose.
    await page.evaluate(async () => {
      const s = await (await fetch('/api/state')).json();
      await fetch('/api/people', {method:'POST', headers:{'Content-Type':'application/json','X-Bridge-CSRF':s.csrf_token},
        body:JSON.stringify({name:'Riley Handon', email:'riley@example.test'})});
    });
    await page.reload();
    await page.waitForSelector('.decision-card');
    await tab('All open').click();
    const openQuestion = page.locator('#app .decision-card').filter({hasNotText:'prepaid credits'}).first();
    const handedQuestion = (await openQuestion.locator('h2').textContent()).trim();
    await openQuestion.getByRole('button', {name:/^(Answer|Sign)\b/}).click();
    await page.waitForSelector('#modal[open] #refer-learn');
    assert.equal(await page.locator('#hand-on').getAttribute('data-action'), 'assign');
    assert.equal(await page.locator('#refer-scope').isVisible(), false);
    await page.check('#refer-learn');
    assert.equal(await page.locator('#hand-on').getAttribute('data-action'), 'refer');
    await page.waitForSelector('#modal[open] #refer-scope');
    const teach = await page.locator('#refer-scope option').allTextContents();
    assert(teach.includes('this question only'), teach.join(' | '));
    await page.selectOption('#refer-scope', {label:'this question only'});
    const riley = await page.locator('#assign-owner option', {hasText:'Riley Handon'}).getAttribute('value');
    await page.selectOption('#assign-owner', riley);
    await page.getByRole('button', {name:'Hand on',exact:true}).click();
    await page.waitForFunction(() => /for this question only/.test(document.querySelector('#toast')?.textContent || ''));
    const handed = await (await fetch(`${url}/api/state`)).json();
    assert.equal(handed.decisions.find(d => d.question === handedQuestion).owner_name, 'Riley Handon');
    const learned = await (await fetch(`${url}/api/people`)).json();
    assert.equal(learned.authority.filter(a => a.source === 'referral').length, 0);
    await page.keyboard.press('Escape');

    // Add a person, say what they decide, and route a request to them using the web UI.
    await nav('People').click();
    await page.getByRole('button', {name:'Add person',exact:true}).click();
    await page.getByLabel('Name', {exact:true}).fill('Jamie Test');
    await page.getByLabel('Team', {exact:true}).fill('Payments');
    await page.locator('#person-form').getByRole('button', {name:'Add person',exact:true}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    const jamie = page.locator('.person-card').filter({hasText:'Jamie Test'});
    await jamie.waitFor();
    assert.match(await jamie.textContent(), /Nothing recorded yet/);
    await jamie.getByRole('button', {name:/^Say what they decide/}).click();
    assert.match(await page.locator('#authority-who option:checked').textContent(), /Jamie Test/);
    await page.selectOption('#authority-kind', 'path');
    await page.fill('#authority-scope', 'payments/*');
    await page.locator('#authority-form').getByRole('button', {name:'Save',exact:true}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    await page.waitForFunction(() => /decides payments\/\* · every repository · from config/.test(document.querySelector('#app')?.textContent || ''));
    assert.equal(await page.locator('#app table').count(), 0);
    await askAQuestion();
    await page.getByLabel('Question or prompt', {exact:true}).fill('Should payment retries stop after three attempts?');
    await page.getByLabel('Repository scope', {exact:true}).selectOption('test/platform');
    await page.locator('#request-context > summary').click();
    await page.getByLabel('Context & evidence').fill('The retry worker can cap attempts. Confirm the customer-facing policy.');
    await page.getByLabel('Relevant file path').fill('payments/retries.py');
    await page.locator('#request-form').getByRole('button', {name:'Ask Raven',exact:true}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    await page.waitForSelector('.task-summary');
    await nav('Inbox').click();
    await tab('All open').click();
    const card = page.locator('.decision-card').filter({hasText:'Should payment retries stop after three attempts?'});
    await card.waitFor();
    assert.match(await card.locator('.pill').innerText(), /^Waiting on Jamie Test$/);
    // The form started a task on the canvas and wrote the question as its first node.
    const started = await (await fetch(`${url}/api/state`)).json();
    const task = started.runs.find(r => r.title === 'Should payment retries stop after three attempts?');
    assert.equal(task.agent, 'Raven UI');
    assert.equal(task.paths, 'payments/retries.py');
    assert.match(task.verdict, /^(engage|pass)$/);
    const tree = await (await fetch(`${url}/api/tasks/${task.id}/tree`)).json();
    assert.equal(tree.nodes[0].question, 'Should payment retries stop after three attempts?');
    assert.equal(tree.nodes[0].owner, 'Jamie Test');
    await card.getByRole('button', {name:/^Answer\b/}).click();
    await page.getByLabel('Your answer').fill('Stop after three attempts and notify the customer.');
    await page.locator('#answer-form').getByLabel('Why', {exact:true}).fill('Avoid duplicate charges and make the failure visible.');
    assert.match(await page.locator('#answer-form').innerText(), /Recorded by the local operator on behalf of Jamie Test\./);
    await page.getByRole('button', {name:'Record my answer',exact:true}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});

    // An MCP client can retrieve the exact browser-approved answer over HTTP.
    const state = await (await fetch(`${url}/api/state`)).json();
    const decision = state.decisions.find(d => d.question === 'Should payment retries stop after three attempts?');
    assert.equal(decision.answered_by, 'Jamie Test');
    const message = {jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'bridge_get_decision',arguments:{decision_id:decision.id}}};
    const command = state.mcp_config.mcpServers.bridge;
    const mcp = await (await fetch(command.url, {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(message)})).json();
    const answer = JSON.parse(mcp.result.content[0].text);
    assert.equal(answer.answer, 'Stop after three attempts and notify the customer.');
    assert.equal(answer.approval_pending, false);

    // Corrections are persistent, searchable on the Decided tab, and exported (from Settings > Advanced)
    // with the prior answer retained.
    await tab('Decided').click();
    await page.locator('#inbox-search').fill('payment retries');
    assert.equal(await page.locator('.decision-card').count(), 1);
    assert.equal(await page.locator('#owner-filter option').first().innerText(), 'All owners');
    await page.getByRole('button', {name:/^View\b/}).click();
    await page.waitForSelector('#modal[open] #answer-form');
    assert.equal(await page.getByLabel('Your answer').inputValue(), 'Stop after three attempts and notify the customer.');
    await page.getByLabel('Your answer').fill('Stop after two attempts and notify the customer.');
    await page.locator('#answer-form').getByLabel('Why', {exact:true}).fill('Updated policy after reviewing provider limits.');
    await page.getByRole('button', {name:'Save correction',exact:true}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    await nav('Settings').click();
    await page.locator('#repository-signals > summary').click();
    const downloaded = page.waitForEvent('download');
    await page.getByRole('button', {name:'Export history',exact:true}).click();
    const download = await downloaded;
    const exported = JSON.parse(fs.readFileSync(await download.path(), 'utf8'));
    assert(exported.events.some(e => e.kind === 'previous_answer' && e.detail.includes('three attempts')));
    assert(exported.events.some(e => e.kind === 'task_note' && e.detail.includes('02:00 UTC')));
    // An old Memory bookmark lands on the inbox's Decided tab.
    await page.goto(`${url}/#memory`);
    await page.waitForSelector('.decision-card');
    assert.equal(new URL(page.url()).hash, '#inbox');
    assert.equal(await tab('Decided').getAttribute('aria-pressed'), 'true');
    assert.match(await page.locator('#app').textContent(), /Stop after two attempts/);

    // The owner makes the answer a reusable rule from its card. Measured on
    // the hard end-to-end run: the form reloaded the page and made nothing.
    await page.locator('#inbox-search').fill('payment retries');
    await page.getByRole('button', {name:/^View\b/}).click();
    await page.locator('#decision-details > summary').click();
    await page.locator('#modal summary', {hasText:'Make this a rule'}).click();
    await page.fill('#rule-conditions', 'card payments');
    await page.check('#rule-scope');
    await page.getByRole('button', {name:'Make it a rule',exact:true}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    const ruled = await (await fetch(`${url}/api/decisions/${decision.id}`)).json();
    assert.equal(ruled.reusable, 1);
    assert.equal(ruled.rule_scope, 'any');
    assert.match(ruled.rule_conditions, /card payments/);

    // Escape HTML in stored user/agent content; never insert it into the DOM as markup.
    await page.evaluate(async () => {
      const s = await (await fetch('/api/state')).json();
      await fetch('/api/owners', {method:'POST',headers:{'Content-Type':'application/json','X-Bridge-CSRF':s.csrf_token},body:JSON.stringify({name:'<img src=x onerror=alert(1)>',team:'Escaping test',patterns:'xss/*'})});
    });
    await page.reload();
    await page.waitForSelector('.decision-card');
    await nav('People').click();
    await page.waitForSelector('.person-card');
    assert.equal(await page.locator('.person-card img').count(), 0);
    assert.match(await page.locator('#app').textContent(), /<img src=x onerror=alert\(1\)>/);

    // An authority whose date has passed decides nothing; it used to read
    // the same as a live one, which is how a map looks set up and routes
    // nowhere.
    await page.evaluate(async () => {
      const s = await (await fetch('/api/state')).json();
      const head = {'Content-Type':'application/json','X-Bridge-CSRF':s.csrf_token};
      await fetch('/api/people', {method:'POST',headers:head,
        body:JSON.stringify({name:'Robin Past',email:'robin@test.example'})});
      await fetch('/api/authority', {method:'POST',headers:head,
        body:JSON.stringify({person:'Robin Past',scope_kind:'path',scope:'payments/*',role:'decides',
                             effective_to:'2020-01-01',by:'Local operator'})});
    });
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#app').textContent.includes('Robin Past'));
    const robin = page.locator('.person-card').filter({hasText:'Robin Past'});
    assert.match(await robin.textContent(), /decides payments\/\* · every repository · from config · asserted by Local operator · ended 2020-01-01/);
    assert.equal(await robin.locator('.authority-row.ended button').count(), 0);

    // A reviewer the map says must approve a path signs every decision
    // about it, even one that only inherits the path from its task, and
    // the inbox names who a decision still waits on. Measured live: such a
    // node finished on its owner's answer alone.
    const ledger = await page.evaluate(async () => {
      const s = await (await fetch('/api/state')).json();
      const head = {'Content-Type':'application/json','X-Bridge-CSRF':s.csrf_token};
      const post = async (to, body) => (await fetch(to, {method:'POST',headers:head,body:JSON.stringify(body)})).json();
      await post('/api/people', {name:'Lee Ledger',email:'lee@test.example'});
      await post('/api/people', {name:'Zoe Reviewer',email:'zoe@test.example'});
      await post('/api/authority', {person:'Lee Ledger',scope_kind:'path',scope:'ledger/*',role:'decides',by:'Local operator'});
      await post('/api/authority', {person:'Zoe Reviewer',scope_kind:'path',scope:'ledger/*',role:'approves',by:'Local operator'});
      const task = await post('/api/tasks/start', {title:'Close the books early',repo:'demo/books',paths:'ledger/close.py'});
      const node = await post(`/api/tasks/${task.task_id}/nodes`, {question:'May the month-end close run before every import lands?'});
      return {task:task.task_id, node};
    });
    assert.deepEqual(ledger.node.required_signers, ['Zoe Reviewer']);
    await page.goto(`${url}/#inbox`);
    await page.waitForSelector('.decision-card');
    await tab('All open').click();
    const closing = page.locator('#app .decision-card').filter({hasText:'May the month-end close run'});
    assert.match(await closing.locator('.pill').innerText(), /^Waiting on Lee Ledger$/);
    await closing.getByRole('button', {name:/^Answer\b/}).click();
    await page.getByLabel('Your answer').fill('No. Wait for every import, then close.');
    await page.locator('#answer-form').getByLabel('Why', {exact:true}).fill('A partial close has to be reopened by hand.');
    await page.getByRole('button', {name:'Record my answer',exact:true}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    await page.waitForFunction(() => /Signed by Lee Ledger, waiting on Zoe Reviewer/.test(document.querySelector('#app').textContent));
    assert.equal(await closing.locator('.pill').innerText(), 'Signed by Lee Ledger, waiting on Zoe Reviewer');
    const finishCall = {jsonrpc:'2.0',id:2,method:'tools/call',params:{name:'bridge_finish_task',arguments:{task_id:ledger.task}}};
    const early = await (await fetch(`${url}/mcp`, {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(finishCall)})).json();
    assert.match(JSON.stringify(early), /sign-off wanted from Zoe Reviewer/);
    await closing.getByRole('button', {name:/^Sign\b/}).click();
    await page.waitForFunction(() => /Signed by Lee Ledger, waiting on Zoe Reviewer/.test(document.querySelector('#modal').textContent));
    await page.getByRole('button', {name:/^Sign as Zoe Reviewer$/}).click();
    await page.waitForFunction(() => /Signed by Lee Ledger, Zoe Reviewer/.test(document.querySelector('#modal').textContent));
    // A follow-up the owner marks required holds the task until the agent
    // takes it up. Measured live: one left unadopted let the task finish.
    await page.locator('#modal details.history > summary', {hasText:/^More$/}).click();
    await page.fill('#followups', 'Which imports may still arrive after the close?');
    await page.check('#followups-required');
    await page.getByRole('button', {name:/Add follow-up questions/}).click();
    await page.waitForFunction(() => /1 follow-up question added/.test(document.querySelector('#toast')?.textContent || ''));
    const held = await (await fetch(`${url}/mcp`, {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(finishCall)})).json();
    assert.match(JSON.stringify(held), /a follow-up a person marked required; adopt it with bridge_add_node/);
    await page.keyboard.press('Escape');

    await nav('Settings').click();
    await page.getByRole('button', {name:'Copy configuration'}).click();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    const copiedBridge = JSON.parse(copied).mcpServers.bridge;
    assert.equal(copiedBridge.type, 'http');
    assert.equal(copiedBridge.url, `${url}/mcp`);

    // Every view fits a mobile viewport, and dialogs remain operable.
    await page.setViewportSize({width:390,height:844});
    for (const route of ['inbox','runs','owners','connect','today']) {
      await page.goto(`${url}/#${route}`);
      await page.waitForFunction(() => document.querySelector('#connection').textContent.includes('Local workspace'));
      await page.waitForTimeout(150);
      // Name the element that overflows. A bare boolean here cost a
      // reviewer a red run nobody could reproduce.
      const overflow = await page.evaluate(() => {
        const wide = document.documentElement.scrollWidth - innerWidth;
        if (wide <= 1) return null;
        const guilty = [...document.querySelectorAll('body *')]
          .map(el => ({el, r: el.getBoundingClientRect()}))
          .filter(({r}) => r.width > 0 && r.right > innerWidth + 1)
          .sort((a, b) => b.r.right - a.r.right)
          .slice(0, 3)
          .map(({el, r}) => `${el.tagName.toLowerCase()}.${(el.className || '').toString().split(' ').filter(Boolean).join('.')} right=${Math.round(r.right)} w=${Math.round(r.width)} "${(el.textContent || '').trim().slice(0, 60)}"`);
        return {wide, guilty};
      });
      assert(overflow === null, `${route} overflows on mobile by ${overflow && overflow.wide}px: ${overflow && overflow.guilty.join(' | ')}`);
      if (route === 'inbox') await page.screenshot({path:path.join(artifacts,'mobile.png'),fullPage:true});
    }
    await askAQuestion();
    await page.screenshot({path:path.join(artifacts,'mobile-dialog.png'),fullPage:true});
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#modal').evaluate(el => el.open), false);
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: four-page navigation, seeded canvas task, sign-off from All open, hand-on, routing, approval → MCP retrieval, corrections on Decided, persistence, export from Settings, escaping, setup copy, and five mobile views.');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) { const exited = once(server, 'exit'); server.kill('SIGTERM'); await exited; }
    fs.rmSync(temp, {recursive:true,force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
