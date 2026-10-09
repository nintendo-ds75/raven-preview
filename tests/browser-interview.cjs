/* UI tests use mocked speech APIs only: no live microphone or provider claim. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'raven-interview-browser-'));
  const root = path.resolve(__dirname, '..');
  const server = spawn(process.env.BRIDGE_PYTHON || 'python3', ['tests/interview_browser_server.py', path.join(temp, 'test.db')], {cwd:root});
  let browser;
  server.stderr.on('data', () => {});
  try {
    const world = await new Promise((resolve, reject) => {
      let text = '';
      const timer = setTimeout(() => reject(new Error('Interview server did not start')), 10000);
      server.stdout.on('data', chunk => {
        text += chunk;
        if (text.includes('\n')) { clearTimeout(timer); resolve(JSON.parse(text.split('\n')[0])); }
      });
      server.once('error', reject);
    });
    browser = await chromium.launch({headless:true, executablePath:process.env.BRIDGE_BROWSER || '/usr/bin/chromium'});
    const context = await browser.newContext({viewport:{width:1280,height:1000}});
    const [name, value] = world.cookie.split('=');
    await context.addCookies([{name, value, url:world.url}]);
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(() => {
      window.testSpeech = {started:0, aborted:0, spoken:[], cancelled:0};
      class Recognition {
        constructor() { window.testRecognition = this; }
        start() { window.testSpeech.started++; }
        abort() { window.testSpeech.aborted++; }
      }
      window.SpeechRecognition = Recognition;
      Object.defineProperty(window, 'speechSynthesis', {value:{speak:u => window.testSpeech.spoken.push(u.text), cancel:() => window.testSpeech.cancelled++}});
      window.SpeechSynthesisUtterance = function(text) { this.text = text; };
    });
    await page.goto(`${world.url}/#runs/${world.task}`);
    await page.getByRole('button', {name:'Decisions (1)',exact:true}).click();
    const open = async () => {
      const question = page.locator('.task-question').first();
      if (!(await question.evaluate(el => el.open))) await question.locator(':scope > summary').click();
      await question.getByRole('button', {name:/Open decision/}).click();
      await page.getByRole('button', {name:'Start or resume interview',exact:true}).click();
      await page.locator('#interview-modal[open]').waitFor();
    };
    await open();
    await page.getByRole('button', {name:'Read question aloud',exact:true}).click();
    assert.match((await page.evaluate(() => window.testSpeech.spoken)).at(-1), /Which timeout/);
    await page.getByRole('button', {name:'Start microphone',exact:true}).click();
    await page.evaluate(() => window.testRecognition.onresult({resultIndex:0,results:[Object.assign([{transcript:'Use five seconds, except old clients keep ten.'}], {isFinal:true})]}));
    await page.waitForFunction(() => document.querySelector('[data-interview-status]').textContent.includes('Draft saved'));
    assert.match(await page.locator('#interview-response').inputValue(), /old clients keep ten/);
    // Recognition failure preserves responses and is durably visible after resume.
    await page.evaluate(() => window.testRecognition.onerror({error:'not-allowed'}));
    await page.getByText(/Microphone stopped: not-allowed/).waitFor();
    await page.waitForFunction(() => document.querySelector('[data-interview-status]').textContent === 'Interview failed.');
    await page.getByRole('button', {name:'Save draft and close interview',exact:true}).click();
    await page.locator('#interview-modal[open]').waitFor({state:'hidden'});
    await open();
    assert.match(await page.locator('#interview-response').inputValue(), /old clients keep ten/);
    await page.getByRole('button', {name:'Save response and continue',exact:true}).click();
    await page.getByText('Guided question 2 of 5', {exact:true}).waitFor();
    await page.getByText('No model interviewer is configured. Continuing with guided prompts.', {exact:true}).waitFor();
    await page.locator('#interview-answer').fill('Use five seconds for new clients. Existing clients retain ten seconds.');
    await page.locator('#interview-rationale').fill('Retain compatibility, including the old-client exception.');
    await page.getByRole('button', {name:'Save and review',exact:true}).click();
    await page.getByRole('heading', {name:'Review your decision',exact:true}).waitFor();
    await page.getByRole('button', {name:'Read decision aloud',exact:true}).click();
    assert.match((await page.evaluate(() => window.testSpeech.spoken)).at(-1), /Existing clients retain ten/);
    // Readback and saving do not sign anything.
    let decision = await (await context.request.get(`${world.url}/api/decisions/${world.node}`)).json();
    assert.equal(decision.signed_by, '');
    await page.getByRole('button', {name:'Back to edit',exact:true}).click();
    await page.locator('#interview-rationale').fill('Preserve old clients; test both timeout defaults.');
    await page.getByRole('button', {name:'Save and review',exact:true}).click();
    await page.getByRole('button', {name:'Confirm and sign as Ada Owner',exact:true}).click();
    await page.locator('#interview-modal[open]').waitFor({state:'hidden'});
    decision = await (await context.request.get(`${world.url}/api/decisions/${world.node}`)).json();
    assert.equal(decision.signed_by, 'Ada Owner');
    assert.match(decision.rationale, /test both/);
    assert.equal(decision.events.filter(e => e.kind === 'interview_confirmed').length, 1);
    // Escape saves and stops all audio; navigation never reopens a dismissed dialog.
    await open();
    await page.getByRole('button', {name:'Start microphone',exact:true}).click();
    const before = await page.evaluate(() => ({...window.testSpeech}));
    await page.keyboard.press('Escape');
    await page.locator('#interview-modal[open]').waitFor({state:'hidden'});
    const after = await page.evaluate(() => ({...window.testSpeech}));
    assert(after.aborted > before.aborted);
    assert(after.cancelled > before.cancelled);
    await open();
    await page.getByRole('button', {name:'Start microphone',exact:true}).click();
    await page.getByRole('button', {name:'Discard this interview',exact:true}).click();
    await page.locator('#interview-modal[open]').waitFor({state:'hidden'});
    await open();
    await page.getByRole('button', {name:'Start microphone',exact:true}).click();
    await page.evaluate(() => { location.hash = 'inbox'; });
    await page.locator('#interview-modal[open]').waitFor({state:'hidden'});
    assert.equal(await page.evaluate(() => interviewSession), null);
    // The same dialog is reachable from a personal task link without a cookie.
    const guest = await browser.newContext();
    const linked = await guest.newPage();
    await linked.goto(`${world.url}/brief#${world.link}`);
    await linked.getByRole('button', {name:'Start or resume interview',exact:true}).click();
    await linked.locator('#interview-modal[open]').waitFor();
    await linked.locator('#interview-answer').fill('Use five seconds for new clients only.');
    await linked.locator('#interview-rationale').fill('Old clients retain ten seconds.');
    await linked.getByRole('button', {name:'Save and review',exact:true}).click();
    await linked.getByText(/using your personal task link/).waitFor();
    assert.equal((await guest.request.get(`${world.url}/api/state`)).status(),401);
    await linked.getByRole('button', {name:'Back to edit',exact:true}).click();
    await linked.getByRole('button', {name:'Discard this interview',exact:true}).click();
    await guest.close();
    assert.deepEqual(errors, []);
    // Unsupported speech remains fully usable through typed input.
    await page.evaluate(() => { window.SpeechRecognition = undefined; window.webkitSpeechRecognition = undefined; });
    await page.goto(`${world.url}/#runs/${world.task}`);
    await page.getByRole('button', {name:'Decisions (1)',exact:true}).click();
    await page.evaluate(() => { window.SpeechRecognition = undefined; window.webkitSpeechRecognition = undefined; });
    await open();
    assert.equal(await page.getByRole('button', {name:'Start microphone',exact:true}).isDisabled(), true);
    await page.getByText(/Browser dictation is unavailable here/).waitFor();
    await page.locator('#interview-response').fill('Typed answers remain available.');
    await page.getByRole('button', {name:'Save draft',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('[data-interview-status]').textContent.includes('Draft saved'));
    fs.mkdirSync(path.join(root,'test-results'), {recursive:true});
    await page.screenshot({path:path.join(root, 'test-results/interview.png'),fullPage:true});
    console.log('Interview browser checks passed (mocked speech APIs; no real microphone/provider test).');
  } finally {
    if (browser) await browser.close();
    server.kill();
    fs.rmSync(temp, {recursive:true,force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
