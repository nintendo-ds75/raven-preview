/* Fresh source snapshots in the real web UI; only a local synthetic server. */
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
  const root = path.resolve(__dirname,'..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(),'raven-agent-source-browser-'));
  const child = spawn(process.env.BRIDGE_PYTHON || 'python3',[path.join(root,'tests/agent_source_browser_server.py'),path.join(temp,'sources.db')],{cwd:root});
  let stderr='',browser;
  child.stderr.on('data',data=>{stderr+=data;});
  try {
    const lines = readline.createInterface({input:child.stdout});
    const fixture = await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error('Startup timeout: '+stderr)),30000);
      lines.on('line',line=>{try {const value=JSON.parse(line);clearTimeout(timer);resolve(value);}catch {}});
      child.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+': '+stderr));});
    });
    const state=await (await fetch(fixture.url+'/api/state')).json();
    const read=async id=>(await fetch(fixture.url+'/api/decisions/'+id)).json();
    const update=async (record,body)=>{
      const response=await fetch(fixture.url+'/api/records',{method:'POST',headers:{'Content-Type':'application/json','X-Bridge-CSRF':state.csrf_token},body:JSON.stringify({...record.data,body})});
      assert.equal(response.status,200);return response.json();
    };
    browser=await chromium.launch({headless:true,...(process.env.BRIDGE_BROWSER?{executablePath:process.env.BRIDGE_BROWSER}:{})});
    const page=await browser.newPage({viewport:{width:1280,height:900}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(fixture.url);
    await page.waitForFunction(()=>typeof review==='function' && state?.decisions?.length);
    const open=async id=>{await page.evaluate(id=>review(id),id);await page.locator('#modal[open]').waitFor();};
    const sign=async (id,status=200)=>{
      const pending=page.waitForResponse(r=>r.url().endsWith('/api/decisions/'+id+'/signoff')&&r.request().method()==='POST');
      await page.locator('[data-action="signoff"]').click();
      const response=await pending;
      assert.equal(response.status(),status,await response.text());
      return response.request().postDataJSON();
    };
    const artifacts=process.env.BRIDGE_TEST_ARTIFACTS || path.join(root,'test-results');
    fs.mkdirSync(artifacts,{recursive:true});
    await open(fixture.support);
    const initial=await read(fixture.support);
    assert.equal(initial.needs_review,0);
    assert.equal(initial.authorized,false);
    assert.equal(await page.locator('#source-review-signoff').count(),0);
    assert.equal(await page.locator('.source-evidence-review .source-snapshot').count(),2);
    for (const record of fixture.records.slice(0,2)) {
      const block=page.locator('.source-evidence-review [data-source-record="'+record.source.record_id+'"]');
      assert.equal(await block.locator('.source-snapshot-body').innerText(),record.data.body);
      for (const value of [record.source.record_id,record.source.source_version_id,'support']) assert((await block.innerText()).includes(value),value);
      await block.getByText('Complete source metadata',{exact:true}).click();
      assert((await block.innerText()).includes('fixture.example'));
      assert((await block.innerText()).includes(record.data.url));
    }
    assert((await page.locator('#modal').innerText()).includes('SCOPE-CUSTOMER'));
    assert((await page.locator('#modal').innerText()).includes('policy/export.py'));
    assert.equal(await page.locator('.source-evidence-review img').count(),0);
    assert.equal(await page.evaluate(()=>Boolean(window.sourceExecuted)),false);
    await page.screenshot({path:path.join(artifacts,'fresh-agent-sources-desktop.png'),fullPage:true});
    await page.getByRole('button',{name:'Close',exact:true}).last().click();
    assert.equal((await read(fixture.support)).authorized,false);
    await open(fixture.support);
    assert.equal(await page.locator('.source-evidence-review .source-snapshot').count(),2);
    const ordinary=await sign(fixture.support);
    assert.deepEqual(Object.keys(ordinary).sort(),['by','expected_updated_at']);
    const signed=await read(fixture.support);
    assert.equal(signed.authorized,true);
    assert.equal(signed.source,'agent');
    assert.equal(signed.sources.length,2);

    // Informational context remains visible, retained, and nonblocking even
    // when a newer source observation exists. No fake needs_review or checkbox.
    await update(fixture.records[2],'New context observation; the earlier snapshot is retained.');
    await open(fixture.context);
    const contextual=await read(fixture.context);
    assert.equal(contextual.needs_review,0);
    assert.equal(await page.locator('#source-review-signoff').count(),0);
    assert.equal(await page.locator('.source-snapshot-body').innerText(),fixture.records[2].data.body);
    assert(!(await page.locator('.source-evidence-review').innerText()).includes('revalidation needed'));

    // The accountless phone review also displays exact IDs, full text, and
    // metadata for context-only pins without introducing a blocking checkbox.
    const phone=await browser.newPage({viewport:{width:390,height:844}});
    phone.on('pageerror',error=>errors.push(error.message));
    await phone.goto(fixture.url+'/brief#'+fixture.links.context);
    await phone.locator('.brief-source-review').waitFor();
    assert((await phone.locator('.brief-source-review').innerText()).includes(fixture.records[2].source.record_id));
    assert((await phone.locator('.brief-source-review').innerText()).includes(fixture.records[2].source.source_version_id));
    assert.equal(await phone.locator('.brief-source-body').first().innerText(),fixture.records[2].data.body);
    assert.equal(await phone.locator('#brief-source-confirm').count(),0);
    assert(await phone.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await phone.screenshot({path:path.join(artifacts,'fresh-agent-context-phone.png'),fullPage:true});
    await sign(fixture.context);
    assert.equal((await read(fixture.context)).authorized,true);
    assert.equal((await read(fixture.context)).sources[0].source_version_id,fixture.records[2].source.source_version_id);

    // A source changed after fresh display still loses atomically at signoff.
    await open(fixture.race);
    assert.equal(await page.locator('#source-review-signoff').count(),0);
    await update(fixture.records[1],'Changed second source after the fresh web reading.');
    await sign(fixture.race,400);
    assert.equal((await read(fixture.race)).authorized,false);
    await open(fixture.race);
    await page.locator('#source-review-signoff').waitFor();
    assert((await page.locator('.source-revalidation').first().innerText()).includes('Changed second source'));
    await page.locator('#source-review-signoff').check();
    await sign(fixture.race);
    assert.equal((await read(fixture.race)).authorized,true);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,checks:['fresh complete snapshots and exact pins','literal escaped source text','full source metadata','close/reopen preserves unsigned proposal','ordinary signoff unchanged','context-only stale snapshot stays informational','phone context snapshot display','stale second source refused','existing explicit revalidation still works']}));
  } finally {
    if(browser) await browser.close();
    if(child.exitCode===null&&child.signalCode===null){child.kill('SIGTERM');await once(child,'exit').catch(()=>{});}
    fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
