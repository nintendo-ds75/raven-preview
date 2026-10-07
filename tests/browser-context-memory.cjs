/* Ordinary web-owner recovery against synthetic source changes; no providers. */
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
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'raven-context-browser-'));
  const child = spawn(process.env.BRIDGE_PYTHON || 'python3', [path.join(root, 'tests/context_browser_server.py'), path.join(temp, 'context.db')], {cwd:root});
  let stderr = '';
  child.stderr.on('data', data => { stderr += data; });
  let browser;
  try {
    const lines = readline.createInterface({input:child.stdout});
    const fixture = await new Promise((resolve,reject) => {
      const timer = setTimeout(() => reject(new Error('Fixture startup timeout: ' + stderr)), 30000);
      lines.on('line', line => { try {const value=JSON.parse(line);clearTimeout(timer);resolve(value);} catch {} });
      child.once('exit', code => {clearTimeout(timer);reject(new Error('Fixture exited ' + code + ': ' + stderr));});
    });
    const state = await (await fetch(fixture.url + '/api/state')).json();
    const read = async () => (await fetch(fixture.url + '/api/decisions/' + fixture.decision_id)).json();
    const update = async body => {
      const result = await fetch(fixture.url + '/api/records', {method:'POST',headers:{'Content-Type':'application/json','X-Bridge-CSRF':state.csrf_token},body:JSON.stringify({...fixture.source_data,body})});
      assert.equal(result.status,200); return result.json();
    };
    browser = await chromium.launch({headless:true,...(process.env.BRIDGE_BROWSER ? {executablePath:process.env.BRIDGE_BROWSER}: {})});
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    const errors = [];
    page.on('pageerror',error => errors.push(error.message));
    const confirmSourceReview = async decisionId => {
      const response = page.waitForResponse(r => r.url().endsWith('/api/decisions/' + decisionId + '/signoff')
        && r.request().method() === 'POST');
      await page.locator('[data-action="signoff"]').click();
      const result = await response;
      assert.equal(result.status(), 200, await result.text());
      // The shipped signedAs() label uses a middle dot, not "Signed by".
      await page.waitForFunction(() => document.querySelector('#modal')?.textContent.includes('Signed · Ada Example'));
    };
    await page.goto(fixture.url);
    await page.waitForFunction(() => typeof review === 'function' && state?.decisions?.length);
    await page.evaluate(id => review(id), fixture.decision_id);
    await page.locator('#modal[open]').waitFor();
    await page.locator('#source-review-signoff').waitFor();
    assert.match(await page.locator('.source-revalidation').first().textContent(), /Revision B: keep records for thirty days after migration/);
    assert.equal((await read()).authorized,false);
    // Closing the displayed snapshots is not approval.
    await page.getByRole('button',{name:'Close',exact:true}).last().click();
    assert.equal((await read()).authorized,false);
    await page.evaluate(id => review(id), fixture.decision_id);
    await page.locator('#source-review-signoff').check();
    const immutablePins = JSON.parse(await page.locator('#source-pins-signoff').inputValue());
    await confirmSourceReview(fixture.decision_id);
    const signed = await read();
    assert.equal(signed.authorized,true);
    assert.equal(signed.independent_source_replacement,0);
    assert.equal(signed.sources[0].source_version_id,immutablePins[0].source_version_id);
    // A race after a new displayed readback cannot use its old checkbox/pins.
    await update('Revision C: keep records for thirty days, with audited deletion.');
    await page.evaluate(id => review(id), fixture.decision_id);
    await page.locator('#source-review-signoff').check();
    await update('Revision D: the source changed after the owner read revision C.');
    const response = page.waitForResponse(r => r.url().endsWith('/signoff') && r.request().method() === 'POST');
    await page.locator('[data-action="signoff"]').click();
    assert.equal((await response).status(),400);
    assert.equal((await read()).authorized,false);
    await page.evaluate(id => review(id), fixture.decision_id);
    assert.match(await page.locator('.source-revalidation').first().textContent(), /Revision D/);
    await page.locator('#source-review-signoff').check();
    await confirmSourceReview(fixture.decision_id);
    assert.equal((await read()).authorized,true);
    // Pure-human chains expose the full reviewed scope and bind that exact
    // immutable human version, even with no external source snapshots.
    await page.evaluate(id => review(id), fixture.human_child);
    await page.locator('#source-review-signoff').waitFor();
    const humanReading = await page.locator('.source-revalidation').first().textContent();
    for (const field of ['Northstar', 'Release 4 only', 'Northstar contract limitation', 'scope_paths', 'applicability', 'required_signers', 'Seven days.']) assert.ok(humanReading.includes(field), field);
    assert.deepEqual(JSON.parse(await page.locator('#source-pins-signoff').inputValue()), []);
    const humanPins = JSON.parse(await page.locator('#source-decisions-signoff').inputValue());
    assert.equal(humanPins.length,1);
    assert.equal(humanPins[0].decision_id,fixture.human_parent);
    assert.ok(humanPins[0].source_version_id);
    assert.match(humanPins[0].source_snapshot_sha256,/^[a-f0-9]{64}$/);
    await page.locator('#source-review-signoff').check();
    await confirmSourceReview(fixture.human_child);
    const humanSigned = await (await fetch(fixture.url + '/api/decisions/' + fixture.human_child)).json();
    assert.equal(humanSigned.authorized,true);
    const pinned = JSON.parse(humanSigned.context_history.at(-1).snapshot).derivations.find(link => link.related_id === fixture.human_parent);
    assert.equal(pinned.source_version_id,humanPins[0].source_version_id);
    // The ordinary authenticated owner can explicitly replace a human-only
    // derivation without pretending to reapprove its former parent.
    await page.evaluate(id => review(id), fixture.human_child);
    await page.locator('#correct-form [name="evidence_mode"]').check();
    await page.locator('#correction').fill('Independent owner policy: five days for this child.');
    await page.locator('#correction-rationale').fill('Direct decision for this task; retire the former derivation.');
    const correction = page.waitForResponse(r => r.url().endsWith('/api/decisions/' + fixture.human_child + '/signoff')
      && r.request().method() === 'POST');
    await page.locator('#correct-form button[type="submit"]').click();
    const correctionResponse = await correction;
    assert.equal(correctionResponse.status(),200,await correctionResponse.text());
    const independent = await (await fetch(fixture.url + '/api/decisions/' + fixture.human_child)).json();
    assert.equal(independent.authorized,true);
    assert.equal(independent.independent_source_replacement,1);
    assert.equal(independent.source_id,null);
    assert.equal(independent.source_revalidation.has_reliance,false);

    // A Slack recipient reviews a large source through their existing personal
    // task link, with no Raven account and no workspace-wide API access.
    const longBody = 'Updated retention applies to partner archives. '.repeat(360) + 'Final requirement: thirty days.';
    await update(longBody);
    await page.setViewportSize({width:390,height:844});
    await page.goto(fixture.url + '/brief#' + fixture.personal_link);
    await page.locator('#brief-source-confirm').waitFor();
    assert((await page.locator('.brief-source-body').first().innerText()).includes('Final requirement: thirty days.'));
    await page.locator('#answer-text').fill('Keep partner archives for thirty days.');
    await page.locator('#brief-source-confirm').check();
    const fromLink = page.waitForResponse(r => r.url().endsWith('/api/brief/answer') && r.request().method() === 'POST');
    await page.locator('#answer-form button[type="submit"]').click();
    const fromLinkResponse = await fromLink;
    assert.equal(fromLinkResponse.status(), 200, await fromLinkResponse.text());
    assert.equal((await read()).authorized, true);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));

    assert.deepEqual(errors,[]);
    const artifacts = process.env.BRIDGE_TEST_ARTIFACTS || path.join(root,'test-results');
    fs.mkdirSync(artifacts,{recursive:true});
    await page.screenshot({path:path.join(artifacts,'context-revalidated-owner.png'),fullPage:true});
    console.log(JSON.stringify({ok:true,checks:['complete current source display','close is not approval','deliberate bound revalidation','retains source dependence','source race refused','reopen and revalidate current head','human-only independent owner replacement','pure-human complete scope display','exact human version rebound','accountless full source review on phone'],decision_id:fixture.decision_id}));
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGTERM');
      await once(child,'exit').catch(()=>{});
    }
    fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
