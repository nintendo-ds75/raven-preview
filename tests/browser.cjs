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
    'import sys; from bridge.store import Store; s = Store(sys.argv[1]); s.graph.set_setting("workspace_name", "Browser test workspace")', db],
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
    // Manual actions sit under "More" on every view; the disclosure keeps its state across views.
    const openMore = async () => {
      const more = page.locator('#manual-actions');
      if (!(await more.evaluate(d => d.open))) await more.locator('summary').click();
    };
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    await page.goto(url);
    await page.waitForSelector('.decision-card');
    // Three open questions and one node the demo agent settled itself, waiting for sign-off.
    assert.equal(await page.locator('.decision-card').count(), 4);
    assert.equal(await page.locator('#inbox-count').textContent(), '4');
    await page.screenshot({path:path.join(artifacts,'desktop.png'),fullPage:true});

    // The seeded canvas task shows its kickoff verdict and its tree.
    await page.getByRole('link', {name:'Tasks',exact:true}).click();
    await page.locator('tr').filter({hasText:'Move the nightly backup job'}).getByRole('button', {name:'View run',exact:true}).click();
    await page.waitForSelector('.task-summary');
    assert.match(await page.locator('#app').textContent(), /Requested by Priya Natarajan/);
    assert.match(await page.locator('#app').textContent(), /What the agent has learned/);
    const taskUrl = page.url();
    // A deep link may return the tree before the account. Never interpret
    // that missing auth state as permission to show editing controls.
    const delayed = await browser.newPage();
    let releaseAccount;
    const accountReady = new Promise(resolve => { releaseAccount = resolve; });
    await delayed.route('**/api/state', async route => { await accountReady; await route.continue(); });
    await delayed.goto(taskUrl);
    await delayed.locator('#app').getByText('Loading workspace…', {exact:true}).waitFor();
    assert.equal(await delayed.locator('#note-form').count(), 0);
    releaseAccount();
    await delayed.waitForSelector('.task-summary');
    assert.equal(await delayed.locator('#note-form').count(), 1);
    await delayed.close();
    await page.getByRole('button', {name:'Decisions (2)',exact:true}).click();
    assert.equal(await page.locator('#app .tree-node').count(), 2);
    assert.match(await page.locator('#app .tree-node').nth(1).textContent(), /Level 1 · added by a person/);
    await page.getByRole('button', {name:'Overview',exact:true}).click();
    await page.locator('#app summary', {hasText:'Add context for the agent'}).click();
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
    await page.getByRole('link', {name:'Judgment inbox',exact:true}).click();

    // A node the agent settled sits under Needs review until a person signs it off.
    const settled = page.locator('#app .decision-card').filter({hasText:'Should the nightly backup keep its 30-day retention'});
    await settled.waitFor();
    assert.match(await settled.textContent(), /Settled by the agent/);
    assert.match(await settled.textContent(), /Sign-off wanted/);
    await settled.getByRole('button', {name:'Review',exact:true}).click();
    await page.waitForSelector('#modal[open] #correct-form');
    assert.equal(await page.locator('#modal #answer-form').count(), 0);
    assert.match(await page.locator('#modal').textContent(), /Evidence: settled by the agent/);
    await page.getByRole('button', {name:/Sign off as Alex Morgan/}).click();
    await page.waitForFunction(() => document.querySelector('#modal').textContent.includes('Signed · Alex Morgan'));
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => document.querySelectorAll('#app .decision-card').length === 3);
    assert.equal(await settled.count(), 0);
    assert.equal(await page.locator('#inbox-count').textContent(), '3');

    // Suggestions can be inspected, but remain unapproved until a human records an answer.
    await page.getByRole('button', {name:/Suggested/}).click();
    assert.equal(await page.locator('.decision-card').count(), 1);
    await page.getByRole('button', {name:'Review',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#modal-title').textContent === 'Should unused prepaid credits expire at the end of the month?');
    // The suggestion sits beside the answer box, never inside it: a person
    // must choose to use it.
    assert.equal(await page.locator('#answer').inputValue(), '');
    assert.match(await page.locator('#modal .prediction').textContent(), /Roll unused credits/);
    await page.getByRole('button', {name:/Use this text as the answer/}).click();
    assert.match(await page.locator('#answer').inputValue(), /Roll unused credits/);
    await page.getByRole('button', {name:/Inspect source decision/}).click();
    await page.waitForFunction(() => document.querySelector('#modal-title').textContent === 'Should unused prepaid credits expire?');
    await page.keyboard.press('Escape');

    // Handing a question on shows what it would teach Raven, and a person can choose that it
    // teaches nothing. Measured live: a docs hand-on taught security authority nobody chose.
    await page.evaluate(async () => {
      const s = await (await fetch('/api/state')).json();
      await fetch('/api/people', {method:'POST', headers:{'Content-Type':'application/json','X-Bridge-CSRF':s.csrf_token},
        body:JSON.stringify({name:'Riley Handon', email:'riley@example.test'})});
    });
    await page.reload();
    await page.waitForSelector('.decision-card');
    await page.getByRole('button', {name:/Needs review/}).click();
    const openQuestion = page.locator('#app .decision-card').filter({hasNotText:'prepaid credits'}).first();
    const handedQuestion = (await openQuestion.locator('h3').textContent()).trim();
    await openQuestion.getByRole('button', {name:'Review',exact:true}).click();
    await page.waitForSelector('#modal[open] #refer-scope');
    const teach = await page.locator('#refer-scope option').allTextContents();
    assert(teach.includes('this question only'), teach.join(' | '));
    await page.selectOption('#refer-scope', {label:'this question only'});
    const riley = await page.locator('#assign-owner option', {hasText:'Riley Handon'}).getAttribute('value');
    await page.selectOption('#assign-owner', riley);
    await page.getByRole('button', {name:/Hand on/}).click();
    await page.waitForFunction(() => /for this question only/.test(document.querySelector('#toast')?.textContent || ''));
    const handed = await (await fetch(`${url}/api/state`)).json();
    assert.equal(handed.decisions.find(d => d.question === handedQuestion).owner_name, 'Riley Handon');
    const learned = await (await fetch(`${url}/api/people`)).json();
    assert.equal(learned.authority.filter(a => a.source === 'referral').length, 0);
    await page.keyboard.press('Escape');

    // Add a real owner and a routed request using the web UI.
    await page.getByRole('link', {name:'People and ownership',exact:true}).click();
    await openMore();
    await page.getByRole('button', {name:'Add owner',exact:true}).click();
    await page.getByLabel('Name', {exact:true}).fill('Jamie Test');
    await page.getByLabel('Team', {exact:true}).fill('Payments');
    await page.getByLabel('Path patterns').fill('payments/*');
    await page.locator('#owner-form').getByRole('button', {name:'Add owner',exact:true}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    await page.getByRole('link', {name:'Judgment inbox',exact:true}).click();
    await openMore();
    await page.getByRole('button', {name:'New request',exact:true}).click();
    await page.getByLabel('Task', {exact:true}).fill('Verify retry policy');
    await page.getByLabel('Agent', {exact:true}).fill('Browser test agent');
    await page.getByLabel('Repository', {exact:true}).fill('test/platform');
    await page.getByLabel('What decision is needed?').fill('Should payment retries stop after three attempts?');
    await page.getByLabel('Context & evidence').fill('The retry worker can cap attempts. Confirm the customer-facing policy.');
    await page.getByLabel('Relevant file path').fill('payments/retries.py');
    await page.getByRole('button', {name:/Create request/}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    await page.getByRole('button', {name:/Needs review/}).click();
    const card = page.locator('.decision-card').filter({hasText:'Should payment retries stop after three attempts?'});
    await card.waitFor();
    assert.match(await card.textContent(), /Jamie Test/);
    // The form started a task on the canvas and wrote the question as its first node.
    const started = await (await fetch(`${url}/api/state`)).json();
    const task = started.runs.find(r => r.title === 'Verify retry policy');
    assert.equal(task.paths, 'payments/retries.py');
    assert.match(task.verdict, /^(engage|pass)$/);
    const tree = await (await fetch(`${url}/api/tasks/${task.id}/tree`)).json();
    assert.equal(tree.nodes[0].question, 'Should payment retries stop after three attempts?');
    assert.equal(tree.nodes[0].owner, 'Jamie Test');
    await card.getByRole('button', {name:'Review',exact:true}).click();
    await page.getByLabel('Your answer').fill('Stop after three attempts and notify the customer.');
    await page.getByLabel('Why this decision?').fill('Avoid duplicate charges and make the failure visible.');
    await page.getByRole('button', {name:/Record decision/}).click();
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

    // Corrections are persistent, searchable, and exported with the prior answer retained.
    await page.getByRole('link', {name:'Decision memory',exact:true}).click();
    await page.getByRole('searchbox', {name:'Search decision memory'}).fill('payment retries');
    assert.equal(await page.locator('.decision-card').count(), 1);
    await page.getByRole('button', {name:/Context & history/}).click();
    await page.getByLabel('Recorded answer · edit to make a correction').fill('Stop after two attempts and notify the customer.');
    await page.getByLabel('Why this decision?').fill('Updated policy after reviewing provider limits.');
    await page.getByRole('button', {name:'Save correction'}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    const downloaded = page.waitForEvent('download');
    await page.getByRole('button', {name:/Export history/}).click();
    const download = await downloaded;
    const exported = JSON.parse(fs.readFileSync(await download.path(), 'utf8'));
    assert(exported.events.some(e => e.kind === 'previous_answer' && e.detail.includes('three attempts')));
    assert(exported.events.some(e => e.kind === 'task_note' && e.detail.includes('02:00 UTC')));
    await page.reload();
    await page.waitForSelector('.decision-card');
    assert.match(await page.locator('#app').textContent(), /Stop after two attempts/);

    // The owner makes the answer a reusable rule from its card. Measured on
    // the hard end-to-end run: the form reloaded the page and made nothing.
    await page.getByRole('link', {name:'Decision memory',exact:true}).click();
    await page.getByRole('searchbox', {name:'Search decision memory'}).fill('payment retries');
    await page.getByRole('button', {name:/Context & history/}).click();
    await page.locator('#modal summary', {hasText:'Make this answer a reusable rule'}).click();
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
    await page.getByRole('link', {name:'People and ownership',exact:true}).click();
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
    const authorityText = await page.locator('#app').textContent();
    assert.match(authorityText, /expired: decides nothing now/);
    assert.match(authorityText, /1 expired/);

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
    await page.getByRole('button', {name:/Needs review/}).click();
    const closing = page.locator('#app .decision-card').filter({hasText:'May the month-end close run'});
    await closing.getByRole('button', {name:'Review',exact:true}).click();
    await page.getByLabel('Your answer').fill('No. Wait for every import, then close.');
    await page.getByLabel('Why this decision?').fill('A partial close has to be reopened by hand.');
    await page.getByRole('button', {name:/Record decision/}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    await page.getByRole('button', {name:/Needs review/}).click();
    assert.match(await closing.textContent(), /Signed in part/);
    assert.match(await closing.textContent(), /Waiting on Zoe Reviewer/);
    const finishCall = {jsonrpc:'2.0',id:2,method:'tools/call',params:{name:'bridge_finish_task',arguments:{task_id:ledger.task}}};
    const early = await (await fetch(`${url}/mcp`, {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(finishCall)})).json();
    assert.match(JSON.stringify(early), /sign-off wanted from Zoe Reviewer/);
    await closing.getByRole('button', {name:'Review',exact:true}).click();
    await page.waitForFunction(() => /Answered and signed by Lee Ledger, still waiting on Zoe Reviewer/.test(document.querySelector('#modal').textContent));
    await page.getByRole('button', {name:/Sign off as Zoe Reviewer/}).click();
    await page.waitForFunction(() => /Signed · Lee Ledger, Zoe Reviewer/.test(document.querySelector('#modal').textContent));
    // A follow-up the owner marks required holds the task until the agent
    // takes it up. Measured live: one left unadopted let the task finish.
    await page.fill('#followups', 'Which imports may still arrive after the close?');
    await page.check('#followups-required');
    await page.getByRole('button', {name:/Add follow-up questions/}).click();
    await page.waitForFunction(() => /1 follow-up question added/.test(document.querySelector('#toast')?.textContent || ''));
    const held = await (await fetch(`${url}/mcp`, {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(finishCall)})).json();
    assert.match(JSON.stringify(held), /a follow-up a person marked required; adopt it with bridge_add_node/);
    await page.keyboard.press('Escape');

    await page.getByRole('link', {name:'Connections & setup',exact:true}).click();
    await page.getByRole('button', {name:'Copy configuration'}).click();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    const copiedBridge = JSON.parse(copied).mcpServers.bridge;
    assert.equal(copiedBridge.type, 'http');
    assert.equal(copiedBridge.url, `${url}/mcp`);

    // Every view fits a mobile viewport, and dialogs remain operable.
    await page.setViewportSize({width:390,height:844});
    for (const route of ['inbox','runs','memory','owners','connect']) {
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
    await page.goto(`${url}/#inbox`);
    await openMore();
    await page.getByRole('button', {name:'New request',exact:true}).click();
    await page.screenshot({path:path.join(artifacts,'mobile-dialog.png'),fullPage:true});
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#modal').evaluate(el => el.open), false);
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: seeded canvas task, sign-off from Needs review, routing, approval → MCP retrieval, corrections, persistence, export, escaping, setup copy, and five mobile views.');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) { const exited = once(server, 'exit'); server.kill('SIGTERM'); await exited; }
    fs.rmSync(temp, {recursive:true,force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
