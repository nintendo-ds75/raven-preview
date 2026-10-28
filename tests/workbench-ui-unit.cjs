/* Browser-task presentation and submission contracts. DOM doubles; no browser. */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
function harness(id='task') {
  const handlers={}, requests=[], elements={};
  const el=()=>({value:'',innerHTML:'',dataset:{},querySelectorAll:()=>[],querySelector:()=>null});
  for(const id of ['app','work-prompt','work-repository']) elements[id]=el();
  elements.app.dataset.workId=id; elements['work-repository'].value='sample/repo';
  const task={task_id:id,requester:'Original author',prompt:'Task',repository:'sample/repo',status:'input',conversation:[],pending_messages:[],activity:[]};
  const tree={task_id:id,nodes:[]};
  const c={console,crypto:require('node:crypto').webcrypto,view:'work',navigationVersion:0,
    location:{hash:'#work/'+id},state:{auth:{enabled:false},workbench:{ready:true,enabled:true,repositories:[{id:'sample/repo'}]},browser_tasks:[]},
    document:{activeElement:{},getElementById:id=>elements[id],addEventListener:(type,fn)=>handlers[type]=fn},
    window:{scrollY:0,scrollTo(){}},CSS:{escape:x=>x},$:s=>elements[s.slice(1)],
    esc:x=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
    icon:()=>'',avatar:()=>'',flattenTask:n=>n,taskAuthorized:n=>!!n.authorized,refresh:async()=>{}};
  c.api=async(url,data)=>{requests.push({url,data});if(data)return {task_id:'created'};return url.endsWith('/tree')?tree:url.endsWith('/trace')?{}:task;};
  vm.createContext(c);for(const file of ['task.js','workbench.js'])vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../web/'+file),'utf8'),c);
  const run=s=>vm.runInContext(s,c);c.fixture={task,tree,trace:{}};run('workData=fixture');
  const submit=()=>handlers.submit({target:{id:'work-compose',dataset:{task:id},reportValidity:()=>true},preventDefault(){}});
  const draft=text=>{elements['work-prompt'].value=text;run('workDraft().text='+JSON.stringify(text));};
  return {c,run,submit,draft,requests,elements,task,tree};
}
(async()=>{
  let h=harness('');h.draft('<img src=x onerror=bad()>');h.run('renderWorkbench()');
  assert.equal(h.run("workText('`unfinished')"),'`unfinished');
  assert.match(h.run("workText('**<script>**')"),/<strong>&lt;script&gt;<\/strong>/);
  assert.match(h.run("workText('| A | B |\\n|---|---|\\n| `<script>` | value |')"), /<table>.*<code>&lt;script&gt;<\/code>/);
  assert.match(h.run("workText('```\\n## literal heading\\n```')"), /<pre><code>## literal heading/);
  assert.match(h.elements.app.innerHTML,/&lt;img/);assert.doesNotMatch(h.elements.app.innerHTML,/<img src=x/);
  // The home is the composer under 'Start a task', with nothing else to read; no recents list exists.
  assert.match(h.elements.app.innerHTML,/<h1>Start a task<\/h1><div class="work-modes"/);assert.match(h.elements.app.innerHTML,/aria-label="Start task" >/);
  assert.doesNotMatch(h.elements.app.innerHTML,/What should we work on|View all workspace tasks|Raven uses your context|separate copy of your repository|work-home-note/);
  assert.equal(h.run('typeof renderWorkRecents'),'undefined');
  h.run('workMode="ask";renderWorkbench()');assert.match(h.elements.app.innerHTML,/<h1>Start a task<\/h1>/);assert.doesNotMatch(h.elements.app.innerHTML,/What would you like to know\?<\/h1>|No coding host is started/);h.run('workMode="task"');
  h.c.state.me={role:'viewer'};h.c.state.auth.enabled=true;h.run('renderWorkbench()');
  assert.match(h.elements.app.innerHTML,/<p class="work-home-note">Your account can view tasks\. A workspace member can start one\.<\/p>/);assert.match(h.elements.app.innerHTML,/aria-label="Start task" disabled/);
  // Without a ready workbench the home draws nothing: navigate() sends #work to the task list.
  h=harness('');h.c.state.workbench.ready=false;h.run('renderWorkbench()');assert.equal(h.elements.app.innerHTML,'');
  // The error card and its retry.
  h=harness();h.run("workData=null;workError='Synthetic failure';renderWorkbench()");assert.match(h.elements.app.innerHTML,/^<div class="empty" role="alert"><h3>Couldn’t load your workspace\.<\/h3><p>Synthetic failure<\/p><button class="button" data-action="retry">Try again<\/button><\/div>$/);
  h.run("workError='';renderWorkbench()");assert.match(h.elements.app.innerHTML,/role="status">Opening your task…/);
  h=harness();h.draft('A follow-up');await h.submit();assert.equal(h.elements['work-prompt'].value,'');assert.equal(h.run('workDraft().text'),'');
  h=harness();h.draft('Retry this exactly');let payload;h.c.api=async(url,data)=>{if(data){if(!payload){payload=data;throw Error('Network interrupted');}assert.deepEqual(data,payload);return {saved:true};}return url.endsWith('/tree')?h.tree:url.endsWith('/trace')?{}:h.task;};
  await h.submit();assert.equal(h.run('workDraft().error'),'Network interrupted');await h.submit();assert.equal(h.run('workDraft().text'),'');
  h=harness('');h.draft('Original task');let finish;h.c.api=()=>new Promise(resolve=>finish=resolve);const pending=h.submit();
  h.c.navigationVersion=2;finish({task_id:'late'});await pending;assert.equal(h.c.location.hash,'#work/');
  h=harness();h.task.conversation=[{role:'user',actor:'Other member',text:'A shared request'}];h.run('renderWorkbench()');assert.match(h.elements.app.innerHTML,/Other member/);assert.doesNotMatch(h.elements.app.innerHTML,/>You<|Evidence is context/);
  h.tree.nodes=[{node_id:'one',status:'suggested',question:'Optional idea',owner:'Mira',blocking:false}];
  h.run('renderWorkbench()');assert.match(h.elements.app.innerHTML,/Follow-up for the agent · Mira/);assert.doesNotMatch(h.elements.app.innerHTML,/Required follow-up|Suggested follow-up/);assert.match(h.elements.app.innerHTML,/data-work-detail="decision-one"/);
  // 'At a glance' rows carry the task page's sentence: an answer awaiting sign-off is not 'Waiting for an answer',
  // an unanswered non-blocking node is not 'Answer recorded', and a name is never written twice.
  h.tree.nodes=[{node_id:'two',status:'proposed',question:'Backup retention?',owner:'Alex Morgan',blocking:true,answer:'Keep 30 days.',signoff:'required'},
    {node_id:'three',status:'pending',question:'Open one',owner:'Alex Morgan',blocking:false},
    {node_id:'four',status:'resolved',question:'Signed one',owner:'Alex Morgan',blocking:false,authorized:true,signoff:'signed',signatures:['Marisol Vega'],answer:'Yes.'},
    {node_id:'five',status:'pending',question:'Nobody yet',owner:'',blocking:true}];
  h.run('renderWorkbench()');const glance=h.elements.app.innerHTML;
  assert.match(glance,/Waiting for sign-off · Alex Morgan<\/span><span>Backup retention\?/);
  assert.match(glance,/Waiting on Alex Morgan<\/span><span>Open one/);
  assert.match(glance,/is-signed">Signed by Marisol Vega · Alex Morgan<\/span><span>Signed one/);
  assert.match(glance,/Needs a contact<\/span><span>Nobody yet/);
  assert.doesNotMatch(glance,/Waiting for an answer|Answer recorded|Authorized ·|Unassigned|Waiting on Alex Morgan · Alex Morgan/);
  h.tree.nodes=[];
  h.c.fixture.trace.notifications=[{person_name:'Earlier contact'}];h.run('renderWorkbench()');assert.match(h.elements.app.innerHTML,/Earlier contact/);
  h.task.status='completed';h.task.summary='Agent claims done';h.tree.review={status:'failed'};h.run('renderWorkbench()');
  assert.match(h.elements.app.innerHTML,/Patch ready for manual review/);assert.match(h.elements.app.innerHTML,/automatic code review is incomplete/);
  h.run("workVerifications.set('task',{config:{ready:true,profiles:[{id:'heldout',repository:'sample/repo',label:'Behavior cases'}]},receipts:[{profile_id:'heldout',status:'passed',current:false,usable_pass:false,coverage:'<script>bad</script>',coverage_basis:'operator-held-fixtures',binding:{candidate_tree_sha256:'abc'}}]})");
  h.run('renderWorkbench()');assert.match(h.elements.app.innerHTML,/no longer current/);assert.match(h.elements.app.innerHTML,/&lt;script&gt;/);assert.doesNotMatch(h.elements.app.innerHTML,/Current configured checks passed/);
  // The checks sit behind one 'Checks' disclosure on the result, after its actions.
  assert.match(h.elements.app.innerHTML,/Decisions & review<\/a><\/div><details class="work-checks" data-work-detail="checks"><summary>Checks<\/summary>[\s\S]*data-work-action="verify-profile"[\s\S]*<\/details><small>The original repository is unchanged/);assert.doesNotMatch(h.elements.app.innerHTML,/Independent artifact checks/);
  h.c.state.auth.enabled=true;h.c.state.me={role:'viewer'};h.run('renderWorkbench()');assert.doesNotMatch(h.elements.app.innerHTML,/data-work-action="verify-profile"/);
  h.task.withheld=true;h.run('renderWorkbench()');assert.match(h.elements.app.innerHTML,/Access needs attention/);assert.doesNotMatch(h.elements.app.innerHTML,/Behavior cases|operator-held-fixtures/);
  console.log('Browser task UI: 18 scenario checks passed (DOM/API doubles).');
})().catch(e=>{console.error(e);process.exitCode=1;});
