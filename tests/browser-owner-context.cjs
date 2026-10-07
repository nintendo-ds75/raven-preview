/* New synthetic UI regression: local Chromium, read-only presentation only. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const readline = require('node:readline');
const {once} = require('node:events');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'raven-owner-context-'));
  const child = spawn(process.env.BRIDGE_PYTHON || 'python3',
    [path.join(root, 'tests/owner_context_browser_server.py'), path.join(temp, 'scope.db')], {cwd:root});
  let stderr = '', browser;
  child.stderr.on('data', chunk => { stderr += chunk; });
  try {
    const lines = readline.createInterface({input:child.stdout});
    const fixture = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Fixture startup timeout: ' + stderr)), 30000);
      lines.on('line', line => { try { const value = JSON.parse(line); clearTimeout(timer); resolve(value); } catch {} });
      child.once('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited ' + code + ': ' + stderr)); });
    });
    const read = async route => {
      const response = await fetch(fixture.url + route);
      assert.equal(response.status, 200, await response.clone().text());
      return response.json();
    };
    const decision = () => read('/api/decisions/' + fixture.node_id);
    const stable = d => Object.fromEntries(['answer','updated_at','revision','signoff','signatures','signed_by',
      'signed_hash','signed_revision','authorized','sources','source_revalidation','approval_scope','approval_scope_text']
      .map(key => [key,d[key]]));
    const initial = stable(await decision());
    assert.equal(initial.authorized, false);
    browser = await chromium.launch({headless:true,
      ...(process.env.BRIDGE_BROWSER ? {executablePath:process.env.BRIDGE_BROWSER} : {})});
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.setDefaultTimeout(10000);
    await page.route('**/*', route => new URL(route.request().url()).origin === fixture.url
      ? route.continue() : route.abort());
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const artifacts = process.env.BRIDGE_TEST_ARTIFACTS || path.join(root, 'test-results');
    fs.mkdirSync(artifacts, {recursive:true});
    const screenshots = [];
    const screenshot = async name => {
      const filename = 'new-synthetic-ui-' + name + '.png';
      await page.screenshot({path:path.join(artifacts,filename),fullPage:true});
      screenshots.push(filename);
    };
    const brief = () => page.goto(fixture.url + '/brief#' + fixture.token);
    await brief();
    await page.locator('#focus-question').waitFor();
    assert.equal(await page.locator('.brief-hero h1').innerText(), 'Decision review · WORK-42');
    assert.equal(await page.locator('#focus-question').innerText(), fixture.question);
    assert.equal(await page.locator('.brief-prompt p').textContent(), fixture.request);
    assert.equal(await page.locator('.scope-exact').getAttribute('open'), null);
    for (const value of ['Cedar','preview','WORK-42']) {
      assert((await page.locator('[data-scope-field="facts"]').innerText()).includes(value));
    }
    assert((await page.locator('[data-scope-field="context"]').innerText()).includes('no implementation, launch or source change'));
    assert((await page.locator('[data-scope-field="scope_paths"]').innerText()).includes('src/release_policy/version.py'));
    await screenshot('owner-context-desktop');
    await page.getByText('Exact recorded scope', {exact:true}).click();
    assert.deepEqual(JSON.parse(await page.locator('.scope-json').textContent()), initial.approval_scope);
    await page.getByText('Original scope text', {exact:true}).click();
    assert.equal(await page.locator('.scope-text').textContent(), initial.approval_scope_text);
    await page.evaluate(() => render());
    assert.notEqual(await page.locator('.scope-exact').getAttribute('open'), null);
    assert.equal(await page.locator('.scope-text').isVisible(), true);
    await page.getByText('Exact recorded scope', {exact:true}).click();
    // Closing and re-opening details never resets a draft or changes bound pins.
    await page.locator('#answer-text').fill('Unsubmitted presentation test draft');
    await page.getByText('Exact recorded scope', {exact:true}).click();
    await page.getByText('Exact recorded scope', {exact:true}).click();
    assert.equal(await page.locator('#answer-text').inputValue(), 'Unsubmitted presentation test draft');
    await page.locator('#answer-text').fill('');
    assert.deepEqual(stable(await decision()), initial);
    await page.setViewportSize({width:390,height:844});
    await page.locator('.brief-stack').waitFor();
    assert.equal(await page.locator('.brief-prompt p').textContent(), fixture.request);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile overflow');
    await screenshot('owner-context-mobile');
    // Render adversarial-looking, unusual synthetic values through the shared
    // shipped helper in the actual browser, without mutating any bound record.
    const unusual = {zero:0, false:false, null:null, empty:'', array:[], object:{},
      '<img src=x onerror=alert(1)>':'<script>throw new Error("bad")</script>',
      nested:{unicode:['日本語',false,0,null]}, long:'Unbroken'.repeat(200)};
    await page.evaluate(facts => {
      const element = document.createElement('section'); element.id = 'synthetic-unusual';
      element.innerHTML = approvalScope({question:'Exact question', approval_scope:{question:'Exact question',facts},
        approval_scope_labels:{facts:'Facts'}, approval_scope_text:'Original text'});
      document.querySelector('#brief').append(element);
    }, unusual);
    assert.equal(await page.locator('#synthetic-unusual img, #synthetic-unusual script').count(), 0);
    assert.deepEqual(JSON.parse(await page.locator('#synthetic-unusual .scope-json').textContent()).facts, unusual);
    assert((await page.locator('#synthetic-unusual [data-scope-field="facts"]').innerText()).includes(unusual.long));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'unusual mobile overflow');
    await page.evaluate(() => document.querySelector('#synthetic-unusual').remove());
    // The main task page and owner modal consume the same presentation helper.
    await page.setViewportSize({width:1280,height:900});
    await page.goto(fixture.url + '/#runs/' + fixture.task_id);
    await page.locator('#task-tab-overview').waitFor();
    assert.equal(await page.locator('.task-heading h1').innerText(), 'Task overview · WORK-42');
    await page.locator('#task-brief summary').click();
    assert.equal(await page.locator('.task-goal').textContent(), fixture.request);
    await page.evaluate(id => review(id), fixture.node_id);
    await page.locator('#modal[open] .decision-scope').waitFor();
    await page.locator('#modal .scope-exact > summary').click();
    assert.deepEqual(JSON.parse(await page.locator('#modal .scope-json').textContent()), initial.approval_scope);
    await page.getByRole('button', {name:'Close',exact:true}).last().click();
    await page.evaluate(id => review(id), fixture.node_id);
    await page.locator('#modal[open] .decision-scope').waitFor();
    assert.equal(await page.locator('#modal .scope-exact').getAttribute('open'), null);
    await page.getByRole('button', {name:'Close',exact:true}).last().click();
    await page.goBack();
    await page.locator('#focus-question').waitFor();
    assert.equal(await page.locator('.brief-prompt p').textContent(), fixture.request);
    assert.deepEqual(stable(await decision()), initial);
    const task = await read('/api/tasks/' + fixture.task_id + '/tree');
    assert.equal(task.title, fixture.request.slice(0,300));
    assert.equal(task.goal, fixture.request);
    assert.deepEqual(errors, []);
    const result = {ok:true, fixture:'New synthetic UI, not the recovered task', browser:'Chromium',
      checks:['concise exact structured heading','complete request retained','complete question and scope facts',
        'expandable exact JSON and original text','desktop and mobile layout','unusual nested and escaped values',
        'draft survives details toggles','shared task and modal presentation','close/reopen and back navigation',
        'stored title/goal, source pins, revisions and signatures unchanged','no browser errors'], screenshots};
    fs.writeFileSync(path.join(artifacts, 'owner-context-browser-result.json'), JSON.stringify(result,null,2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null && child.signalCode === null) { child.kill('SIGTERM'); await once(child,'exit').catch(() => {}); }
    fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(error => {console.error(error); process.exitCode=1;});
