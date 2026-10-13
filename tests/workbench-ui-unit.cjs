/* Browser-task presentation and submission contracts. DOM doubles; no browser. */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
function harness(id='task') {
  const handlers={}, requests=[], elements={};
  const el=()=>({value:'',innerHTML:'',dataset:{},querySelectorAll:()=>[],querySelector:()=>null});
  for(const id of ['app','work-prompt','work-repository','work-recents']) elements[id]=el();
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
  vm.createContext(c);vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../web/workbench.js'),'utf8'),c);
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
  h.c.state.me={role:'viewer'};h.c.state.auth.enabled=true;h.run('renderWorkbench()');
  assert.match(h.elements.app.innerHTML,/Your account can view tasks/);assert.match(h.elements.app.innerHTML,/aria-label="Start task" disabled/);
  h=harness();h.draft('A follow-up');await h.submit();assert.equal(h.elements['work-prompt'].value,'');assert.equal(h.run('workDraft().text'),'');
  h=harness();h.draft('Retry this exactly');let payload;h.c.api=async(url,data)=>{if(data){if(!payload){payload=data;throw Error('Network interrupted');}assert.deepEqual(data,payload);return {saved:true};}return url.endsWith('/tree')?h.tree:url.endsWith('/trace')?{}:h.task;};
  await h.submit();assert.equal(h.run('workDraft().error'),'Network interrupted');await h.submit();assert.equal(h.run('workDraft().text'),'');
  h=harness('');h.draft('Original task');let finish;h.c.api=()=>new Promise(resolve=>finish=resolve);const pending=h.submit();
  h.c.navigationVersion=2;finish({task_id:'late'});await pending;assert.equal(h.c.location.hash,'#work/');
  h=harness();h.task.conversation=[{role:'user',actor:'Other member',text:'A shared request'}];h.run('renderWorkbench()');assert.match(h.elements.app.innerHTML,/Other member/);assert.doesNotMatch(h.elements.app.innerHTML,/>You</);
  h.tree.nodes=[{node_id:'one',status:'suggested',question:'Optional idea',owner:'Mira',blocking:false}];
  h.run('renderWorkbench()');assert.match(h.elements.app.innerHTML,/Suggested follow-up/);assert.doesNotMatch(h.elements.app.innerHTML,/Required follow-up/);assert.match(h.elements.app.innerHTML,/data-work-detail="decision-one"/);
  h.c.fixture.trace.notifications=[{person_name:'Earlier contact'}];h.run('renderWorkbench()');assert.match(h.elements.app.innerHTML,/Earlier contact/);
  h.task.status='completed';h.task.summary='Agent claims done';h.tree.review={status:'failed'};h.run('renderWorkbench()');
  assert.match(h.elements.app.innerHTML,/Patch ready for manual review/);assert.match(h.elements.app.innerHTML,/automatic code review is incomplete/);
  console.log('Browser task UI: 9 scenario checks passed (DOM/API doubles).');
})().catch(e=>{console.error(e);process.exitCode=1;});
