/* Real local UI/worker, deterministic fake provider, no credentials or network inference. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-agents-browser-'));
  const root = path.resolve(__dirname, '..');
  const server = spawn(process.env.BRIDGE_PYTHON || 'python3', ['tests/agents_browser_server.py', path.join(temp, 'test.db')], {cwd:root});
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Server startup timeout')), 10000);
      server.stdout.on('data', data => { const match = String(data).match(/http:\/\/127\.0\.0\.1:\d+/); if (match) {clearTimeout(timer); resolve(match[0]);} });
      server.once('error', reject);
      server.stderr.on('data', () => {});
      server.once('exit', code => {clearTimeout(timer); reject(new Error(`Server exited ${code}`));});
    });
    browser = await chromium.launch({headless:true, ...(process.env.BRIDGE_BROWSER ? {executablePath:process.env.BRIDGE_BROWSER} : {})});
    const page = await browser.newPage({viewport:{width:1440,height:1050}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(url);
    await page.getByRole('button', {name:'Start a task',exact:true}).click();
    await page.getByLabel('Task', {exact:true}).fill('Add usage-based pricing');
    assert.equal(await page.getByLabel('Repository').inputValue(), 'billing-fixture');
    await page.getByRole('button', {name:'Start task',exact:true}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    await page.waitForFunction(async () => (await (await fetch('/api/state')).json()).decisions.some(d => d.status === 'pending'));
    await page.goto(url);
    await page.getByRole('button', {name:'Review',exact:true}).click();
    await page.waitForSelector('#modal[open] #answer');
    assert.match(await page.locator('#modal').textContent(), /Evidence:/);
    await page.getByLabel('Your answer').fill('Exclude this fixture spike only.');
    await page.getByLabel('Why this decision?').fill('Browser validation policy.');
    await page.getByRole('button', {name:/Record decision/}).click();
    await page.waitForSelector('#modal:not([open])', {state:'attached'});
    // Delivery and continuation happen with the browser closed.
    await page.close();
    const deadline = Date.now() + 15000;
    let state;
    while (Date.now() < deadline) {
      state = await (await fetch(`${url}/api/state`)).json();
      if (state.executions[0]?.status === 'result_ready') break;
      await new Promise(r => setTimeout(r, 300));
    }
    assert.equal(state.runs.length, 1);
    assert.equal(state.executions[0].status, 'result_ready');
    assert.equal(state.deliveries[0].state, 'delivered');
    assert.match(state.deliveries[0].payload, /Exclude this fixture spike only/);
    const result = await browser.newPage({viewport:{width:390,height:844}});
    await result.goto(`${url}/#runs`);
    await result.getByRole('button', {name:'View run',exact:true}).click();
    await result.getByRole('button', {name:'Execution output & files',exact:true}).click();
    assert.match(await result.locator('#modal').textContent(), /result ready/);
    assert.match(await result.locator('#modal').textContent(), /exit 0/);
    assert.equal(await result.locator('#modal script').count(), 0);
    assert(await result.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    fs.mkdirSync(path.join(root,'test-results'), {recursive:true});
    await result.screenshot({path:path.join(root,'test-results','agents-result-mobile.png'),fullPage:true});
    assert.deepEqual(errors, []);
    console.log('Agents browser checks passed: task intake, routed review, delivery with browser closed, run output, escaping, mobile.');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) { const exited = once(server, 'exit'); server.kill('SIGTERM'); await exited; }
    fs.rmSync(temp, {recursive:true,force:true});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
