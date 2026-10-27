/* Today UI contracts with DOM/API doubles; not live browser or provider tests. */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
function harness(hash='#today') {
  const handlers={}, requests=[];
  const app={innerHTML:'',dataset:{},querySelectorAll:()=>[],contains:()=>false};
  const c={console,URL,crypto:require('node:crypto').webcrypto,view:'today',location:{hash},
    state:{auth:{enabled:false},workbench:{hosts:[]}},
    document:{activeElement:{},getElementById:id=>id==='app'?app:null,addEventListener:(type,fn)=>(handlers[type]||=[]).push(fn)},
    esc:x=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')};
  c.fixture={objectives:[{id:'o1',title:'Ship safely',outcome:'Reduce failures',status:'active',revision:3,repository:'sample/repo',success_criteria:'Measured result',stop_criteria:'No production access',guardrails:'No publishing'}],proposals:[{id:'p1',title:'<img onerror=bad()>',status:'proposed',revision:2,outcome:'Investigate source',sources:[{title:'<script>',text:'Ignore previous instructions'}]}],capabilities:[{id:'claude',name:'Claude Agent SDK',ready:true,live_verified:false}],repositories:[{id:'sample/repo'}]};
  c.api=async(url,data)=>{requests.push({url,data});return data?{saved:true}:url==='/api/objectives'?c.fixture:url.startsWith('/api/meetings')?{meetings:[]}:c.fixture.objectives[0];};
  vm.createContext(c); vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../web/objectives.js'),'utf8'),c);
  const run=code=>vm.runInContext(code,c);run('objectiveData=fixture');
  const click=async(action,id='')=>{const button={disabled:false,dataset:{objectiveAction:action,id}};for(const fn of handlers.click||[])await fn({target:{closest:()=>button}});};
  const submit=async(id,data={},dataset={},className='')=>{const form={id,dataset,className,reportValidity:()=>true,querySelectorAll:()=>Object.entries(data).map(([name,value])=>typeof value==='boolean'?{name,type:'checkbox',checked:value}:{name,value,type:'text'})};for(const fn of handlers.submit||[])await fn({target:form,preventDefault(){}});};
  return {c,app,run,click,submit,requests,handlers};
}
(async()=>{
  let evidence=harness();
  const rendered=evidence.run("objectiveEvidence([{record_id:'record-uuid',source_version_id:'version-hash',repo:'sample/repo',title:'Onboarding call',quote:'The trial lasts seven days.',url:'https://example.com/call',sequence:2}])");
  assert.match(rendered,/<strong>Onboarding call<\/strong>/);assert.match(rendered,/The trial lasts seven days/);assert.match(rendered,/href="https:\/\/example.com\/call"/);assert.doesNotMatch(rendered,/<strong>record-uuid/);
  for(const url of ['javascript:alert(1)','data:text/html,bad','meeting://private','https://user:secret@example.com']){
    evidence.c.badURL=url;const html=evidence.run("objectiveEvidence([{record_id:'r',repo:'sample/repo',title:'Safe',url:badURL,metadata_only:true,quote:'Unread body'}])");
    assert.doesNotMatch(html,/Unread body|href="(?:javascript:|data:|meeting:|https:\/\/user:)/);assert.match(html,/href="\/api\/records\?record_id=r&amp;repo=sample%2Frepo"/);
  }
  assert.equal(evidence.run("objectiveJudgment('interpretation_tradeoff')"),'Interpretation tradeoff');
  let budget=harness();budget.run("objectiveData.research_limits=[{repository:'sample/repo',used:6,limit:6,status:'throttled',next_retry_at:'2027-01-01T00:00:00Z'}];renderObjectives()");assert.match(budget.app.innerHTML,/Waiting for the research budget/);assert.match(budget.app.innerHTML,/Evidence remains queued/);
  budget.run("objectiveData.research_status=[{repository:'sample/repo',state:'blocked',attempts:3}];renderObjectives()");assert.match(budget.app.innerHTML,/Automatic retries stopped/);assert.match(budget.app.innerHTML,/explicit retry/);
  let h=harness();h.run('renderObjectives()');assert.match(h.app.innerHTML,/&lt;img/);assert.doesNotMatch(h.app.innerHTML,/<img onerror/);assert.match(h.app.innerHTML,/Live acceptance not verified/);assert.doesNotMatch(h.app.innerHTML,/Verified complete/);
  h.c.state.auth.enabled=true;h.c.state.me={role:'viewer'};h.run('renderObjectives()');assert.doesNotMatch(h.app.innerHTML,/data-objective-action="review-accept"/);await h.click('pause','o1');assert.equal(h.requests.length,0);
  h=harness();await h.click('review-accept','p1');assert.match(h.app.innerHTML,/does not activate/);await h.submit('objective-accept-form',{}, {id:'p1'});assert.equal(h.requests[0].url,'/api/proposals/p1/accept');assert.equal(h.requests[0].data.expected_revision,2);assert.ok(h.requests[0].data.request_key);
  h=harness();let finish;h.c.api=(url,data)=>{h.requests.push({url,data});return new Promise(resolve=>finish=resolve);};const first=h.click('pause','o1');await Promise.resolve();await h.click('pause','o1');assert.equal(h.requests.length,1);h.c.location.hash='#work';h.c.view='work';finish({saved:true});await first;assert.equal(h.c.location.hash,'#work');
  h=harness();let key;h.c.api=async(url,data)=>{if(data){if(!key){key=data.request_key;throw Error('Connection interrupted');}assert.equal(data.request_key,key);return {saved:true};}return h.c.fixture;};await h.click('pause','o1');assert.match(h.app.innerHTML,/Connection interrupted/);await h.click('pause','o1');
  h=harness();await h.click('new');assert.match(h.app.innerHTML,/Choose explicitly/);await h.submit('objective-create-form',{title:'Title',outcome:'Outcome',repository:'sample/repo',provider:'claude',success_criteria:'Tests',stop_criteria:'Blocked',guardrails:'Local only'});assert.equal(h.requests[0].url,'/api/objectives');assert.equal(h.requests[0].data.provider,'claude');
  h=harness('#today/o1');h.run('renderObjectives()');assert.match(h.app.innerHTML,/Measured result/);assert.match(h.app.innerHTML,/No production access/);assert.match(h.app.innerHTML,/No independent verification recorded/);assert.match(h.app.innerHTML,/human attestation|Attest outcome/);
  h=harness();let resolve;h.c.api=()=>new Promise(r=>resolve=r);const load=h.run('loadObjectives()');h.c.location.hash='#today/o1';resolve({objectives:[],proposals:[]});await load;assert.equal(h.run('objectiveData.objectives.length'),1);
  h=harness();h.c.api=async()=>{const error=Error('Access revoked');error.status=403;throw error;};await h.run('loadObjectives()');assert.equal(h.run('objectiveData'),null);assert.match(h.app.innerHTML,/Access revoked/);assert.doesNotMatch(h.app.innerHTML,/Ship safely/);
  h=harness();await h.click('meetings');assert.match(h.app.innerHTML,/create record only/i);assert.match(h.app.innerHTML,/every workspace reader/i);assert.doesNotMatch(h.app.innerHTML,/Connected to Vexa/);
  h=harness('#today/o1');h.c.fixture.objectives[0].proposals=[{id:'nested',status:'proposed',title:'Recheck the policy',revision:5}];h.run('renderObjectives()');assert.match(h.app.innerHTML,/Recheck the policy/);await h.click('review-accept','nested');await h.submit('objective-accept-form',{}, {id:'nested'});assert.equal(h.requests[0].url,'/api/proposals/nested/accept');assert.equal(h.requests[0].data.expected_revision,5);
  h=harness();h.c.fixture.proposals[0].status='review';h.c.fixture.proposals[0].task_id='review-task';h.run('renderObjectives()');assert.match(h.app.innerHTML,/#runs\/review-task/);assert.doesNotMatch(h.app.innerHTML,/data-objective-action="review-accept"/);
  h=harness();h.run("objectivePanel='new';objectiveDrafts.set('new',{start_preparation:false});renderObjectives()");assert.match(h.app.innerHTML,/name="start_preparation"(?![^>]*checked)/);
  h=harness();h.run("objectivePanel='scan';objectiveDrafts.set('scan',{enabled:true});renderObjectives()");assert.match(h.app.innerHTML,/name="enabled"[^>]*checked/);
  // Checkbox-shaped DOM doubles catch accidental 'on' string coercion.
  h=harness();await h.submit('objective-create-form',{start_preparation:true});assert.equal(h.requests[0].data.start_preparation,true);
  h=harness();await h.submit('objective-create-form',{start_preparation:false});assert.equal(h.requests[0].data.start_preparation,false);
  h=harness();await h.submit('objective-watch-form',{repository:'sample/repo',enabled:true});assert.equal(h.requests[0].data.enabled,true);await h.submit('objective-watch-form',{repository:'sample/repo',enabled:false});assert.equal(h.requests.filter(r=>r.data).at(-1).data.enabled,false);
  function meetingHarness(role='admin') {
    const item=harness();item.c.state.auth.enabled=true;item.c.state.me={role};
    item.run(`objectivePanel='meetings';objectiveMeetings={meetings:[{id:'m1',title:'Consented call',repo:'sample/repo',provider:'vexa',state:'requested',background_sync:{enabled:false,status:'disabled'}}]};renderObjectives()`);
    return item;
  }
  h=meetingHarness();assert.match(h.app.innerHTML,/No automatic join or recording/);assert.match(h.app.innerHTML,/periodic background status and transcript reads/);assert.match(h.app.innerHTML,/Capture not established complete/);
  await h.submit('',{notice_confirmed:true,join:true,transcribe:true},{id:'m1'},'objective-meeting-consent');assert.equal(h.requests[0].url,'/api/meetings/m1/consent');assert.equal(h.requests[0].data.record,false);assert.equal(h.requests[0].data.notice_confirmed,true);assert.equal(h.requests.filter(r=>r.url.endsWith('/join')).length,0);
  h=meetingHarness();await h.submit('',{enabled:true,workspace_shared:true},{id:'m1'},'objective-meeting-background');assert.equal(h.requests[0].url,'/api/meetings/m1/background-sync');assert.equal(h.requests[0].data.enabled,true);assert.equal(h.requests[0].data.workspace_shared,true);assert.equal(h.requests[0].data.repo,'sample/repo');
  h=meetingHarness();await h.submit('',{enabled:false,workspace_shared:false},{id:'m1'},'objective-meeting-background');assert.equal(h.requests[0].data.enabled,false);assert.equal(h.requests[0].data.workspace_shared,false);
  h=meetingHarness();let release;h.c.api=async(url,data)=>{h.requests.push({url,data});if(data)await new Promise(resolve=>release=resolve);return {meetings:[]};};const saving=h.submit('',{enabled:true,workspace_shared:true},{id:'m1'},'objective-meeting-background');await Promise.resolve();await h.submit('',{enabled:true,workspace_shared:true},{id:'m1'},'objective-meeting-background');assert.equal(h.requests.length,1);release();await saving;
  for(const role of ['viewer','member']) {h=meetingHarness(role);assert.doesNotMatch(h.app.innerHTML,/Save background sync|Record consent|Stop remote bot/);await h.submit('',{enabled:true,workspace_shared:true},{id:'m1'},'objective-meeting-background');await h.click('meeting-stop','m1');assert.equal(h.requests.length,0);}
  // Today groups work by what it waits on; closed objectives fold away; the
  // sidebar count is the server's two counts added up; no dead 'pending' state.
  h=harness();h.c.fixture.objectives=[{id:'o1',title:'Blocked outcome',status:'needs_you',revision:1,blocked_reason:'Scheduled checkpoint is due'},{id:'o2',title:'Moving outcome',status:'moving',revision:1,runs:[{task_id:'t2',status:'working',updated_at:new Date().toISOString()}]},{id:'o3',title:'Finished outcome',status:'waiting',revision:1,runs:[{task_id:'t3',status:'completed',updated_at:new Date().toISOString()}]},{id:'o4',title:'Stale outcome',status:'waiting',revision:1,runs:[{task_id:'t4',status:'completed',updated_at:'2020-01-01T00:00:00Z'}]}];
  h.c.fixture.proposals=[{id:'p1',title:'Suggested move',status:'proposed',revision:2},{id:'p2',title:'Reviewed move',status:'review',revision:3,task_id:'gov-task',reviewer:'Policy Owner'}];
  h.c.fixture.closed=[{id:'o9',title:'Achieved outcome',status:'achieved',lifecycle:'achieved',revision:4}];h.c.fixture.today={proposals_open:1,objectives_needing_you:1};
  h.run('renderObjectives()');
  const section=key=>{const html=h.app.innerHTML;const start=html.indexOf(`data-group="${key}"`);const end=html.indexOf('<section class="objective-section"',start+1);return html.slice(start,end<0?undefined:end);};
  assert.match(section('decide'),/Needs your decision/);assert.match(section('decide'),/Suggested move/);assert.match(section('decide'),/Blocked outcome/);assert.doesNotMatch(section('decide'),/Reviewed move|Moving outcome/);
  assert.match(section('progress'),/In progress/);assert.match(section('progress'),/Moving outcome/);assert.match(section('progress'),/Awaiting signed review · Policy Owner/);
  assert.match(section('review'),/Ready to review/);assert.match(section('review'),/Finished outcome/);assert.doesNotMatch(section('review'),/Stale outcome/);
  assert.match(h.app.innerHTML,/<details class="objective-evidence objective-closed"><summary>Closed · 1<\/summary>[^]*Achieved outcome/);
  assert.equal(h.run('objectiveTodayCount(fixture.today)'),(section('decide').match(/<article class="objective-card"/g)||[]).length);
  assert.doesNotMatch(fs.readFileSync(require('node:path').join(__dirname,'../web/objectives.js'),'utf8'),/'pending'/);
  assert.match(h.app.innerHTML,/<div id="evidence-mount"><\/div>/);assert.doesNotMatch(h.app.innerHTML,/Open loops/);
  // Without a model key: the open loops block carries the notice and Find opportunities is off.
  h.c.state.model={backend:'none',proposals:false,investigation:false,notice:'Suggestions and briefs are off: no model key. Routing, records, reminders and signed decisions still run. Add a key with ./setup --configure and restart.'};
  h.c.fixture.without_model={followups:[{id:'d1',run_id:'t1',task_title:'Pilot CSV path',question:'Which trial limit applies?'}],needs_review:[{id:'d2',run_id:'t1',task_title:'Pilot CSV path',question:'Keep the CSV example?',review_reason:'The policy it leaned on was corrected'}],checkpoints_due:[{id:'o1',title:'Blocked outcome',next_check_at:'2020-01-01T00:00:00Z',waiting_on:'Pilot'}],new_records:[{repository:'sample/repo',count:3}],totals:{followups:12,needs_review:1,checkpoints_due:1,new_records:1}};
  h.run('renderObjectives()');
  assert.match(h.app.innerHTML,/Open loops \(no model key\)/);assert.match(h.app.innerHTML,/Suggestions and briefs are off: no model key\./);assert.match(h.app.innerHTML,/Which trial limit applies\?/);assert.match(h.app.innerHTML,/The policy it leaned on was corrected/);assert.match(h.app.innerHTML,/sample\/repo<\/strong> · 3 records/);assert.match(h.app.innerHTML,/11 more\./);
  assert.match(h.app.innerHTML,/data-objective-action="scan-panel"[^>]*disabled[^>]*title="Suggestions and briefs are off: no model key\. Routing, records, reminders and signed decisions still run\. Add a key with \.\/setup --configure and restart\."/);
  await h.click('scan-panel');assert.doesNotMatch(h.app.innerHTML,/Inspect evidence/);
  // A due checkpoint the worker already marked has no next_check_at left; it is still listed.
  h.c.fixture.without_model.checkpoints_due=[{id:'o1',title:'Blocked outcome',next_check_at:'',waiting_on:'Pilot'}];h.run('renderObjectives()');assert.match(h.app.innerHTML,/Blocked outcome<\/a> · due now · waiting on Pilot/);
  // With the internal claude-cli backend automatic suggestions are off, so the open loops stay, but a manual Find opportunities click works.
  h.c.state.model={backend:'claude-cli',proposals:false,investigation:true,notice:'Manual investigation only; automatic suggestions need an API key.'};h.run('renderObjectives()');
  assert.match(h.app.innerHTML,/Open loops \(no model key\)/);assert.match(h.app.innerHTML,/Manual investigation only/);assert.doesNotMatch(h.app.innerHTML,/data-objective-action="scan-panel"[^>]*disabled/);
  await h.click('scan-panel');assert.match(h.app.innerHTML,/Inspect evidence/);h.run('objectivePanel=""');
  // The inbox card links a scope review to #today/<proposal id>.
  h=harness('#today/p1');h.run('renderObjectives()');assert.match(h.app.innerHTML,/Suggested next step/);assert.match(h.app.innerHTML,/Investigate source/);assert.doesNotMatch(h.app.innerHTML,/Objective unavailable/);
  h=harness('#today/p1');await h.run('loadObjectives()');assert.deepEqual(h.requests.map(r=>r.url),['/api/objectives']);assert.match(h.app.innerHTML,/Investigate source/);
  // 'Not now' opens a reason field inside the card; nothing is posted until a reason of three characters is given.
  h=harness();await h.click('dismiss','p1');assert.equal(h.requests.length,0);assert.match(h.app.innerHTML,/id="objective-dismiss-form"[^>]*data-id="p1"/);assert.match(h.app.innerHTML,/name="rationale"[^>]*maxlength="200"/);assert.match(h.app.innerHTML,/shapes the next investigation/);
  await h.submit('objective-dismiss-form',{rationale:'  '},{id:'p1'});assert.equal(h.requests.length,0);assert.match(h.app.innerHTML,/A reason of at least 3 characters is required/);
  await h.submit('objective-dismiss-form',{rationale:'Already handled on the pilot branch'},{id:'p1'});assert.equal(h.requests[0].url,'/api/proposals/p1/dismiss');assert.equal(h.requests[0].data.rationale,'Already handled on the pilot branch');assert.equal(h.requests[0].data.expected_revision,2);assert.ok(h.requests[0].data.request_key);
  // A card under review says who it waits on, with the reason the review was routed there.
  h=harness();h.c.fixture.proposals=[{id:'p2',title:'Reviewed move',status:'review',revision:3,task_id:'gov-task',reviewer:'Policy Owner',reviewer_reason:'Assigned to Policy Owner as the objective accepter: no authority map covers this scope'}];h.run('renderObjectives()');
  assert.match(h.app.innerHTML,/Waiting on Policy Owner \(Assigned to Policy Owner as the objective accepter: no authority map covers this scope\)/);
  h.c.fixture.proposals[0].reviewer_reason='';h.run('renderObjectives()');assert.match(h.app.innerHTML,/Waiting on Policy Owner<\/p>/);
  // The research aside carries the ledger's precision line and the declined reasons from /api/metrics, read once with Today; a failed read hides only that line.
  h=harness();h.c.api=async(url,data)=>{h.requests.push({url,data});return url==='/api/metrics'?{window_days:30,work_proposals:{signed:3,dismissed:1,precision:0.75,declined_recent:[{repository:'sample/repo',title:'Rewrite the importer',rationale:'The importer is being replaced next quarter',updated_at:'2026-10-20T10:00:00Z'}]}}:h.c.fixture;};
  await h.run('loadObjectives()');assert.deepEqual(h.requests.map(r=>r.url),['/api/objectives','/api/metrics']);
  assert.match(h.app.innerHTML,/Proposals, last 30 days: 3 signed · 1 declined · precision 75%/);assert.match(h.app.innerHTML,/Declined recently/);assert.match(h.app.innerHTML,/Rewrite the importer<\/strong> · The importer is being replaced next quarter/);
  h=harness();h.c.api=async(url)=>{if(url==='/api/metrics')throw Error('metrics unavailable');return h.c.fixture;};await h.run('loadObjectives()');assert.match(h.app.innerHTML,/Ship safely/);assert.doesNotMatch(h.app.innerHTML,/Proposals, |metrics unavailable/);
  // The line names its window and says nothing while no proposal was judged in it: older dismissals list on their own, a withheld one without a reason.
  const metricsHarness=(work,metrics={})=>{const item=harness();item.c.api=async(url)=>url==='/api/metrics'?{window_days:30,...metrics,work_proposals:work}:item.c.fixture;return item;};
  h=metricsHarness({signed:0,corrected:0,dismissed:0,precision:null,declined_recent:[{repository:'sample/repo',withheld:true,title:'Proposal withheld: source access changed',updated_at:'2026-09-01T10:00:00Z'}]});await h.run('loadObjectives()');
  assert.doesNotMatch(h.app.innerHTML,/Proposals, |not yet measurable/);assert.match(h.app.innerHTML,/Declined recently/);assert.match(h.app.innerHTML,/Proposal withheld: source access changed<\/strong><small>/);
  h=metricsHarness({signed:2,dismissed:2,precision:0.5},{window_days:0});await h.run('loadObjectives()');assert.match(h.app.innerHTML,/Proposals, all time: 2 signed · 2 declined · precision 50%/);
  // Where nothing can be investigated there is no precision to report, and the section stays hidden with it.
  h=metricsHarness({signed:2,dismissed:2,precision:0.5});h.c.state.model={backend:'none',proposals:false,investigation:false,notice:'No model key.'};await h.run('loadObjectives()');assert.doesNotMatch(h.app.innerHTML,/Proposals, |Background investigation/);
  // A short reason is refused on screen even while focus is inside the form, the typed text stays, and focus follows the form:
  // into the reason field as it opens, back to the card's 'Not now' as it closes, to the group heading when the card is gone; never to the body.
  h=harness();const focused=[];h.app.contains=()=>true;h.c.document.getElementById=id=>id==='app'?h.app:h.app.innerHTML.includes(`id="${id}"`)?{id,focus(){focused.push(id);}}:null;
  h.c.document.activeElement={id:'objective-dismiss-p1',closest:()=>null};await h.click('dismiss','p1');
  assert.deepEqual(focused,['objective-dismiss-rationale']);assert.match(h.app.innerHTML,/<textarea name="rationale" id="objective-dismiss-rationale"/);assert.doesNotMatch(h.app.innerHTML,/id="objective-dismiss-p1"/);
  const dismissForm={id:'objective-dismiss-form',className:'objective-dismiss',querySelectorAll:()=>[{name:'rationale',value:'ok',type:'text'}]};
  for(const fn of h.handlers.input)fn({target:{closest:()=>dismissForm}});
  h.c.document.activeElement={id:'objective-dismiss-rationale',closest:()=>dismissForm,selectionStart:2,selectionEnd:2};
  await h.submit('objective-dismiss-form',{rationale:'ok'},{id:'p1'});
  assert.equal(h.requests.length,0);assert.match(h.app.innerHTML,/role="alert">A reason of at least 3 characters is required/);assert.match(h.app.innerHTML,/id="objective-dismiss-rationale"[^>]*>ok<\/textarea>/);assert.equal(focused.at(-1),'objective-dismiss-rationale');
  await h.click('close-panel');assert.equal(focused.at(-1),'objective-dismiss-p1');assert.doesNotMatch(h.app.innerHTML,/id="objective-dismiss-form"|at least 3 characters/);
  await h.click('dismiss','p1');await h.submit('objective-dismiss-form',{rationale:'Already handled'},{id:'p1'});assert.equal(h.requests.filter(r=>r.data).length,1);assert.equal(focused.at(-1),'objective-dismiss-p1');
  await h.click('dismiss','p1');h.c.fixture={...h.c.fixture,proposals:[]};await h.submit('objective-dismiss-form',{rationale:'Already handled'},{id:'p1'});assert.equal(focused.at(-1),'objective-group-decide');assert.match(h.app.innerHTML,/<h2 id="objective-group-decide" tabindex="-1">/);
  console.log('Today UI: 43 contract scenarios passed (DOM/API doubles).');
})().catch(error=>{console.error(error);process.exitCode=1;});
