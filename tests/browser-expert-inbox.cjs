'use strict';
// Synthetic identities, actual local HTTP and Chromium. No API response doubles or external traffic.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn,execFileSync}=require('node:child_process'),readline=require('node:readline'),{once}=require('node:events');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'raven-expert-world-'));
const artifacts=process.env.BRIDGE_TEST_ARTIFACTS||path.join(root,'test-results','expert-inbox');fs.mkdirSync(artifacts,{recursive:true});
const checks=[],failures=[],pageErrors=[];
let sourceCommit='unknown (archive checkout without Git metadata)', sourceTreeDirty=null;
try { sourceCommit=execFileSync('git',['-C',root,'rev-parse','--verify','HEAD'],{encoding:'utf8',stdio:['ignore','pipe','ignore']}).trim(); sourceTreeDirty=!!execFileSync('git',['-C',root,'status','--porcelain','--untracked-files=no'],{encoding:'utf8',stdio:['ignore','pipe','ignore']}).trim(); } catch {}
const check=async(name,fn)=>{try{await fn();checks.push(name);console.log('PASS '+name);}catch(e){failures.push({name,error:e.message});console.log('FAIL '+name+': '+e.message);}};
(async()=>{
 let browser;const child=spawn(process.env.BRIDGE_PYTHON||'python3',[path.join(root,'tests/expert_inbox_browser_server.py'),temp],{cwd:root});let stderr='';child.stderr.on('data',d=>stderr+=d);
 try {
  const world=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error(stderr||'Startup timed out')),20000);readline.createInterface({input:child.stdout}).on('line',s=>{try{const j=JSON.parse(s);clearTimeout(timer);resolve(j);}catch{}});child.once('exit',code=>{clearTimeout(timer);reject(Error('Fixture exited '+code+': '+stderr));});});
  const {A,B}=world,deep=A.base+'/#runs/'+A.task+'/'+A.node;
  const ctl=async(action,params={})=>(await fetch(world.control+'/?'+new URLSearchParams({action,...params}))).json();
  browser=await chromium.launch({...(process.env.BRIDGE_BROWSER?{executablePath:process.env.BRIDGE_BROWSER}:{}),headless:true,chromiumSandbox:true});
  const pages=[];async function page(){const p=await browser.newPage({viewport:{width:1365,height:1000}});p.setDefaultTimeout(8000);p.on('pageerror',e=>pageErrors.push(e.message));await p.route('**/*',r=>[A.base,B.base].includes(new URL(r.request().url()).origin)?r.continue():r.abort());pages.push(p);return p;}
  const api=async(p,url,data)=>p.evaluate(async({url,data})=>{const r=await fetch(url,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-Bridge-CSRF':state.csrf_token}:{},body:data?JSON.stringify(data):undefined});return {status:r.status,body:await r.json()};},{url,data});
  const login=async(p,who='expert')=>{await p.goto(A.base+'/auth/login');await p.locator('input[name=email]').fill(A[who].email);await p.locator('input[name=password]').fill(A.password);await p.locator('form[action="/auth/password"] button').click();await p.waitForURL(u=>u.pathname==='/');await p.waitForFunction(()=>state?.me);};
  const visitor=await page();
  await check('Signed-out notification link opens login without private task data',async()=>{await visitor.goto(deep);await visitor.waitForURL(/\/auth\/login/);assert.equal(new URL(visitor.url()).hash,'#runs/'+A.task+'/'+A.node);assert(!((await visitor.locator('body').innerText()).includes('SYNTHETIC_TASK_CONTEXT')));for(const endpoint of ['/api/state','/api/tasks/'+A.task+'/tree','/api/decisions/'+A.node,'/api/records/'+A.restricted_node]){const r=await visitor.request.get(A.base+endpoint);assert.equal(r.status(),401);assert(!((await r.text()).includes('TENANT_A_PRIVATE_GOAL')));}});
  await check('Expired invitation fails without creating a signed-in session',async()=>{const p=await page();await p.goto(A.base+'/auth/join#invite='+A.expired);await p.locator('[name=name]').fill('Expired Synthetic');await p.locator('[name=password]').fill(A.password);await p.locator('button[type=submit]').click();await p.getByText(/invitation is invalid or expired/).waitFor();assert.equal((await p.request.get(A.base+'/api/state')).status(),401);await p.screenshot({path:path.join(artifacts,'expired-invite.png'),fullPage:true});});
  const expert=await page();
  await check('Invite maps preprovisioned expert to the existing identity and inbox',async()=>{await expert.goto(A.base+'/auth/join#invite='+A.invite);await expert.locator('[name=name]').fill('A different display name cannot replace identity');await expert.locator('[name=password]').fill(A.password);await expert.locator('button[type=submit]').click();await expert.waitForURL(u=>u.pathname==='/');await expert.waitForFunction(()=>state?.me);const me=await expert.evaluate(()=>state.me);assert.equal(me.id,A.expert.id);assert.equal(me.name,A.expert.name);assert.equal(me.role,'member');assert.equal(new URL(expert.url()).hash,'#inbox');assert((await expert.locator('body').innerText()).includes('Keep synthetic export copies'));await expert.screenshot({path:path.join(artifacts,'expert-inbox.png'),fullPage:true});});
  await check('Returning password login restores the exact task and decision deep link',async()=>{await visitor.locator('input[name=email]').fill(A.expert.email);await visitor.locator('input[name=password]').fill(A.password);await visitor.locator('form[action="/auth/password"] button').click();await visitor.waitForURL(deep);await visitor.locator('#modal[open]').waitFor();assert((await visitor.locator('#modal').innerText()).includes('SYNTHETIC_TASK_CONTEXT'));assert(await visitor.getByRole('button',{name:/Sign as Synthetic Expert/}).isVisible());await visitor.screenshot({path:path.join(artifacts,'expert-deep-link.png'),fullPage:true});});
  await check('Decision inquiry reads only current task evidence without changing authority',async()=>{
    const before=await ctl('decision'),counts=await ctl('counts');
    // Ask Raven is shown only when the server has a model to read with; this fixture has none,
    // so the same read-only inquiry goes through the API the dialog would call.
    const asked=await visitor.locator('#modal details.decision-inquiry').count();
    let inquiry;
    if(asked){
      await visitor.locator('#modal details.decision-inquiry > summary').click();
      await visitor.locator('#decision-inquiry-text').fill('What context and production constraints apply?');
      const response=visitor.waitForResponse(r=>r.url().endsWith('/api/tasks/'+A.task+'/inquiry')&&r.request().method()==='POST');
      await visitor.locator('#modal details.decision-inquiry button[type=submit]').click();
      const inquiryResponse=await response;assert.equal(inquiryResponse.status(),200);
      inquiry=await inquiryResponse.json();
    }else{
      assert.equal(await visitor.evaluate(()=>state.model?.investigation),false,'Ask Raven is absent only without a model');
      const r=await api(visitor,'/api/tasks/'+A.task+'/inquiry',{question:'What context and production constraints apply?',focus_id:A.node});
      assert.equal(r.status,200);inquiry=r.body;
      assert(JSON.stringify(inquiry).includes('SYNTHETIC_TASK_CONTEXT'));
    }
    if(asked){
    await visitor.locator('.decision-inquiry-result summary').filter({hasText:/^Evidence used$/}).click();
    await visitor.locator('.decision-inquiry-result').filter({hasText:/SYNTHETIC_TASK_CONTEXT/}).waitFor();
    const citations=Array.isArray(inquiry.citations)?inquiry.citations:[];
    if(citations.length){
      const disclosure=visitor.locator('#decision-inquiry-citations');
      assert.equal(await disclosure.getAttribute('open'),null,'quotes initially collapsed');
      await disclosure.locator('summary').click();
      assert.equal(await disclosure.locator('blockquote').count(),citations.length);
      const sourceIds=[...new Set(citations.map(citation=>citation.id))];
      assert.equal(await disclosure.locator('figure').count(),sourceIds.length);
      for(let i=0;i<sourceIds.length;i++){
        const id=sourceIds[i],source=inquiry.evidence.find(e=>e.id===id);
        assert(source,'actual response must identify the quoted source');
        const figure=disclosure.locator('figure').nth(i);
        assert.deepEqual(await figure.locator('blockquote').allTextContents(),citations.filter(citation=>citation.id===id).map(citation=>citation.quote));
        const identity=await figure.locator('figcaption').textContent();
        assert(identity.includes(source.title||source.id));assert(identity.includes('Source: '+source.id));
      }
      assert.equal(await disclosure.locator('a').count(),0);
    }else{
      assert.equal(await visitor.locator('#decision-inquiry-citations').count(),0);
      console.log('INFO Real inquiry response has no citations; exact-quote browser branch not exercised.');
    }
    }
    const text=asked?await visitor.locator('.decision-inquiry-result').innerText():JSON.stringify(inquiry);
    assert(!text.includes('UNRELATED_DECISION_CANARY'));assert(!text.includes('UNRELATED_TASK_CANARY'));
    const after=await ctl('decision');assert.equal(after.authorized,false);assert.equal(after.updated_at,before.updated_at);
    assert.deepEqual(await ctl('counts'),counts);
    await visitor.screenshot({path:path.join(artifacts,'expert-inquiry.png'),fullPage:true});
  });
  const peer=await page();await login(peer,'peer');
  await check('Another workspace member may read shared context but cannot impersonate expert signoff',async()=>{await peer.goto(deep);await peer.locator('#modal[open]').waitFor();assert.equal(await peer.locator('[data-action=signoff]').count(),0);const d=(await api(peer,'/api/decisions/'+A.node)).body;assert(!d.allowed_actions.includes('sign'));const r=await api(peer,'/api/decisions/'+A.node+'/signoff',{expected_updated_at:d.updated_at,signed_by:A.expert.name,by:A.expert.name});assert.equal(r.status,403);assert.equal((await ctl('decision')).authorized,false);});
  await check('Forged inquiry scope and signoff fields fail without widening retrieval',async()=>{for(const data of [{question:'Read other task',focus_id:A.other_node},{question:'Approve',signed_by:A.expert.name},{question:'Approve',signoff:true},{question:'Read tenant B',task_id:B.task}]){const r=await api(visitor,'/api/tasks/'+A.task+'/inquiry',data);assert.equal(r.status,400);assert(!JSON.stringify(r).includes('UNRELATED_DECISION_CANARY'));}});
  await check('Expert signs the reviewed answer as their authenticated stable identity',async()=>{const response=visitor.waitForResponse(r=>r.url().endsWith('/api/decisions/'+A.node+'/signoff')&&r.request().method()==='POST');await visitor.getByRole('button',{name:/Sign as Synthetic Expert/}).click();assert.equal((await response).status(),200);const d=await ctl('decision');assert.equal(d.authorized,true);assert.equal(d.signed_by,A.expert.name);const event=d.events.find(e=>e.kind==='signoff');assert(event);const detail=JSON.parse(event.detail);assert.equal(detail.actor_id,A.expert.id);assert.equal(detail.actor_kind,'session');});
  await check('Independent tenant instance rejects tenant A session and invitation',async()=>{const response=await expert.request.get(B.base+'/api/state');assert.equal(response.status(),401);assert(!((await response.text()).includes('TENANT_B_PRIVATE_GOAL')));const p=await page();await p.goto(B.base+'/auth/join#invite='+A.invite);await p.locator('[name=name]').fill('Cross tenant attempt');await p.locator('[name=password]').fill(A.password);await p.locator('button[type=submit]').click();await p.getByText(/invitation is invalid or expired/).waitFor();assert.equal((await p.request.get(B.base+'/api/state')).status(),401);});
  await check('Connected source revocation withholds copied task, inquiry and state content',async()=>{const current=await api(expert,'/api/decisions/'+A.restricted_node);assert(JSON.stringify(current).includes('REVOKED_SOURCE_CANARY'));await ctl('revoke_source');for(const endpoint of ['/api/state','/api/tasks/'+A.restricted_task+'/tree','/api/decisions/'+A.restricted_node]){const r=await api(expert,endpoint);assert(!JSON.stringify(r).includes('REVOKED_SOURCE_CANARY'));}const result=await api(expert,'/api/tasks/'+A.restricted_task+'/inquiry',{question:'What allowance applies?'});assert.equal(result.status,400);assert(!JSON.stringify(result).includes('REVOKED_SOURCE_CANARY'));});
  await check('Expired legacy link does not expose task context',async()=>{const r=await fetch(A.base+'/api/brief',{headers:{'X-Raven-Link':A.expired_legacy}});assert.equal(r.status,401);assert(!(await r.text()).includes('SYNTHETIC_TASK_CONTEXT'));});
  await check('Revoked identity loses existing browser and legacy-link access immediately',async()=>{await ctl('revoke_expert');for(const endpoint of ['/api/state','/api/tasks/'+A.task+'/tree','/api/decisions/'+A.node])assert.equal((await expert.request.get(A.base+endpoint)).status(),401);const inquiry=await api(visitor,'/api/tasks/'+A.task+'/inquiry',{question:'Read after identity revoked'});assert.equal(inquiry.status,401);const r=await fetch(A.base+'/api/brief',{headers:{'X-Raven-Link':A.legacy}});assert.equal(r.status,401);await visitor.evaluate(()=>refresh({quiet:true}));await visitor.getByText('Workspace access unavailable',{exact:true}).waitFor();assert.equal(await visitor.locator('#modal[open]').count(),0);assert(!((await visitor.locator('#app').innerText()).includes('SYNTHETIC_TASK_CONTEXT')));await visitor.screenshot({path:path.join(artifacts,'expert-revoked.png'),fullPage:true});});
  await check('No unexpected JavaScript page errors',async()=>assert.deepEqual(pageErrors,[]));
  const result={commit:sourceCommit,source_tree_dirty:sourceTreeDirty,ok:failures.length===0,checks,failures,limitations:['Synthetic identities and local loopback HTTP; no real GitHub OAuth, external accounts, provider inference or live notifications.','Isolation means separate Raven databases and signing secrets; members/viewers intentionally read their shared workspace.','Default authenticated notification links are stable routes, not expiring bearer links; invitation and explicit legacy bearer expiration are tested.']};
  fs.writeFileSync(path.join(artifacts,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));if(failures.length)process.exitCode=1;
 } finally {if(browser)await browser.close();if(child.exitCode===null){child.kill();await once(child,'exit').catch(()=>{});}fs.rmSync(temp,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
