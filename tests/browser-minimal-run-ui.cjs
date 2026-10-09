/* Fresh synthetic Chromium QA. Run explicitly; never part of unittest discovery.
 * Uses the real loopback HTTP/Store fixture plus explicit browser API failure doubles.
 * node tests/browser-minimal-run-ui.cjs
 * Optional: PLAYWRIGHT_MODULE, BRIDGE_BROWSER, BRIDGE_PYTHON, BRIDGE_TEST_ARTIFACTS. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process'),readline=require('node:readline'),{once}=require('node:events');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const synthetic=require('./minimal-run-ui-fixture.cjs');

const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
const jsonResponse=(route,status,data)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});

// These scenarios deliberately double API responses. They validate presentation,
// loading/access states and browser lifecycle only, never authority mechanisms.
async function responseCases(browser,fixture,screenshot,errors) {
  async function scenario(options={},body) {
    const page=await browser.newPage({viewport:{width:1280,height:900}});page.setDefaultTimeout(10000);
    page.on('pageerror',error=>errors.push(error.message));
    const control={stateStatus:200,treeStatus:200,stateGate:null,treeGate:null,startGate:null,nodeGate:null,
      startCalls:[],nodeCalls:[],nodeResponses:[],stateData:synthetic.state(),tree:synthetic.tree('synthetic-response-run'),trace:synthetic.trace(),...options};
    await page.route('**/*',async route=>{
      const request=route.request(),url=new URL(request.url()),endpoint=url.pathname;
      if(url.origin!==fixture.url)return route.abort();
      if(endpoint==='/api/state') {
        if(control.stateGate)await control.stateGate.promise;
        return jsonResponse(route,control.stateStatus,control.stateStatus===200?control.stateData:{error:'Synthetic workspace failure '+control.stateStatus});
      }
      if(/\/api\/tasks\/[^/]+\/tree$/.test(endpoint)) {
        if(control.treeGate)await control.treeGate.promise;
        return jsonResponse(route,control.treeStatus,control.treeStatus===200?{...control.tree,task_id:decodeURIComponent(endpoint.split('/')[3])}:{error:'Synthetic initial tree failure'});
      }
      if(/\/api\/tasks\/[^/]+\/trace$/.test(endpoint))return jsonResponse(route,200,control.trace);
      if(endpoint==='/api/tasks/start'&&request.method()==='POST') {
        control.startCalls.push(request.postDataJSON());if(control.startGate)await control.startGate.promise;
        return jsonResponse(route,200,{task_id:'synthetic-created-response-run'});
      }
      if(/\/api\/tasks\/[^/]+\/nodes$/.test(endpoint)&&request.method()==='POST') {
        control.nodeCalls.push(request.postDataJSON());if(control.nodeGate)await control.nodeGate.promise;
        const response=control.nodeResponses.shift();return jsonResponse(route,response?.status||200,response?.body||{node_id:'synthetic-created-node'});
      }
      return route.continue();
    });
    try {await page.goto(fixture.url+'/#runs/'+control.tree.task_id);await body(page,control);}
    finally {for(const key of ['stateGate','treeGate','startGate','nodeGate'])control[key]?.resolve();await page.close();}
  }
  await scenario({tree:synthetic.tree('synthetic-empty',{nodes:[],notes:[],counts:{blocking:0,total:0},next:''})},async page=>{
    await page.locator('.task-summary').waitFor();assert.equal(await page.locator('details.task-question').count(),0);
    assert.match(await page.locator('#app').innerText(),/No questions or findings recorded yet/);
    assert.match(await page.locator('.task-summary').innerText(),/0 blocker/);await screenshot('synthetic-empty',page);
  });
  await scenario({treeStatus:503},async page=>{
    await page.getByRole('heading',{name:'Could not open this task'}).waitFor();
    assert.match(await page.locator('#app').innerText(),/Synthetic initial tree failure/);
    assert.equal(await page.locator('details.task-question').count(),0);await screenshot('synthetic-initial-tree-error',page);
  });
  await scenario({stateGate:deferred(),treeGate:deferred()},async(page,control)=>{
    await page.getByRole('status').filter({hasText:/Loading workspace/}).waitFor();
    assert.equal(await page.locator('#note-form').count(),0);assert.equal(await page.locator('details.task-question').count(),0);
    await screenshot('synthetic-initial-loading',page);
    control.stateGate.resolve();await page.waitForFunction(()=>!!state.auth);
    assert.equal(await page.locator('details.task-question').count(),0,'tree remains pending after access is read');
    control.treeGate.resolve();await page.locator('details.task-question').first().waitFor();
  });
  await scenario({stateData:synthetic.state({auth:{enabled:true},me:{name:'Synthetic Viewer',role:'viewer'}})},async page=>{
    await page.locator('.task-summary').waitFor();assert.equal(await page.locator('#note-form').count(),0);
    assert.match(await page.locator('#app').innerText(),/read-only access/);
  });
  await scenario({tree:synthetic.tree('synthetic-observed-source',{nodes:[synthetic.node('observed',{
    source:'record',sources:[{ref:'Synthetic source',current:true,url:'javascript:bad()'}]})]})},async page=>{
    await page.locator('.task-question > summary').waitFor();await page.locator('.task-question > summary').click();
    await page.locator('.task-evidence > summary').click();
    assert.match(await page.locator('.task-evidence').innerText(),/Current observed version/);
    assert.match(await page.locator('.task-evidence').innerText(),/not live verification/);
    assert.equal(await page.locator('a[href^="javascript:"]').count(),0);
  });
  await scenario({tree:synthetic.tree('synthetic-stale',{nodes:[synthetic.node('stale-answer',{
    source:'memory',answer:'A historical seven-day exception.',authorized:true,signoff:'signed',needs_review:true,
    historical_signatures:['Synthetic Prior Reviewer'],review_reason:'The source changed after the earlier signature.',
    sources:[{record_id:'synthetic-old-policy',ref:'Earlier policy',current:false,stale:true}]})]})},async page=>{
    await page.locator('details.task-question').first().waitFor();
    assert.match(await page.locator('.task-question > summary').innerText(),/Approval needs review/);
    assert.match(await page.locator('.task-summary').innerText(),/0 authorized answers/);
    await page.locator('.task-question > summary').click();
    assert.match(await page.locator('.task-authority').innerText(),/does not authorize use/);
    await page.locator('.task-evidence > summary').click();
    assert.match(await page.locator('.task-evidence').innerText(),/Historical or unavailable version/);
    await screenshot('synthetic-stale-history',page);
  });
  for(const status of [401,403,503])await scenario({},async(page,control)=>{
    await page.locator('details.task-question').first().waitFor();
    await page.evaluate(()=>openModal('Synthetic current dialog','','Temporary UI content'));
    control.stateStatus=status;await page.evaluate(()=>refresh({quiet:true}));
    if(status===503) {
      assert.equal(await page.locator('details.task-question').count(),1);
      assert.match(await page.locator('#app').innerText(),/last successful read/);
      assert.equal(await page.locator('#modal').evaluate(el=>el.open),true);
      await page.evaluate(()=>closeModal());
    } else {
      assert.equal(await page.locator('details.task-question').count(),0);
      assert.equal(await page.locator('#note-form').count(),0);
      assert.equal(await page.locator('#modal').evaluate(el=>el.open),false);
      assert.match(await page.locator('#app').innerText(),/Workspace access unavailable/);
    }
    assert.match(await page.locator('#app').innerText(),new RegExp('Synthetic workspace failure '+status));
    await screenshot('synthetic-workspace-'+status,page);
  });
  await scenario({startGate:deferred()},async(page,control)=>{
    await page.locator('.task-summary').waitFor();await page.evaluate(()=>newRequest());
    await page.locator('#question').fill('Synthetic keyboard and duplicate-submit question');
    await page.locator('#repo').selectOption('synthetic/minimal-ui');
    const started=page.waitForRequest('**/api/tasks/start');
    await page.locator('#request-form [type="submit"]').focus();await page.keyboard.press('Enter');await started;
    await page.waitForFunction(()=>requestDraft.pending);
    await page.evaluate(()=>{const form=document.querySelector('#request-form');form.requestSubmit();form.requestSubmit();});
    assert.equal(control.startCalls.length,1);assert.equal(control.nodeCalls.length,0);
    assert.equal(await page.locator('#request-form [type="submit"]').isDisabled(),true);
    control.startGate.resolve();await page.waitForFunction(()=>location.hash==='#runs/synthetic-created-response-run');
    assert.equal(control.startCalls.length,1);assert.equal(control.nodeCalls.length,1);
  });
  await scenario({nodeResponses:[{status:200,body:{status:'needs_scope_clarification',scope_clarifications:[{
    missing_keys:['customer','__proto__'],source_facts:{customer:'HISTORICAL VALUE MUST NOT BE COPIED'}}]}},
    {status:503,body:{error:'Synthetic scope retry lost response'}},{status:200,body:{node_id:'scoped-created-node'}}]},async(page,control)=>{
    await page.locator('.task-summary').waitFor();await page.evaluate(()=>newRequest());
    await page.locator('#question').fill('Synthetic current customer scope question');await page.locator('#repo').selectOption('synthetic/minimal-ui');
    await page.locator('#request-form [type="submit"]').click();await page.locator('#request-scope').waitFor();
    assert.equal(await page.evaluate(()=>requestDraft.complete),false);assert.equal(await page.evaluate(()=>location.hash),'#runs/synthetic-response-run');
    for(const key of ['customer','__proto__'])assert.equal(await page.locator('[data-fact-key="'+key+'"]').inputValue(),'');
    assert.doesNotMatch(await page.locator('#request-scope').innerText(),/HISTORICAL VALUE/);
    assert.equal(await page.locator('#request-run-link').getAttribute('href'),'#runs/synthetic-created-response-run');
    await page.locator('[data-fact-key="customer"]').fill('Current Cedar');await page.locator('[data-fact-key="__proto__"]').fill('Explicit current scope');
    await page.locator('#request-form [type="submit"]').click();await page.locator('#request-form #form-error:not([hidden])').waitFor();
    assert.match(await page.locator('#request-form #form-error').innerText(),/Synthetic scope retry lost response/);
    assert.equal(await page.locator('[data-fact-key="customer"]').getAttribute('readonly'),'');
    await screenshot('synthetic-scope-retry',page);
    await page.evaluate(()=>{document.querySelector('[data-fact-key="customer"]').value='Changed after uncertainty';});
    await page.locator('#request-form [type="submit"]').click();
    await page.waitForFunction(()=>location.hash==='#runs/synthetic-created-response-run');
    assert.equal(control.startCalls.length,1);assert.equal(control.nodeCalls.length,3);
    assert.equal(control.nodeCalls[0].client_ref,control.nodeCalls[1].client_ref);assert.deepEqual(control.nodeCalls[1],control.nodeCalls[2]);
    assert.equal(control.nodeCalls[2].facts.customer,'Current Cedar');assert.equal(Object.hasOwn(control.nodeCalls[2].facts,'__proto__'),true);
  });
  for(const interrupt of ['newer-dialog','navigation-away-back'])await scenario({nodeGate:deferred()},async(page,control)=>{
    await page.locator('.task-summary').waitFor();await page.evaluate(()=>newRequest());
    await page.locator('#question').fill('Synthetic late response question');await page.locator('#repo').selectOption('synthetic/minimal-ui');
    const adding=page.waitForRequest('**/api/tasks/*/nodes');await page.locator('#request-form [type="submit"]').click();await adding;
    if(interrupt==='newer-dialog')await page.evaluate(()=>{closeModal();openModal('Newer synthetic dialog','','Keep this newer dialog open');});
    else {
      await page.evaluate(()=>location.hash='runs/synthetic-newer-run');await page.waitForFunction(()=>taskDetail?.tree.task_id==='synthetic-newer-run');
      await page.goBack();await page.waitForFunction(()=>location.hash==='#runs/synthetic-response-run');
    }
    const hash=await page.evaluate(()=>location.hash);control.nodeGate.resolve();
    await page.waitForFunction(()=>requestDraft?.complete===true);
    assert.equal(await page.evaluate(()=>location.hash),hash);
    assert.equal(await page.locator('#modal').evaluate(el=>el.open),true);
    if(interrupt==='newer-dialog')assert.match(await page.locator('#modal-content').innerText(),/Keep this newer dialog open/);
    assert.equal(control.startCalls.length,1);assert.equal(control.nodeCalls.length,1);
  });
  const design=synthetic.design(fixture.earlier_decision_id);
  await scenario({tree:design.tree,trace:design.trace},async page=>{
    await page.locator('.task-summary').waitFor();
    assert.equal(await page.locator('.task-question:not(.task-scope)').count(),4);
    assert.equal(await page.locator('.task-scope').count(),1);
    const summaries=await page.locator('.task-question:not(.task-scope) > summary').allInnerTexts();
    assert(summaries.some(text=>/Source record/.test(text)&&/Not authorized/.test(text)));
    assert(summaries.some(text=>/Earlier decision/.test(text)&&/Not authorized/.test(text)));
    assert(summaries.some(text=>/Recorded human answer/.test(text)&&/Human sign-off recorded/.test(text)));
    await screenshot('design-review-desktop',page);await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Design-review mobile overflow');
    await screenshot('design-review-mobile',page);await page.setViewportSize({width:1280,height:900});
    await page.locator('#finding-earlier-finding > summary').click();
    await page.locator('#finding-earlier-finding-evidence > summary').click();
    assert.equal(await page.locator('#finding-earlier-finding-earlier-decision').getAttribute('data-action'),'source');
    assert.equal(await page.locator('#finding-earlier-finding-earlier-decision').getAttribute('data-id'),fixture.earlier_decision_id);
    await page.locator('#finding-earlier-finding-related-0 > summary').click();
    assert.match(await page.locator('#finding-earlier-finding-evidence').innerText(),/synthetic-earlier-version-2/);
    await screenshot('design-review-evidence',page);
    await page.locator('#finding-earlier-finding-earlier-decision').click();
    await page.locator('#modal[open]').waitFor();
    // The local operator sees the recorded answer in the existing correction
    // form. A textarea's value is not part of its containing element's innerText.
    const earlierAnswer=page.locator('#modal[open] #answer-form textarea[name="answer"]');
    await earlierAnswer.waitFor();
    assert.equal(await page.locator('#modal #answer-form').getAttribute('data-id'),fixture.earlier_decision_id);
    assert.equal(await earlierAnswer.inputValue(),'Support copies may be retained for seven days within the earlier preview scope.');
    await screenshot('design-review-source-decision',page);
    await page.getByRole('button',{name:'Close dialog',exact:true}).click();
    await page.locator('#finding-earlier-finding-evidence > summary').click();
    await page.locator('#finding-earlier-finding-contact > summary').click();
    assert.match(await page.locator('#finding-earlier-finding-contact').innerText(),/Synthetic Policy Reviewer/);
    assert.match(await page.locator('#finding-earlier-finding-contact').innerText(),/Owns export-copy retention decisions/);
    await screenshot('design-review-contact',page);
    await page.locator('#task-tab-history').click();
    assert.match(await page.locator('.task-timeline').innerText(),/Decision recorded/);
    assert.match(await page.locator('.task-timeline').innerText(),/Synthetic Export Reviewer/);
    assert.equal(await page.locator('.task-timeline > li').count(),2,'only supplied current-run events appear');
    for(const detail of await page.locator('.task-timeline details').all()) {
      if(!(await detail.evaluate(el=>el.open)))await detail.locator(':scope > summary').click();
    }
    await screenshot('design-review-history',page);
    await page.evaluate(()=>newRequest());
    await page.locator('#question').fill('May support keep a preview export copy for seven days?');
    await page.locator('#repo').selectOption('synthetic/minimal-ui');
    assert.equal(await page.locator('#request-context').getAttribute('open'),null);
    assert.match(await page.locator('#modal-content').innerText(),/does not launch an external coding agent/);
    await screenshot('design-review-ask-desktop',page);
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Design-review Ask mobile overflow');
    await screenshot('design-review-ask-mobile',page);
    await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  });
}
(async()=>{
  const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'raven-minimal-run-'));
  const child=spawn(process.env.BRIDGE_PYTHON||'python3',[path.join(__dirname,'minimal_run_browser_server.py'),path.join(temp,'qa.db')],{cwd:root});
  let stderr='',browser;child.stderr.on('data',chunk=>stderr+=chunk);
  try {
    const lines=readline.createInterface({input:child.stdout});
    const fixture=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error('Fixture startup timeout: '+stderr)),30000);
      lines.on('line',line=>{try{const value=JSON.parse(line);clearTimeout(timer);resolve(value);}catch{}});
      child.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+': '+stderr));});
    });
    browser=await chromium.launch({headless:true,...(process.env.BRIDGE_BROWSER?{executablePath:process.env.BRIDGE_BROWSER}:{})});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(10000);
    await page.route('**/*',route=>new URL(route.request().url()).origin===fixture.url?route.continue():route.abort());
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    const artifacts=process.env.BRIDGE_TEST_ARTIFACTS||path.join(root,'test-results');fs.mkdirSync(artifacts,{recursive:true});
    const screenshots=[];const screenshot=async(name,target=page)=>{const file='fresh-minimal-run-'+name+'.png';
      await target.evaluate(async()=>{window.scrollTo(0,0);document.querySelectorAll('dialog,.modal-body').forEach(el=>el.scrollTo(0,0));
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));});
      await target.screenshot({path:path.join(artifacts,file),fullPage:true});screenshots.push(file);};
    const [a,b]=fixture.tasks;
    await page.goto(fixture.url+'/#runs/'+a.task_id);await page.locator('details.task-question').first().waitFor();
    assert.equal(await page.locator('details.task-question').count(),12);
    assert.equal(await page.locator('details.task-question[open]').count(),0);
    assert.match(await page.locator('.task-summary').innerText(),/28 blocker/);
    assert.equal(await page.locator('.task-note').count(),12);
    await page.locator('#task-more-finding').click();assert.equal(await page.locator('details.task-question').count(),24);
    const question=page.locator('details.task-question').first();await question.locator(':scope > summary').click();
    await question.locator('.task-contact > summary').click();
    assert.match(await question.innerText(),/Recorded synthetic CODEOWNERS/);
    assert.match(await question.innerText(),/Synthetic recorded referral/);
    await page.locator('#task-note-compose > summary').click();await page.locator('#note-text').fill('Unsubmitted run A draft');
    await page.locator('#note-text').evaluate(el=>{el.focus();el.setSelectionRange(3,8);});
    await page.evaluate(()=>loadTask({quiet:true}));
    assert.equal(await page.locator('#note-text').inputValue(),'Unsubmitted run A draft');
    assert.deepEqual(await page.locator('#note-text').evaluate(el=>[document.activeElement===el,el.selectionStart,el.selectionEnd]),[true,3,8]);
    assert.equal(await page.locator('details.task-question[open]').count(),1);
    await screenshot('desktop-draft');
    await page.evaluate(id=>location.hash='runs/'+id,b.task_id);await page.waitForFunction(id=>taskDetail?.tree.task_id===id,b.task_id);
    await page.locator('#task-note-compose > summary').click();assert.equal(await page.locator('#note-text').inputValue(),'');
    await page.locator('#note-text').fill('Unsubmitted run B draft');
    await page.goBack();await page.waitForFunction(id=>taskDetail?.tree.task_id===id,a.task_id);
    assert.equal(await page.locator('#note-text').inputValue(),'Unsubmitted run A draft');
    assert.equal(await page.locator('details.task-question').count(),24);
    assert.equal(await page.locator('details.task-question[open]').count(),1);
    await page.locator('#task-tab-history').click();assert.equal(await page.locator('.task-timeline > li').count(),25);
    await page.locator('#task-more-history').click();assert((await page.locator('.task-timeline > li').count())>25);
    await page.locator('#task-tab-overview').click();
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile horizontal overflow');
    await screenshot('mobile-overview');
    await page.evaluate(()=>refresh());
    // Real start endpoint, deliberately failed first node response, then real node endpoint retry.
    await page.setViewportSize({width:1280,height:900});await page.evaluate(()=>newRequest());
    assert.equal(await page.locator('#request-context').getAttribute('open'),null);
    const prompt='🪶'.repeat(310)+' Check a new synthetic policy question.';
    await page.locator('#question').fill(prompt);await page.locator('#repo').selectOption(fixture.repo);
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile Ask overflow');
    await screenshot('ask-mobile');await page.setViewportSize({width:1280,height:900});
    let failNode=true;const starts=[],nodes=[];
    page.on('request',request=>{if(request.method()==='POST'){
      if(new URL(request.url()).pathname==='/api/tasks/start')starts.push(request.postDataJSON());
      if(/\/api\/tasks\/[^/]+\/nodes$/.test(new URL(request.url()).pathname))nodes.push(request.postDataJSON());}});
    await page.route('**/api/tasks/*/nodes',route=>{if(failNode){failNode=false;return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Synthetic first-node failure'})});}return route.continue();});
    await page.locator('#request-form [type="submit"]').click();await page.locator('#request-form #form-error:not([hidden])').waitFor();
    assert.match(await page.locator('#request-form #form-error').innerText(),/Synthetic first-node failure/);
    const created=await page.locator('#request-form').getAttribute('data-task-id');assert(created);
    assert.equal(await page.locator('#request-run-link').getAttribute('href'),'#runs/'+created);
    assert.equal(await page.locator('#question').inputValue(),prompt);
    await page.locator('#request-form [type="submit"]').click();await page.waitForFunction(id=>location.hash==='#runs/'+id,created);
    assert.equal(starts.length,1);assert.equal(starts[0].title,Array.from(prompt).slice(0,300).join(''));
    assert.equal(starts[0].goal,prompt);assert.equal(starts[0].agent,'Raven UI');assert(!('requester' in starts[0]));
    assert.equal(nodes.length,2);assert.deepEqual(nodes[0],nodes[1]);assert(nodes[0].client_ref);
    await page.locator('details.task-question').first().waitFor();await screenshot('ask-retry');
    await responseCases(browser,fixture,screenshot,errors);
    assert.deepEqual(errors,[]);
    const result={ok:true,fixture:fixture.fixture,browser:'Chromium',screenshots,
      checks:['bounded questions/history/notes','contact evidence and referral trace','polling draft/disclosure/cursor',
        'per-run drafts through Back navigation','mobile overflow','source observations and unsafe links',
        'unknown and read-only access','real Ask start/node retry with Unicode and stable keys',
        'synthetic API doubles: empty, initial loading and tree error, stale historical answer',
        'synthetic API doubles: workspace 401/403 revocation versus transient503',
        'browser keyboard Enter and duplicate requestSubmit while POST is pending',
        'late-dialog and navigation-away-back response guards','four-question synthetic design review desktop/mobile',
        'exact historical answer textarea value and source decision identity',
        'expanded evidence pins, contact, history and final Ask desktop/mobile captures'],
      limits:['Synthetic provider-free fixture only','API-response doubles test presentation and lifecycle, not authority mechanisms',
        'The four-question design-review scene is synthetic API data; its human response has an explicit supplied current-run event']};
    fs.writeFileSync(path.join(artifacts,'fresh-minimal-run-browser-result.json'),JSON.stringify(result,null,2)+'\n');
    console.log(JSON.stringify(result));
  } finally {
    if(browser)await browser.close();
    if(child.exitCode===null&&child.signalCode===null){child.kill('SIGTERM');await once(child,'exit').catch(()=>{});}
    fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
