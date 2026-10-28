/* Chromium + real local API. Synthetic fixture, no external model/meeting calls. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process'),readline=require('node:readline');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
  const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'raven-objectives-browser-'));
  const child=spawn(process.env.BRIDGE_PYTHON||'python3',[path.join(root,'tests/objectives_browser_server.py'),path.join(temp,'ui.db')],{cwd:root});
  let stderr='',browser;child.stderr.on('data',chunk=>stderr+=chunk);
  try {
    const fixture=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Fixture timeout: '+stderr)),30000);const lines=readline.createInterface({input:child.stdout});lines.on('line',line=>{try{const result=JSON.parse(line);clearTimeout(timer);resolve(result);}catch{}});child.once('exit',code=>{clearTimeout(timer);reject(Error('Fixture exited '+code+': '+stderr));});});
    const base=fixture.url.split('/#')[0];
    browser=await chromium.launch({headless:true,...(process.env.BRIDGE_BROWSER?{executablePath:process.env.BRIDGE_BROWSER}:{})});
    const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
    page.on('pageerror',error=>errors.push(error.message));page.setDefaultTimeout(15000);
    const artifacts=process.env.BRIDGE_TEST_ARTIFACTS||path.join(root,'test-results');fs.mkdirSync(artifacts,{recursive:true});
    await page.goto(fixture.url);await page.getByRole('heading',{name:'Outcomes',exact:true}).waitFor();
    await page.getByRole('button',{name:'Review and accept',exact:true}).waitFor();
    assert.equal(await page.locator('#page-actions').getByRole('button',{name:'Track an outcome',exact:true}).count(),1);
    await page.getByRole('link',{name:'Make the billing release dependable',exact:true}).first().waitFor();
    const proposalCard=page.locator('[data-objective-id="'+fixture.proposal_id+'"]');
    await proposalCard.getByText('Why this came up · 1 source',{exact:true}).click();
    await proposalCard.getByText('Synthetic onboarding call',{exact:true}).waitFor();
    assert.equal(await proposalCard.locator('blockquote').innerText(),'Synthetic customer asks which trial limit applies.');
    assert.match(await proposalCard.innerText(),/Interpretation tradeoff/);assert.doesNotMatch(await proposalCard.innerText(),/interpretation_tradeoff/);
    const sourceLink=proposalCard.getByRole('link',{name:/Open recorded source/});
    const href=await sourceLink.getAttribute('href');assert.match(href,/^\/api\/records\?record_id=/);
    const recorded=await (await fetch(base+href)).json();assert.equal(recorded.source.title,'Synthetic onboarding call');
    await page.screenshot({path:path.join(artifacts,'objectives-api-desktop.png'),fullPage:true});
    await page.getByRole('button',{name:'Review and accept',exact:true}).click();
    await page.getByRole('button',{name:'Cancel',exact:true}).click();
    let overview=await (await fetch(base+'/api/objectives')).json();assert.equal(overview.proposals[0].status,'proposed');
    await page.getByRole('button',{name:'Review and accept',exact:true}).click();
    await page.getByRole('button',{name:'Send to the inbox',exact:true}).click();
    await page.getByRole('link',{name:'Open judgment review'}).waitFor();
    overview=await (await fetch(base+'/api/objectives')).json();assert.equal(overview.proposals[0].status,'review');assert.equal(overview.objectives.length,1);
    assert.match(await page.locator('[data-group="progress"]').innerText(),/Awaiting signed review · Fixture operator/i);
    // One step further: the owner signs the scope in the inbox through the UI and
    // the objective worker turns the proposal into an accepted objective.
    await page.goto(base+'/#inbox');await page.getByRole('button',{name:/All open/}).click();
    const scopeCard=page.locator('.decision-card').filter({hasText:'Should Raven take point on: Close the onboarding question from the call?'});
    await scopeCard.getByRole('button',{name:'Review and accept',exact:true}).click();
    await page.locator('#modal [data-action="signoff"]').click();
    await page.locator('#modal [data-action="signoff"]').waitFor({state:'detached'});
    const accepted=await (async()=>{for(let attempt=0;attempt<40;attempt++){const current=await (await fetch(base+'/api/objectives')).json();const signed=current.objectives.flatMap(item=>item.proposals||[]).find(item=>item.id===fixture.proposal_id);if(signed&&signed.status==='accepted')return current;await new Promise(resolve=>setTimeout(resolve,500));}throw Error('The signed proposal was not accepted by the objective worker');})();
    assert.equal(accepted.objectives.length,2);assert.equal(accepted.proposals.length,0);
    await page.goto(fixture.url);await page.reload();await page.getByRole('heading',{name:'Outcomes',exact:true}).waitFor();
    await page.getByRole('link',{name:'Close the onboarding question from the call',exact:true}).first().waitFor();
    await page.getByRole('link',{name:'Make the billing release dependable',exact:true}).first().click();
    await page.getByRole('button',{name:'Save checkpoint',exact:true}).click();
    await page.locator('[name=summary]').fill('HTTP and browser paths checked; awaiting policy.');
    await page.locator('[name=waiting_on]').fill('Policy reviewer');
    await page.locator('#objective-progress-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#app').textContent.includes('Policy reviewer'));
    const objective=await (await fetch(base+'/api/objectives/'+fixture.objective_id)).json();assert.equal(objective.waiting_on,'Policy reviewer');
    await page.getByRole('link',{name:'← Outcomes',exact:true}).click();await page.getByRole('heading',{name:'Outcomes',exact:true}).waitFor();
    await page.goBack();await page.getByRole('heading',{name:'Make the billing release dependable',exact:true}).waitFor();
    await page.goForward();await page.getByRole('heading',{name:'Outcomes',exact:true}).waitFor();
    // Meetings live on Settings, inside the collapsed 'Meetings' disclosure under Context (#meetings-mount).
    await page.goto(base+'/#connect');await page.getByRole('heading',{name:'Settings',exact:true}).waitFor();await page.waitForFunction(()=>typeof state!=='undefined'&&!!state.auth);
    const meetingsButton=page.getByRole('button',{name:'Manage meeting evidence'});
    await page.locator('#meetings-mount #objective-meetings-open').waitFor({state:'attached'});
    for(let attempt=0;attempt<20&&!(await meetingsButton.isVisible());attempt++){
      const meetings=page.locator('#settings-meetings');
      if(!(await meetings.evaluate(el=>el.open)))await meetings.locator(':scope > summary').click();
      await page.waitForTimeout(250);
    }
    await meetingsButton.click();await page.getByRole('heading',{name:'Synthetic onboarding call'}).waitFor();
    assert.match(await page.locator('.objective-editor').innerText(),/every workspace reader/i);
    const capture=page.locator('.objective-card').filter({has:page.getByRole('heading',{name:'Synthetic consent-only attendance plan',exact:true})});
    const openCaptureControls=async()=>{
      const controls=capture.locator('details').first();
      if(!await controls.evaluate(element=>element.open))await controls.locator('summary').click();
    };
    await openCaptureControls();
    const consent=capture.locator('.objective-meeting-consent');
    for(const name of ['notice_confirmed','join','transcribe'])await consent.locator(`[name=${name}]`).check();
    await Promise.all([page.waitForResponse(r=>r.url().endsWith('/consent')&&r.request().method()==='POST'),consent.getByRole('button',{name:'Record consent',exact:true}).click()]);
    await page.waitForFunction(id=>!objectivePending.has(id),fixture.attendance_id);
    // Rerender preserves disclosure expansion but clears transient consent attestations.
    await openCaptureControls();
    const background=capture.locator('.objective-meeting-background');
    await background.locator('[name=enabled]').check();await background.locator('[name=workspace_shared]').check();
    await Promise.all([page.waitForResponse(r=>r.url().endsWith('/background-sync')&&r.request().method()==='POST'),background.getByRole('button',{name:'Save background sync',exact:true}).click()]);
    await page.waitForFunction(id=>!objectivePending.has(id),fixture.attendance_id);
    let observed=await (await fetch(base+'/api/meetings/'+fixture.attendance_id+'?repo='+encodeURIComponent(fixture.repo))).json();
    assert.equal(observed.background_sync.enabled,1);assert.equal(observed.state,'planned');assert.equal(observed.consent.record,false);
    await openCaptureControls();
    await background.locator('[name=enabled]').uncheck();
    assert.equal(await background.locator('[name=workspace_shared]').isChecked(),false);
    await Promise.all([page.waitForResponse(r=>r.url().endsWith('/background-sync')&&r.request().method()==='POST'),background.getByRole('button',{name:'Save background sync',exact:true}).click()]);
    await page.waitForFunction(id=>!objectivePending.has(id),fixture.attendance_id);
    observed=await (await fetch(base+'/api/meetings/'+fixture.attendance_id+'?repo='+encodeURIComponent(fixture.repo))).json();
    assert.equal(observed.background_sync.enabled,0);assert.equal(observed.state,'planned');
    await page.getByRole('button',{name:'Close',exact:true}).click();assert.equal(await page.locator('.objective-editor').count(),0);
    await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    await page.screenshot({path:path.join(artifacts,'objectives-api-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);console.log('Outcomes real local API + Chromium flows passed; providers remain synthetic/unavailable.');
  } finally {if(browser)await browser.close();child.kill('SIGTERM');fs.rmSync(temp,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1;});
