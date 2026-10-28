/* Fresh synthetic checks: actual shipped JS, queued DOM/API doubles, no browser or provider. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const fixture = require('./minimal-run-ui-fixture.cjs');
const root = path.resolve(__dirname, '..');

function harness() {
  const elements = new Map(), requests = [], closes = [], notices = [];
  const target = () => {
    const handlers = new Map();
    return {addEventListener(type, handler) { handlers.set(type, [...(handlers.get(type) || []),handler]); },
      dispatch(type, event = {}) { event.target ??= this; event.preventDefault ??= () => {};
        return Promise.all((handlers.get(type) || []).map(fn => fn(event))); }};
  };
  const element = (id = '') => ({...target(), id, dataset:{}, innerHTML:'', textContent:'', value:'',
    hidden:false, open:false, disabled:false, isConnected:true, tagName:'DIV', selectionStart:0, selectionEnd:0,
    classList:{add(){},remove(){},toggle(){}}, setAttribute(){},removeAttribute(){},
    getAttribute(name) { return this[name] ?? null; },
    querySelector(){return null;},querySelectorAll(){return [];}, closest(){return null;},
    contains(el){return el === this;},focus(){document.activeElement=this;},
    setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;},
    showModal(){this.open=true;},close(){this.open=false;closes.push(() => this.dispatch('close'));},
    getBoundingClientRect(){return {left:10,right:100,top:10,bottom:100};}});
  const get = selector => {
    if (!elements.has(selector)) elements.set(selector,element(selector.startsWith('#') ? selector.slice(1) : selector));
    return elements.get(selector);
  };
  const document = {...target(),querySelector:get,querySelectorAll:()=>[],getElementById:id=>get('#'+id),
    body:{append(){},classList:{toggle(){}}},documentElement:{lang:'en'},activeElement:{tagName:'BODY'},createElement:element};
  class FormDataDouble { constructor(form) {this.form=form;} *[Symbol.iterator]() {
    for(const [name,input] of Object.entries(this.form.fields)) if(!input.disabled) yield [name,input.value];
  } get(name){return this.form.fields[name]?.value ?? null;} }
  const context = {...target(),console,document,URLSearchParams,FormData:FormDataDouble,
    location:{hash:'#inbox',pathname:'/',search:''},history:{replaceState(){}},
    crypto:require('node:crypto').webcrypto,setTimeout:()=>1,clearTimeout(){},setInterval(){},
    fetch:()=>new Promise(()=>{})};
  context.window=context;vm.createContext(context);
  for(const name of ['presentation.js','task.js','app.js']) vm.runInContext(fs.readFileSync(path.join(root,'web',name),'utf8'),context,{filename:name});
  const run = code => vm.runInContext(code,context);
  context.syntheticState=fixture.state();run('state=syntheticState; fetching=false;');
  context.apiDouble=(url,data) => new Promise((resolve,reject)=>requests.push({url,data,resolve,reject}));
  context.noteDouble=message=>notices.push(message);
  context.realRefresh=run('refresh');
  run('api=apiDouble; notify=noteDouble; refresh=async()=>{};');
  function form(id, values, dataset={}, mount=id==='note-form'?'app':'modal') {
    const form=element(id), button=element(), error=element('form-error'), progress=element('request-progress'), scope=element('request-scope-area');
    form.dataset=id==='request-form'?{clientKey:run('requestDraft.clientKey'),clientRef:run('requestDraft.clientRef'),...dataset}:dataset;
    form.fields={};form.factInputs=[];form.reportValidity=()=>true;form.reset=()=>Object.values(form.fields).forEach(x=>x.value='');
    for(const [name,value] of Object.entries(values)) {const input=element(name==='text'&&id==='note-form'?'note-text':name);
      input.value=value;input.tagName=['question','context','text'].includes(name)?'TEXTAREA':'INPUT';
      input.name=name;input.closest=()=>form; form.fields[name]=input;
      if(!(mount==='modal'&&input.id==='note-text'&&get('#app').querySelector('#note-text')))elements.set('#'+input.id,input);}
    form.querySelector=selector=>selector.includes('submit')?button:selector.includes('error')?error:selector.includes('progress')?progress:selector==='#request-scope-area'?scope:selector==='select'?form.fields.repo:
      Object.values(form.fields).find(x=>selector==='#'+x.id || selector.includes('"'+x.name+'"')) || null;
    form.querySelectorAll=selector=>selector==='[data-fact-key]'?form.factInputs:selector.includes('button')?[button]:[...Object.values(form.fields),...form.factInputs];
    form.contains=el=>el===form || Object.values(form.fields).includes(el) || el===button || el===error;
    form.closest=selector=>selector==='#'+mount?get('#'+mount):null;
    if(mount==='app') {get('#app').querySelector=selector=>selector==='#note-text'?form.fields.text:selector==='#note-form'?form:null;get('#app').contains=el=>form.contains(el);}
    else get('#modal').contains=el=>form.contains(el);
    elements.set('#'+id,form);elements.set('#form-error',error);
    context.currentForm=form;return {form,button,error,progress,scope,
      setFacts:entries=>{form.factInputs=entries.map(([key,value],i)=>Object.assign(element('request-fact-'+i),{dataset:{factKey:key},value}));}};
  }
  function selectTask(tree,trace=fixture.trace()) { context.syntheticDetail={tree,trace};
    context.location.hash='#runs/'+tree.task_id;run("view='runs'; taskDetail=syntheticDetail; taskError=''; taskTab='decisions';"); }
  return {run,context,get,document,requests,notices,form,selectTask,
    detach:selector=>{const el=elements.get(selector);if(el)el.isConnected=false;elements.delete(selector);},
    submit:form=>document.dispatch('submit',{target:form}),
    tick:()=>new Promise(resolve=>setImmediate(resolve))};
}

(async()=>{
  const failures=[];let passed=0;
  async function test(name,body) {let timer;try {await Promise.race([body(),new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new Error('Queued API did not finish')),2000);})]);passed++;}
    catch(error){failures.push(name+': '+error.stack);}finally{clearTimeout(timer);}}
  await test('Ask uses existing repositories and optional context without identity field',()=>{
    const h=harness();h.run('newRequest()');const html=h.get('#modal-content').innerHTML;
    assert.match(html,/<form[^>]+id="request-form"/);assert.match(html,/<textarea[^>]+id="question"[^>]+maxlength="2000"/);
    assert.match(html,/<select[^>]+id="repo"/);assert.match(html,/synthetic\/secondary/);
    assert.match(html,/<details[^>]+id="request-context"/);
    for(const id of ['context','path']) {const tag=html.match(new RegExp('<(?:textarea|input)[^>]+id="'+id+'"[^>]*>'))?.[0];
      assert(tag,id+' is present');assert.doesNotMatch(tag,/\brequired\b/);}
    assert.doesNotMatch(html,/(?:id|name)="(?:requester|owner_id|agent|title)"/);
  });
  await test('unsubmitted Ask draft and keys survive close/reopen with escaped markup',()=>{
    const h=harness();h.run('newRequest()');const raw='<script>bad()</script> & a real question';
    const f=h.form('request-form',{question:raw,repo:'synthetic/minimal-ui',context:'<img src=x>',path:'src/odd&name.py'});
    const key=f.form.dataset.clientKey,ref=f.form.dataset.clientRef;h.run('closeModal();newRequest()');
    const html=h.get('#modal-content').innerHTML;assert(html.includes(key));assert(html.includes(ref));
    assert.match(html,/&lt;script&gt;bad\(\)&lt;\/script&gt;/);assert.match(html,/&lt;img src=x&gt;/);
    assert.doesNotMatch(html,/<script>|<img src=x>/);assert.equal(h.requests.length,0);
  });
  await test('Ask duplicate submit creates one start; failed node retries exact original payload and keys',async()=>{
    const h=harness();h.run('newRequest()');const question='🪶'.repeat(310)+' original full prompt';
    const f=h.form('request-form',{question,repo:'synthetic/minimal-ui',context:'A constraint',path:'src/example.py'});
    const pending=h.run('submitRequest(currentForm)');const duplicate=h.run('submitRequest(currentForm)');
    assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,'/api/tasks/start');
    const start=h.requests[0].data;assert.equal(start.title,Array.from(question).slice(0,300).join(''));
    assert.equal(start.goal,question);assert.equal(start.agent,'Raven UI');assert.equal(start.repo,'synthetic/minimal-ui');
    assert.equal(start.paths,'src/example.py');assert(!('requester' in start));assert(start.client_key);
    h.requests[0].resolve({task_id:'created-run'});await h.tick();
    assert.equal(h.requests[1].url,'/api/tasks/created-run/nodes');const payload=h.requests[1].data;
    assert.equal(payload.question,question);assert.equal(payload.context,'A constraint');assert.equal(payload.paths,'src/example.py');assert(payload.client_ref);
    h.requests[1].reject(new Error('Synthetic node failure'));await pending;await duplicate;
    assert.equal(f.form.dataset.taskId,'created-run');assert.equal(f.form.fields.question.value,question);
    assert.equal(f.button.disabled,false);assert.equal(f.error.hidden,false);assert.match(f.error.textContent,/Synthetic node failure/);
    f.form.fields.question.value='Changed after partial failure';
    const retry=h.run('submitRequest(currentForm)');assert.equal(h.requests.length,3);
    assert.equal(h.requests[2].url,'/api/tasks/created-run/nodes');assert.deepEqual(h.requests[2].data,payload);
    h.requests[2].resolve({node_id:'created-node'});await retry;
    assert.equal(h.requests.filter(x=>x.url==='/api/tasks/start').length,1);
  });
  await test('start failure retries a stable client key and retained prompt',async()=>{
    const h=harness();h.run('newRequest()');const f=h.form('request-form',{question:'Original request',repo:'synthetic/minimal-ui',context:'',path:''});
    const first=h.run('submitRequest(currentForm)');const payload=h.requests[0].data;
    h.requests[0].reject(new Error('Response lost'));await first;
    const second=h.run('submitRequest(currentForm)');assert.deepEqual(h.requests[1].data,payload);
    h.requests[1].resolve({task_id:'repeated-run'});await h.tick();h.requests[2].resolve({node_id:'one-node'});await second;
    assert(payload.client_key);assert.equal(f.error.hidden,true);
  });
  await test('scope response keeps the run incomplete and missing facts blank, including prototype-like keys',async()=>{
    const h=harness();h.run('newRequest()');const f=h.form('request-form',{question:'Scope-sensitive prompt',repo:'synthetic/minimal-ui',context:'Current context',path:'src/example.py'});
    const before=h.context.location.hash,pending=h.run('submitRequest(currentForm)');h.requests[0].resolve({task_id:'scope-run'});await h.tick();
    const initial=h.requests[1].data;h.requests[1].resolve({status:'needs_scope_clarification',scope_clarifications:[{
      missing_keys:['customer','__proto__'],source_facts:JSON.parse('{"customer":"HISTORICAL CUSTOMER","__proto__":"HISTORICAL PROTOTYPE VALUE"}')}]});await pending;
    assert.equal(h.context.location.hash,before);assert.equal(h.get('#modal').open,true);assert.equal(h.run('requestDraft.complete'),false);
    assert.equal(f.form.dataset.taskId,'scope-run');assert.equal(h.notices.length,0);
    assert.match(f.scope.innerHTML,/data-fact-key="customer"[^>]* value=""/);assert.match(f.scope.innerHTML,/data-fact-key="__proto__"[^>]* value=""/);
    assert.doesNotMatch(f.scope.innerHTML,/HISTORICAL CUSTOMER|HISTORICAL PROTOTYPE VALUE/);
    assert.match(f.progress.innerHTML,/Open blocked run/);assert.equal(h.run('requestDraft.scopeEditable'),true);
    assert.doesNotMatch(f.scope.innerHTML,/<input[^>]*\brequired\b/);
    f.setFacts([['customer',''],['__proto__','']]);const empty=h.run('submitRequest(currentForm)');assert.equal(h.requests.length,3);
    assert(!('facts' in h.requests[2].data),'leaving unknown scope blank does not invent facts');
    h.requests[2].resolve({status:'needs_scope_clarification',scope_clarifications:[{missing_keys:['customer','__proto__']}]});await empty;
    f.setFacts([['customer','Current Cedar'],['__proto__','Current explicit fact']]);const retry=h.run('submitRequest(currentForm)');
    assert.equal(h.requests.length,4);const payload=h.requests[3].data;
    assert.equal(payload.client_ref,initial.client_ref);assert.equal(payload.question,initial.question);
    assert.equal(payload.facts.customer,'Current Cedar');assert.equal(Object.hasOwn(payload.facts,'__proto__'),true);
    assert.equal(payload.facts.__proto__,'Current explicit fact');assert.equal(h.run('({}).polluted'),undefined);
    h.requests[3].reject(new Error('Scope retry response lost'));await retry;
    assert.equal(h.run('requestDraft.scopeEditable'),false);f.setFacts([['customer','Changed after lost response'],['__proto__','Changed']]);
    const third=h.run('submitRequest(currentForm)');assert.deepEqual(h.requests[4].data,payload);
    h.requests[4].resolve({node_id:'scope-node'});await third;
    assert.equal(h.run('requestDraft'),null);assert.equal(h.get('#modal').open,false);assert.match(h.context.location.hash,/runs\/scope-run$/);
    assert.equal(h.requests.filter(x=>x.url==='/api/tasks/start').length,1);
  });
  await test('scope retry sends only supplied facts and clearing a currently missing key removes its old value',async()=>{
    const h=harness();h.run('newRequest()');const f=h.form('request-form',{question:'Optional facts only',repo:'synthetic/minimal-ui',context:'',path:''});
    const first=h.run('submitRequest(currentForm)');h.requests[0].resolve({task_id:'partial-facts'});await h.tick();
    h.requests[1].resolve({status:'needs_scope_clarification',scope_clarifications:[{missing_keys:['customer','release']}]});await first;
    f.setFacts([['customer','Actual Cedar'],['release','']]);const second=h.run('submitRequest(currentForm)');
    assert.deepEqual(Object.keys(h.requests[2].data.facts),['customer']);assert.equal(h.requests[2].data.facts.customer,'Actual Cedar');
    h.requests[2].resolve({status:'needs_scope_clarification',scope_clarifications:[{missing_keys:['release']}]});await second;
    f.setFacts([['release','']]);const third=h.run('submitRequest(currentForm)');
    assert.equal(h.requests[3].data.facts.customer,'Actual Cedar','actual nonmissing facts are retained');assert(!('release' in h.requests[3].data.facts));
    h.requests[3].resolve({status:'needs_scope_clarification',scope_clarifications:[{missing_keys:['customer']}]});await third;
    f.setFacts([['customer','']]);const fourth=h.run('submitRequest(currentForm)');assert(!('facts' in h.requests[4].data));
    h.requests[4].resolve({node_id:'partial-facts-node'});await fourth;
  });
  await test('namespace-only scope response stays blocked with a run link and no fabricated anchors',async()=>{
    const h=harness();h.run('newRequest()');const f=h.form('request-form',{question:'Needs source namespace',repo:'synthetic/minimal-ui',context:'',path:''});
    const pending=h.run('submitRequest(currentForm)');h.requests[0].resolve({task_id:'namespace-run'});await h.tick();
    h.requests[1].resolve({status:'needs_scope_clarification',scope_clarifications:[{
      missing_source_namespaces:[{provider:'synthetic-provider',namespace:'synthetic-team'}],source_facts:{customer:'DO NOT PREFILL'}}]});await pending;
    assert.equal(h.run('requestDraft.complete'),false);assert.equal(h.notices.length,0);assert.equal(h.get('#modal').open,true);
    assert.match(f.scope.innerHTML,/synthetic-provider/);assert.match(f.scope.innerHTML,/cannot create source anchors/);
    assert.doesNotMatch(f.scope.innerHTML,/data-fact-key|DO NOT PREFILL/);assert.match(f.progress.innerHTML,/#runs\/namespace-run/);
    const retry=h.run('submitRequest(currentForm)');assert(!('facts' in h.requests[2].data));assert(!('source_namespaces' in h.requests[2].data));
    h.requests[2].resolve({status:'needs_scope_clarification',scope_clarifications:[{missing_source_namespaces:['synthetic-team']}]});await retry;
    assert.equal(h.run('requestDraft.complete'),false);assert.equal(h.context.location.hash,'#inbox');
  });
  await test('unexpected successful HTTP response without a node ID is recoverable and never completion',async()=>{
    const h=harness();h.run('newRequest()');const f=h.form('request-form',{question:'Incomplete response',repo:'synthetic/minimal-ui',context:'',path:''});
    const pending=h.run('submitRequest(currentForm)');h.requests[0].resolve({task_id:'no-node-id'});await h.tick();
    h.requests[1].resolve({status:'pending'});await pending;
    assert.equal(h.run('requestDraft.complete'),false);assert.equal(h.get('#modal').open,true);assert.equal(h.notices.length,0);
    assert.match(f.error.textContent,/No question ID/);assert.equal(f.button.disabled,false);assert.equal(f.form.dataset.taskId,'no-node-id');
  });
  for(const change of ['new-modal','new-hash']) await test('late Ask success respects '+change,async()=>{
    const h=harness();h.run('newRequest()');h.form('request-form',{question:'An old request',repo:'synthetic/minimal-ui',context:'',path:''});
    const pending=h.run('submitRequest(currentForm)');h.requests[0].resolve({task_id:'old-request'});await h.tick();
    if(change==='new-modal')h.run("closeModal(); openModal('Newer dialog','','Newer content')");
    else h.context.location.hash='#runs/newer-run';
    const hash=h.context.location.hash,html=h.get('#modal-content').innerHTML;
    h.requests[1].resolve({node_id:'old-node'});await pending;
    assert.equal(h.context.location.hash,hash);assert.equal(h.get('#modal-content').innerHTML,html);
    if(change==='new-modal')assert.equal(h.get('#modal').open,true);
  });
  await test('Ask response cannot redirect after navigation away and back to the same hash',async()=>{
    const h=harness();h.run('newRequest()');h.form('request-form',{question:'An old request',repo:'synthetic/minimal-ui',context:'',path:''});
    const pending=h.run('submitRequest(currentForm)');h.requests[0].resolve({task_id:'old-request'});await h.tick();
    h.context.location.hash='#runs';h.run('navigate()');h.context.location.hash='#inbox';h.run('navigate()');
    h.requests[1].resolve({node_id:'old-node'});await pending;
    assert.equal(h.context.location.hash,'#inbox');assert.equal(h.get('#modal').open,true);
  });
  await test('late Ask failure never writes error text into a newer dialog',async()=>{
    const h=harness();h.run('newRequest()');h.form('request-form',{question:'An old request',repo:'synthetic/minimal-ui',context:'',path:''});
    const pending=h.run('submitRequest(currentForm)');h.run("closeModal();openModal('New dialog','','Keep this untouched')");
    const next=h.form('answer-form',{text:'New answer'});next.error.textContent='New dialog validation';next.error.hidden=true;
    h.requests[0].reject(new Error('Old start failure'));await pending;
    assert.equal(next.error.textContent,'New dialog validation');assert.equal(next.error.hidden,true);assert.equal(h.get('#modal').open,true);
    assert.equal(h.run('requestDraft.error'),'Old start failure');
  });
  await test('Ask rejects unknown, read-only, invalid repository and oversized prompt without posting',async()=>{
    for(const problem of ['unknown','viewer','repo','no-repos','long']) {
      const h=harness();h.run('newRequest()');h.form('request-form',{question:problem==='long'?'x'.repeat(2001):'Question',repo:problem==='repo'?'unavailable/repo':'synthetic/minimal-ui',context:'',path:''});
      if(problem==='unknown')h.run('state.auth=null');
      if(problem==='viewer')h.run("state.auth={enabled:true};state.me={role:'viewer'}");
      if(problem==='no-repos')h.run('state.graph.repos=[]');
      await h.run('submitRequest(currentForm)');assert.equal(h.requests.length,0,problem);
    }
  });
  await test('Ask rejects detached, replaced, or closed forms before starting a request',async()=>{
    for(const problem of ['detached','replaced','closed']) {
      const h=harness();h.run('newRequest()');const original=h.form('request-form',{question:'Question',repo:'synthetic/minimal-ui',context:'',path:''});
      if(problem==='detached')h.detach('#request-form');
      if(problem==='replaced') {h.form('request-form',{question:'Replacement question',repo:'synthetic/minimal-ui',context:'',path:''});h.context.currentForm=original.form;}
      if(problem==='closed')h.run('closeModal()');
      await h.run('submitRequest(currentForm)');assert.equal(h.requests.length,0,problem);
    }
  });
  await test('note errors belong to their original run and never to a newer dialog',async()=>{
    const h=harness();h.selectTask(fixture.tree());const f=h.form('note-form',{text:'Retain my note'},{id:'synthetic-run-a'});
    const pending=h.run('submitTaskNote(currentForm)');const duplicate=h.run('submitTaskNote(currentForm)');
    assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,'/api/tasks/synthetic-run-a/notes');
    h.run("openModal('Newer dialog','','Leave this open')");const other=h.form('answer-form',{text:'Unrelated answer'});
    h.selectTask(fixture.tree('synthetic-run-b'));h.requests[0].reject(new Error('Run A note failed'));await pending;await duplicate;
    assert.equal(other.error.hidden,false);assert.equal(other.error.textContent,'');assert.equal(other.button.disabled,false);
    assert.equal(h.get('#modal').open,true);assert.equal(f.form.fields.text.value,'Retain my note');
    h.selectTask(fixture.tree());const html=h.run('taskOverview()');
    assert.equal(h.run("taskView('synthetic-run-a').note"),'Retain my note');assert.match(html,/Run A note failed/);
    h.run('restoreTaskView()');assert.equal(h.get('#note-text').value,'Retain my note');
  });
  await test('successful note does not close a newer dialog or erase a newer draft',async()=>{
    const h=harness();h.selectTask(fixture.tree());const f=h.form('note-form',{text:'Submitted note'},{id:'synthetic-run-a'});
    const pending=h.run('submitTaskNote(currentForm)');f.form.fields.text.value='Newer unsent note';
    h.run("openModal('Newer dialog','','Unrelated')");h.requests[0].resolve({ok:true});await pending;
    assert.equal(h.get('#modal').open,true);assert.equal(f.form.fields.text.value,'Newer unsent note');
  });
  for(const outcome of ['success','error'])await test('legacy modal note '+outcome+' preserves the simultaneous run-page draft',async()=>{
    const h=harness();h.selectTask(fixture.tree());const page=h.form('note-form',{text:'Keep page draft'},{id:'synthetic-run-a'});
    h.run("taskView().note='Keep page draft';openModal('Legacy run details','','Modal note form')");
    const modal=h.form('note-form',{text:'Submit modal note'},{id:'synthetic-run-b'},'modal');
    const pending=h.run('submitTaskNote(currentForm)'),duplicate=h.run('submitTaskNote(currentForm)');
    assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,'/api/tasks/synthetic-run-b/notes');
    assert.equal(h.requests[0].data.text,'Submit modal note');
    if(outcome==='success')h.requests[0].resolve({ok:true});else h.requests[0].reject(new Error('Modal note failed'));
    await pending;await duplicate;assert.equal(page.form.fields.text.value,'Keep page draft');
    assert.equal(h.run('taskView().note'),'Keep page draft');assert.equal(h.get('#modal').open,true);
    assert.equal(modal.button.disabled,false);
    if(outcome==='success')assert.equal(modal.form.fields.text.value,'');
    else {assert.equal(modal.form.fields.text.value,'Submit modal note');assert.match(modal.error.textContent,/Modal note failed/);assert.equal(modal.error.hidden,false);}
  });
  for(const outcome of ['success','error'])await test('late legacy modal note '+outcome+' does not touch its replacement dialog',async()=>{
    const h=harness();h.selectTask(fixture.tree());const page=h.form('note-form',{text:'Page draft'},{id:'synthetic-run-a'});
    h.run("openModal('Old modal','','')");h.form('note-form',{text:'Old modal note'},{id:'synthetic-run-a'},'modal');
    const pending=h.run('submitTaskNote(currentForm)');h.run("closeModal();openModal('New modal','','Newer modal')");
    const newer=h.form('note-form',{text:'New modal draft'},{id:'synthetic-run-b'},'modal');newer.error.hidden=true;
    if(outcome==='success')h.requests[0].resolve({ok:true});else h.requests[0].reject(new Error('Old error'));
    await pending;assert.equal(newer.form.fields.text.value,'New modal draft');assert.equal(newer.error.hidden,true);
    assert.equal(newer.error.textContent,'');assert.equal(newer.button.disabled,false);assert.equal(page.form.fields.text.value,'Page draft');
    assert.equal(h.get('#modal').open,true);
  });
  await test('run-page note success clears only its own submitted input beside a modal note with the same ID',async()=>{
    const h=harness();h.selectTask(fixture.tree());const page=h.form('note-form',{text:'Submitted page note'},{id:'synthetic-run-a'});
    const pending=h.run('submitTaskNote(currentForm)');h.run("openModal('Legacy run details','','')");
    const modal=h.form('note-form',{text:'Keep modal draft'},{id:'synthetic-run-b'},'modal');
    h.requests[0].resolve({ok:true});await pending;
    assert.equal(page.form.fields.text.value,'');assert.equal(modal.form.fields.text.value,'Keep modal draft');assert.equal(h.get('#modal').open,true);
  });
  await test('unknown access, viewer, read failure, and revoked access are explicit',()=>{
    const h=harness();h.selectTask(fixture.tree());h.run('state.auth=null');assert.match(h.run('taskOverview()'),/Loading workspace access/);
    h.run("state.auth={enabled:true};state.me={role:'viewer'}");let html=h.run('taskOverview()');
    assert.doesNotMatch(html,/id="note-form"|Add note|read-only/i,'a viewer sees the notes, not a form or a sentence about access');
    h.run("taskDetail=null;taskError='Access revoked'");html=h.run('taskOverview()');
    assert.match(html,/Access revoked/);assert.doesNotMatch(html,/Synthetic question/);
  });
  await test('zero-node scope clarification is a completion blocker and shows next action',()=>{
    const h=harness();h.selectTask(fixture.tree('scope-run',{nodes:[],counts:{blocking:1},
      scope_clarifications:[{question:'Confirm synthetic customer scope',missing_keys:['customer'],client_ref:'scope-only'}],
      next:'Confirm the missing customer scope before asking a person.'}));const html=h.run('taskOverview()');
    assert.match(html,/Scope needed · 1/);assert.match(html,/customer/);assert.match(html,/Needs task facts/);
    assert.doesNotMatch(html,/No recorded decision is blocking|In progress/);
  });
  await test('overview renders compact disclosures and distinguishes rule, human, stale authority',()=>{
    const h=harness();h.selectTask(fixture.tree('authority-run',{nodes:[
      fixture.node('rule',{authorized:true,blocking:false,signoff:'rule',answer:'Rule answer',required_signers:[],signatures:[]}),
      fixture.node('human',{authorized:true,blocking:false,signoff:'signed',signed_by:'Human Reviewer',signatures:['Human Reviewer'],answer:'Human answer'}),
      fixture.node('stale',{authorized:true,needs_review:true,blocking:true,signoff:'signed',signed_by:'Historical Reviewer',answer:'Stale answer'})]}));
    const html=h.run('taskOverview()');assert.match(html,/<details[^>]+class="[^"]*task-question/);
    assert.match(html,/Covered by a rule/);assert.match(html,/Signed by Human Reviewer/);assert.match(html,/Needs a second look/);
    assert.doesNotMatch(html,/Standing rule|Signed answer|Authorized|Not authorized|Review needed/,'row status is one sentence, never a term');
    const stale=h.run('taskDecision(syntheticDetail.tree.nodes[2])');assert.doesNotMatch(stale,/is-signed/);
  });
  await test('an owner who has not joined is said so on the task page, and only there',()=>{
    const unjoined=fixture.node('waiting',{owner:'Dana Okafor',required_signers:['Dana Okafor'],owner_account:{state:'none',person_id:'p9',email:'',name:'Dana Okafor'}});
    const h=harness();h.selectTask(fixture.tree('unjoined-run',{nodes:[unjoined]}));
    const html=h.run('taskOverview()');
    assert.doesNotMatch(html,/task-status-line/,'with one question its row carries the pill; the header does not repeat it');
    assert.match(html,/<summary id="finding-waiting-summary">[\s\S]*?Waiting on Dana Okafor</,'the row pill names who it waits on');
    assert.match(html,/Dana Okafor has not joined yet, so nobody can sign this\./);
    assert.match(html,/data-action="invite-owner" data-person="p9"[^>]*>Invite Dana</);
    assert.match(html,/data-action="review" data-id="waiting">Hand on</);
    assert.match(h.run('taskDecision(syntheticDetail.tree.nodes[0])'),/Dana Okafor · not in Raven yet/);
    h.run("state.me={name:'Member',role:'member'}");const member=h.run('taskOverview()');
    assert.match(member,/has not joined yet/);assert.doesNotMatch(member,/Invite Dana|>Hand on</,'only an administrator can invite or hand on');
    const joined=harness();joined.selectTask(fixture.tree('joined-run',{nodes:[fixture.node('waiting',{owner:'Dana Okafor',required_signers:['Dana Okafor']})]}));
    assert.doesNotMatch(joined.run('taskOverview()'),/not in Raven yet|has not joined|Invite Dana/);
    assert.match(joined.run('taskOverview()'),/Waiting on Dana Okafor</);
    // Two questions: the header names who the task waits on, above the rows.
    const two=harness();two.selectTask(fixture.tree('two-run',{nodes:[fixture.node('waiting',{owner:'Dana Okafor',required_signers:['Dana Okafor']}),fixture.node('other',{owner:'Dana Okafor',required_signers:['Dana Okafor']})]}));
    assert.match(two.run('taskOverview()'),/<p class="task-status-line" role="status"><span class="pill waiting">(?:<span class="status-dot"><\/span>)?Waiting on Dana Okafor<\/span><\/p>/);
  });
  await test('a finished task reads Done, with its signed answer under Decided',()=>{
    const h=harness();h.selectTask(fixture.tree('finished-run',{status:'completed',counts:{blocking:0,total:1},
      nodes:[fixture.node('settled',{blocking:false,authorized:true,answer:'Keep the default.',signoff:'signed',signatures:['Synthetic Reviewer']})]}));
    const html=h.run('taskOverview()');
    assert.match(html,/<p class="task-status-line" role="status"><span class="pill done">(?:<span class="status-dot"><\/span>)?Done<\/span><\/p>/);
    assert.doesNotMatch(html,/Completed|Reported complete|not verified|Next:|Workflow details|Observed|blocker|authorized answers|recorded questions/);
    assert.match(html,/id="resolved-settled"/);assert.match(html,/Signed by Synthetic Reviewer/);assert.match(html,/No open questions\./);
    assert.doesNotMatch(html,/Nothing decided yet/);
    assert.equal(h.run("runStatusLabel('completed')"),'Done','the Tasks list pill says the same word');
  });
  await test('a closed, failed or finished task says so in its pill once nobody is waited on; a person owed comes first, as on the row',()=>{
    for(const [status,label] of [['failed','Failed'],['cancelled','Closed'],['abandoned','Closed'],['completed','Done']]) {
      // Nothing waits on a person: the lifecycle word, with the failure pointer only for a failed run.
      const settled=harness();settled.selectTask(fixture.tree('lifecycle-run',{status,
        nodes:[fixture.node('settled',{blocking:false,authorized:true,answer:'Keep it.',signoff:'signed',signatures:['Synthetic Reviewer']})]}));
      const html=settled.run('taskOverview()');const line=html.match(/<p class="task-status-line"[^>]*>([\s\S]*?)<\/p>/)[1];
      assert.match(line,new RegExp('>'+label+'</span>'),status+' lifecycle shows when nothing waits');
      assert.equal((line.match(/class="pill /g)||[]).length,1,'one pill');
      if(status==='failed')assert.match(line,/See History for the failure\./);else assert.doesNotMatch(line,/See History/);
      assert.doesNotMatch(html,/blocker|authorized answers|Waiting for decisions|Review needed|Cancelled/);
      // A question still owed (a late co-signer, a correction upstream): the page says who, as the Tasks row does for the same run.
      for(const stale of [false,true]) {const h=harness();h.selectTask(fixture.tree('lifecycle-run',{status,
        nodes:[fixture.node('unfinished',{needs_review:stale,authorized:stale,signoff:stale?'signed':'required'}),fixture.node('second',{needs_review:stale,authorized:stale,signoff:stale?'signed':'required'})]}));
        const open=h.run('taskOverview()').match(/<p class="task-status-line"[^>]*>([\s\S]*?)<\/p>/)[1];
        assert.match(open,stale?/>Needs a second look</:/>Waiting on Synthetic Reviewer</,status+' with a person owed names them');
        assert.doesNotMatch(open,new RegExp('>'+label+'</span>'),'not Done beside a question under Waiting');
        h.run("state.runs=[{id:'lifecycle-run',status:"+JSON.stringify(status)+"}];state.decisions=[{id:'unfinished',run_id:'lifecycle-run',status:"+JSON.stringify(stale?'approved':'pending')+",needs_review:"+stale+",owner_name:'Synthetic Reviewer'}]");
        assert.equal(h.run('runStatusSentence(state.runs[0],state.decisions).text'),'Waiting on Synthetic Reviewer','the row waits on the same person');
      }
    }
  });
  await test('reused sources identify origin without implying new current-run contact',()=>{
    for(const [source,label] of [['record','Source record'],['memory','Earlier decision'],['human','Recorded human answer'],['agent','Agent proposal']]) {
      const h=harness();h.selectTask(fixture.tree('origins-run',{nodes:[fixture.node('reused',{
        source,source_id:'synthetic-source-node',answered_by:'Historical Author',answer:'Earlier answer'})]}));
      const html=h.run('taskOverview()');assert(html.includes('Recorded answer source:</strong> '+label),label+' is named inside Details');
      assert.match(html,source==='memory'?/Earlier answer respondent/:/Recorded answer respondent/);
      const summary=html.match(/<summary id="finding-reused-summary">([\s\S]*?)<\/summary>/)[1];
      assert.match(summary,/Waiting for sign-off/);assert.doesNotMatch(summary,new RegExp(label),'the source label stays out of the row summary');
      assert.doesNotMatch(html,/Not authorized|Message sent to Historical Author|Signed by Historical Author|Answered by Historical Author/);
      assert.doesNotMatch(html,/does not imply a new interaction|not live verification|do not grant approval|approval controls/,'the source disclaimer is gone');
    }
  });
  await test('memory source links expose exact recorded pins and a current-record action',()=>{
    const h=harness();h.selectTask(fixture.tree('pins-run',{nodes:[fixture.node('dependent',{
      source:'memory',source_id:'historical-decision',source_revision:'recorded-revision-07',
      related:[{kind:'uses',source_decision_id:'pinned-source',source_version_id:'exact-source-version',
        decision_id:'dependent',decision_version_id:'exact-dependent-version',context:{flag:false,count:0}}]})]}));
    const html=h.run('taskOverview()');
    assert.match(html,/data-action="source" data-id="historical-decision"/);
    for(const literal of ['recorded-revision-07','pinned-source','exact-source-version','exact-dependent-version'])assert(html.includes(literal),literal);
    assert.match(html,/current record/);assert.match(html,/pinned relationship/);
    assert.equal(h.requests.length,0,'reading pin references must not infer or fetch another chain');
  });
  await test('source refresh and review-needed retain distinct labels and no current authorization',()=>{
    const h=harness();h.selectTask(fixture.tree('freshness-run',{nodes:[
      fixture.node('refresh',{authorized:true,source_refresh_required:true,signoff:'signed',answer:'Earlier answer'}),
      fixture.node('review',{authorized:true,needs_review:true,signoff:'rule',answer:'Earlier rule answer'})]}));
    const html=h.run('taskOverview()');assert.match(html,/Needs fresh evidence/);assert.match(html,/Needs a second look/);
    assert.match(html,/id="finding-refresh"/);assert.match(html,/id="finding-review"/);assert.doesNotMatch(html,/id="resolved-/,'neither answer is listed as decided');
    assert.match(html,/Review current evidence and applicability before using this answer\./);
    assert.doesNotMatch(html,/Evidence refresh needed|Review needed|does not authorize use|id="learned-(?:refresh|review)"/);
  });
  await test('one overview list exposes unapproved findings once with separate approval and source origin',()=>{
    const h=harness();const long='A'.repeat(600)+' exact answer tail';
    const nodes=[fixture.node('record',{source:'record',answer:'Unapproved source finding',blocking:false}),
      fixture.node('memory',{source:'memory',source_id:'earlier',answer:'Unapproved prior answer',blocking:false}),
      fixture.node('signed',{source:'human',answer:'Signed answer',authorized:true,blocking:false,signoff:'signed',signatures:['Synthetic Reviewer']}),
      fixture.node('long',{source:'record',answer:long,blocking:true})];
    h.selectTask(fixture.tree('all-findings',{nodes}));const html=h.run('taskOverview()');
    assert.match(html,/<h2>Waiting<\/h2>/);assert.match(html,/<h2>Decided<\/h2>/);assert.doesNotMatch(html,/Questions &amp; findings|Questions & findings|Remaining questions|Resolved decisions/);
    for(const n of nodes)assert.equal((html.match(new RegExp('data-node-id="'+n.node_id+'"','g'))||[]).length,1,n.node_id+' appears once');
    for(const [id,label,answer] of [['record','Source record','Unapproved source finding'],['memory','Earlier decision','Unapproved prior answer']]) {
      const summary=html.match(new RegExp('<summary id="finding-'+id+'-summary">([\\s\\S]*?)</summary>'))?.[1];
      assert(summary);assert(!summary.includes(label),'origin lives inside Details');assert(summary.includes(answer));assert.match(summary,/Waiting for sign-off/);
      assert.doesNotMatch(summary,/Not authorized/);
      assert(html.includes('<details class="task-details" id="finding-'+id+'-details">'),'one Details disclosure per question');
    }
    const summary=html.match(/<summary id="finding-long-summary">([\s\S]*?)<\/summary>/)[1];
    assert.doesNotMatch(summary,/exact answer tail/);assert.match(summary,/…/);assert(html.includes(long),'expanded detail retains full answer');
    assert.match(html,/id="resolved-signed"/);assert.match(html,/Signed by Synthetic Reviewer/);
    assert.doesNotMatch(html,/authorized answer/);
  });
  await test('a late blocker remains visible ahead of older approved findings with stable row order',()=>{
    const h=harness();const older=Array.from({length:100},(_,i)=>fixture.node('older-'+i,{
      blocking:false,authorized:true,signoff:'signed',answer:'Approved older answer',signatures:['Synthetic Reviewer']}));
    h.selectTask(fixture.tree('prioritize-blockers',{nodes:[...older,
      fixture.node('late-blocker'),fixture.node('late-review',{blocking:false,needs_review:true})]}));
    const html=h.run('taskOverview()');const ids=[...html.matchAll(/data-node-id="([^"]+)"/g)].map(match=>match[1]);
    assert.deepEqual(ids.slice(0,4),['late-blocker','late-review','older-0','older-1']);assert.equal(ids.length,14); // Open and resolved sections each have their own pagination.
  });
  await test('selected contact uses recorded owner evidence and referral trace without reranking',()=>{
    const h=harness();h.selectTask(fixture.tree('routing-run',{nodes:[fixture.node('routed',{owner:'Selected Reviewer',
      owner_evidence:'Recorded CODEOWNERS match src/example.py',answer:'Unapproved answer'})]}),fixture.trace({events:[
      {id:1,at:'2026-10-09T00:00:00Z',kind:'owner_changed',decision_id:'routed',detail:{by:'First Reviewer',referral:true,to:'Selected Reviewer',why:'Recorded referral reason'}}]}));
    const html=h.run('taskOverview()');assert.match(html,/Selected Reviewer/);assert.match(html,/Recorded CODEOWNERS match/);
    assert.equal(h.requests.length,0,'rendering must not invoke routing or candidate ranking');
    h.run("taskTab='history'");assert.match(h.run('taskOverview()'),/Recorded referral reason/);
  });
  await test('nested null/falsy/markup inputs and history do not throw or become elements',()=>{
    const h=harness();const raw='<img src=x onerror=bad()>',nested={zero:0,false:false,null:null,deep:{value:raw}};
    h.selectTask(fixture.tree('unusual-run',{facts:nested,notes:[{at:null,by:raw,text:raw}],nodes:[fixture.node('odd',{context:nested,owner_evidence:nested})]}),
      fixture.trace({events:[null,{id:1,kind:'task_note',at:null,detail:null},{id:2,kind:'custom',at:'',detail:nested}],notifications:[null]}));
    for(const tab of ['decisions','history']) {h.run('taskTab='+JSON.stringify(tab));const html=h.run('taskOverview()');
      assert.doesNotMatch(html,/<img src=x/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/\[object Object\]/);}
  });
  await test('late task reads cannot overwrite a newer selected run',async()=>{
    const h=harness();h.selectTask(fixture.tree());h.run('render=()=>{}');const old=h.run('loadTask()');
    h.selectTask(fixture.tree('new-run'));const current=h.run('loadTask()');
    h.requests[2].resolve(fixture.tree('new-run'));h.requests[3].resolve(fixture.trace());await current;
    h.requests[0].resolve(fixture.tree());h.requests[1].resolve(fixture.trace());await old;
    assert.equal(h.run('taskDetail.tree.task_id'),'new-run');
  });
  await test('source current state is an observation and unsafe links never become anchors',()=>{
    const h=harness();h.selectTask(fixture.tree('sources-run',{nodes:[fixture.node('source',{sources:[
      {record_id:'current-doc',current:true,ref:'Observed synthetic source',url:'javascript:bad()'},
      {record_id:'stale-doc',current:false,stale:true,ref:'Historical source'}]})]}));
    const html=h.run('taskOverview()');assert.match(html,/Current observed version/);assert.doesNotMatch(html,/not live verification/);
    assert.doesNotMatch(html,/<a[^>]+href="javascript:/);assert.match(html,/Historical or unavailable version/);
  });
  await test('questions, notes, and history are bounded with explicit expansion',()=>{
    const h=harness();h.selectTask(fixture.tree('large-run',{nodes:Array.from({length:39},(_,i)=>fixture.node('q'+i)),
      notes:Array.from({length:39},(_,i)=>({by:'Synthetic',text:'Note '+i,at:'2026-10-09T00:00:00Z'}))}),
      fixture.trace({events:Array.from({length:65},(_,i)=>({id:i,kind:'task_note',at:'2026-10-09T00:00:00Z',detail:{text:'Event '+i}}))}));
    let html=h.run('taskOverview()');assert.equal((html.match(/class="task-question /g)||[]).length,12);
    assert.equal((html.match(/class="task-note"/g)||[]).length,12);assert.match(html,/data-action="task-more"/);
    h.run('render=()=>{}; taskMore("finding",12)');html=h.run('taskOverview()');
    assert.equal((html.match(/class="task-question /g)||[]).length,24);
    h.run("taskTab='history'");html=h.run('taskOverview()');assert.equal((html.match(/<li>/g)||[]).length,25);
    h.run('taskMore("history",25)');html=h.run('taskOverview()');assert.equal((html.match(/<li>/g)||[]).length,50);
  });
  await test('poll snapshots preserve per-task disclosure, draft, focus and cursor',()=>{
    const h=harness();h.selectTask(fixture.tree());const app=h.get('#app');app.dataset.taskId='synthetic-run-a';
    const f=h.form('note-form',{text:'Run A draft'},{id:'synthetic-run-a'}),disclosure=h.get('#finding-pending-a');
    disclosure.open=true;f.form.fields.text.selectionStart=2;f.form.fields.text.selectionEnd=7;
    app.querySelectorAll=()=>[disclosure];app.querySelector=selector=>selector==='#note-text'?f.form.fields.text:null;
    app.contains=el=>f.form.contains(el)||el===disclosure;h.document.activeElement=f.form.fields.text;
    h.run('rememberTaskView()');disclosure.open=false;f.form.fields.text.value='';
    h.run('restoreTaskView()');assert.equal(disclosure.open,true);assert.equal(f.form.fields.text.value,'Run A draft');
    assert.equal(h.document.activeElement,f.form.fields.text);assert.equal(f.form.fields.text.selectionStart,2);
    assert.equal(f.form.fields.text.selectionEnd,7);
    h.selectTask(fixture.tree('synthetic-run-b'));h.run('restoreTaskView()');assert.equal(f.form.fields.text.value,'');
    h.selectTask(fixture.tree());h.run('restoreTaskView()');assert.equal(f.form.fields.text.value,'Run A draft');
  });
  await test('permission failure discards the previous successful task read',async()=>{
    const h=harness();h.selectTask(fixture.tree());h.run('render=()=>{}');const pending=h.run('loadTask()');
    h.requests[0].reject(Object.assign(new Error('Access revoked'),{status:403}));h.requests[1].resolve(fixture.trace());await pending;
    assert.equal(h.run('taskDetail'),null);assert.match(h.run('taskOverview()'),/Access revoked/);
  });
  await test('transient read failure retains the last observed state with an explicit warning',async()=>{
    const h=harness();h.selectTask(fixture.tree());h.run('render=()=>{}');const pending=h.run('loadTask()');
    h.requests[0].reject(Object.assign(new Error('Temporary read failure'),{status:503}));h.requests[1].resolve(fixture.trace());await pending;
    assert.equal(h.run('taskDetail.tree.task_id'),'synthetic-run-a');
    const html=h.run('taskOverview()');assert.match(html,/Temporary read failure/);assert.match(html,/last successful read/);
  });
  await test('workspace polling 401/403 clears prior access, cached run, and an open stale dialog',async()=>{
    for(const status of [401,403]) {
      const h=harness();h.selectTask(fixture.tree());h.run("openModal('Stale dialog','','Old content')");
      const pending=h.run('realRefresh({quiet:true})');assert.equal(h.requests[0].url,'/api/state');
      h.requests[0].reject(Object.assign(new Error('Workspace access revoked '+status),{status}));await pending;
      assert.equal(h.run('state.auth'),null);assert.equal(h.run('taskDetail'),null);assert.equal(h.get('#modal').open,false);
      const html=h.get('#app').innerHTML;assert.match(html,/Workspace access revoked/);assert.doesNotMatch(html,/Loading workspace access|Synthetic question/);
    }
  });
  await test('workspace polling 503 keeps the last run snapshot and reports the failure',async()=>{
    const h=harness();h.selectTask(fixture.tree());const pending=h.run('realRefresh({quiet:true})');
    h.requests[0].reject(Object.assign(new Error('Workspace temporarily unavailable'),{status:503}));await pending;
    assert.equal(h.run('state.auth.enabled'),false);assert.equal(h.run('taskDetail.tree.task_id'),'synthetic-run-a');
    const html=h.run('taskOverview()');assert.match(html,/Workspace temporarily unavailable/);assert.match(html,/last successful read/);
  });
  await test('supplied model-pending flags keep context reading distinct from final empty or approved findings',()=>{
    const h=harness();h.selectTask(fixture.tree('reading-empty',{model_pending:true,nodes:[],scope_clarifications:[]}));
    let html=h.run('taskOverview()');assert.match(html,/<p class="task-status-line" role="status">[^]*?Reading context…/);
    assert.match(html,/<p class="task-empty">Reading context…<\/p>/);assert.doesNotMatch(html,/No open questions|In progress|No questions or findings/);
    h.selectTask(fixture.tree('reading-node',{model_pending:false,nodes:[fixture.node('reading',{
      model_pending:true,source:'record',evidence:'An observed source finding remains visible',authorized:false})]}));
    html=h.run('taskOverview()');assert.match(html,/<span class="task-reading">Reading context…<\/span>/);
    const summary=html.match(/<summary id="finding-reading-summary">([\s\S]*?)<\/summary>/)[1];
    assert.doesNotMatch(summary,/An observed source finding/,'the retrieval string never sits in a row summary');
    assert.match(html,/<p class="task-checked">What Raven checked: An observed source finding remains visible<\/p>/);
    assert.match(summary,/Waiting on Synthetic Reviewer/);assert.doesNotMatch(html,/Not authorized|Signed by/);
  });
  await test('a question row is one sentence with a name, one button, and its diagnostics behind Details',()=>{
    const h=harness();const cases=[
      [fixture.node('open'),'Waiting on Synthetic Reviewer','Answer'],
      [fixture.node('nobody',{owner:'',required_signers:[]}),'Needs a contact','Answer'],
      [fixture.node('answered',{answer:'A recorded answer'}),'Waiting for sign-off','Sign'],
      [fixture.node('signed',{authorized:true,blocking:false,signoff:'signed',answer:'Yes',signatures:['Ana Lima','Ben Ode','Cy Dao']}),'Signed by Ana Lima, Ben Ode and 1 more','View'],
      [fixture.node('rule',{authorized:true,blocking:false,signoff:'rule',answer:'Yes'}),'Covered by a rule','View'],
      [fixture.node('plain',{authorized:true,blocking:false,signoff:'',answer:'Yes'}),'Answered','View'],
      [fixture.node('review',{authorized:true,needs_review:true,signoff:'signed',answer:'Old'}),'Needs a second look','Sign'],
      [fixture.node('fresh',{authorized:true,source_refresh_required:true,signoff:'signed',answer:'Old'}),'Needs fresh evidence','Sign'],
      [fixture.node('dup',{status:'duplicate',blocking:false,duplicate_of:'other'}),'Duplicate','View'],
      [fixture.node('follow',{status:'suggested',blocking:false}),'Follow-up for the agent','View']];
    h.selectTask(fixture.tree('sentence-run',{nodes:cases.map(([n])=>n)}));
    for(const [n,text,button] of cases) {
      const status=h.run('taskStatusSentence(syntheticDetail.tree.nodes.find(x=>x.node_id==='+JSON.stringify(n.node_id)+'))');
      assert.equal(status.text,text,n.node_id);assert(['waiting','done','neutral'].includes(status.tone));
      const row=h.run('taskDecision(syntheticDetail.tree.nodes.find(x=>x.node_id==='+JSON.stringify(n.node_id)+'),"finding")');
      assert.equal((row.match(/class="pill /g)||[]).length,1,'one pill');assert.match(row,new RegExp('data-action="review" data-id="'+n.node_id+'" aria-label="'+button+': Synthetic question '+n.node_id+'">'+button+'<'),'the button names its question for a screen reader; its visible text leads');
      assert.match(row,new RegExp('<details class="task-details" id="finding-'+n.node_id+'-details"><summary id="finding-'+n.node_id+'-details-summary">Details</summary>'));
      assert.doesNotMatch(row,/Not authorized|Open decision|Evidence refresh needed|Review needed|Context only|Authorized by/);
    }
    h.context.richNode=fixture.node('rich',{rationale:'Because policy',depth:1,origin:'human',next:'Retry after sign-off',routing_reason:'CODEOWNERS',evidence:'memory: nothing similar; records: none',prediction:'Keep seven days',answer:''});
    const rich=h.run('taskDecision(richNode,"finding")');
    const [,summary,body]=rich.match(/<summary[^>]*>([\s\S]*?)<\/summary>([\s\S]*)$/);
    assert.match(summary,/Suggested: Keep seven days/);assert.doesNotMatch(summary,/memory: nothing similar|CODEOWNERS|Level 1/);
    const details=body.slice(body.indexOf('<details class="task-details"'));
    for(const text of ['Level 1 · added by a person','Recorded next action','Retry after sign-off','Selected contact','Contact, routing and referrals','CODEOWNERS','What Raven checked: memory: nothing similar; records: none'])assert(details.includes(text),text+' is inside Details');
    assert(body.slice(0,body.indexOf('<details class="task-details"')).includes('Why: Because policy'),'the rationale sits above the button');
    h.context.pair=[fixture.node('a'),fixture.node('b')];const first=h.run('taskQuestions(pair,"finding","none")');
    assert.match(first,/id="finding-a-review" class="button small primary"/);assert.match(first,/id="finding-b-review" class="button small"/);
  });
  await test('the page head is title, goal, meta line, one pill, two tabs and a More menu; nothing explains the product',()=>{
    const h=harness();const long=('A long brief line that keeps going. ').repeat(12);
    h.selectTask(fixture.tree('head-run',{goal:long,requester:'',verdict:'pass',verdict_why:'Scoped.'}));
    h.run("state.runs=[{id:'head-run',agent:'Cursor',repo:'synthetic/minimal-ui',status:'needs_judgment',updated_at:'2026-10-09T00:00:00Z'}]");
    let html=h.run('taskOverview()');
    assert.match(html,/<a class="task-back" href="#runs">← Tasks<\/a><details class="more-menu" id="task-more"><summary aria-label="More actions">More<\/summary><div class="more-menu-items"><button id="task-share" class="button text" data-action="task-share">Copy link<\/button><\/div><\/details>/);
    assert.match(html,/<p class="task-goal is-clamped" id="task-brief">A long brief line/);
    assert.match(html,/<button id="task-brief-summary" class="button text task-brief-toggle" data-action="task-brief-toggle" aria-expanded="false" aria-controls="task-brief">Show more<\/button>/);
    assert.match(html,/<p class="task-meta">synthetic\/minimal-ui · Cursor<\/p>/,'an unknown requester is left out, not announced');
    assert.doesNotMatch(html,/Requester not recorded|Nothing decided yet|No notes yet|id="task-decided"/,'empty Decided is omitted; Notes shows only the form');
    assert.match(html,/<button id="task-tab-decisions" class="tab active"[^>]*>Decisions \(1\)<\/button><button id="task-tab-history" class="tab "[^>]*>History<\/button><\/nav>/);
    assert.match(html,/<details class="task-panel task-context" id="task-context"><summary id="task-context-summary">Details<\/summary>/);
    assert.match(html,/<p id="task-kickoff"><strong>Kickoff verdict · pass<\/strong> Scoped\.<\/p>/);
    assert.match(html,/<details class="task-panel task-inquiry" id="task-inquiry"><summary id="task-inquiry-summary">Ask Raven about this task<\/summary>/);
    assert.match(html,/<h2>Notes<\/h2>[\s\S]*<div id="task-note-compose"><form id="note-form"[^>]*><label for="note-text">Note for the agent<\/label><textarea id="note-text"[^>]*placeholder="A constraint, a person to ask, a link\."/);
    for(const gone of ['Refresh','Copy app link','Open task page','Read the task brief','Workflow details','Next:','Observed','task-tab-overview','task-tab-review','Questions &amp; findings','Not authorized','Read-only','Context &amp; discussion','Add context for the agent','task-access','A link does not grant access','workflow state','Code review'])assert(!html.includes(gone),gone+' is gone');
    h.run('taskView().briefOpen=true');html=h.run('taskOverview()');
    assert.match(html,/<p class="task-goal" id="task-brief">/);assert.match(html,/aria-expanded="true"[^>]*>Show less</);
    h.selectTask(fixture.tree('same-run',{goal:'Synthetic run overview',facts:{},verdict:''}));html=h.run('taskOverview()');
    assert.doesNotMatch(html,/task-goal|task-brief|id="task-context"/,'a goal that repeats the title is not shown twice; no Details without facts');
    assert.match(html,/<p class="task-meta">Requested by Synthetic Requester · synthetic\/minimal-ui<\/p>/);
    h.run("state.model={investigation:false}");assert.doesNotMatch(h.run('taskOverview()'),/task-inquiry/);
    h.run("state.model={};state.auth={enabled:true};state.me={id:'u1',name:'Me',role:'member'};state.settings={brief_mode:'inbox'};state.executions=[{run_id:'same-run'}];state.browser_tasks=[{task_id:'same-run'}]");
    html=h.run('taskOverview()');const menu=html.match(/<div class="more-menu-items">([\s\S]*?)<\/div><\/details>/)[1];
    assert.match(menu,/Copy link.*Open my personal page.*href="#work\/same-run">Open conversation.*data-action="execution-details" data-id="same-run">Execution output and files/);
    h.run("state.settings={brief_mode:'off'}");assert.doesNotMatch(h.run('taskOverview()'),/Open my personal page/);
  });
  await test('a task with a code review keeps it as a section the warning jumps to',()=>{
    const h=harness();h.selectTask(fixture.tree('review-run',{review:{status:'done',follows:[{node_id:'pending-a',verdict:'unclear',why:'Outside the diff.',requirements:[]}]}}));
    const html=h.run('taskOverview()');
    assert.match(html,/<p class="task-warning">1 decisions need inspection after the model review\.<button class="button small" data-action="task-review">Open code review<\/button><\/p>/);
    assert.match(html,/<section class="task-panel" id="task-review"><h2 tabindex="-1">Code review<\/h2>/);assert.match(html,/Outside the diff\./);
    assert.doesNotMatch(html,/does not replace tests|No code review yet|Code review against decisions/);
    assert.doesNotMatch(h.run('taskOverview()').replace(/<section class="task-panel" id="task-review">[\s\S]*?<\/section>/,''),/Outside the diff/);
    h.selectTask(fixture.tree('no-review-run'));assert.doesNotMatch(h.run('taskOverview()'),/Code review/);
  });
  await test('the Tasks list is rows with one pill and no table; its buttons sit in the page head',()=>{
    const h=harness();const now=new Date().toISOString();
    h.run("state.runs=[{id:'r1',title:'Tighten tokens',repo:'acme/platform',agent:'Cursor',status:'needs_judgment',updated_at:"+JSON.stringify(now)+"},{id:'r2',title:'Old job',repo:'acme/platform',agent:'Codex',status:'completed',updated_at:"+JSON.stringify(now)+"},{id:'r3',title:'Lost job',repo:'acme/platform',agent:'Codex',status:'failed',updated_at:"+JSON.stringify(now)+"},{id:'r4',title:'Stopped',repo:'acme/platform',agent:'Codex',status:'cancelled',updated_at:"+JSON.stringify(now)+"},{id:'r5',title:'Orphan',repo:'acme/billing',agent:'Codex',status:'needs_judgment',updated_at:"+JSON.stringify(now)+"}];"
      +"state.decisions=[{id:'d1',run_id:'r1',status:'pending',owner_name:'Alex Morgan'},{id:'d2',run_id:'r1',status:'pending',owner_name:'Wes Chen'},{id:'d3',run_id:'r1',status:'pending',owner_name:'Marisol Vega'},{id:'d4',run_id:'r5',status:'pending',owner_name:''}];state.browser_tasks=[{task_id:'r2'},{task_id:'b1',prompt:'Browser job',repository:'acme/web',status:'running',updated_at:"+JSON.stringify(now)+"}];state.model={proposals:false};");
    let html=h.run('runs()');const head=h.get('#page-actions').innerHTML;
    assert.doesNotMatch(html,/<table|View run|What Raven saved|Tasks in this workspace/);
    assert.equal((html.match(/<a class="task-row"/g)||[]).length,6);assert.equal((html.match(/class="pill /g)||[]).length,6,'one pill per row');
    assert.match(html,/<a class="task-row" href="#runs\/r1"><div><strong>Tighten tokens<\/strong><span class="muted">acme\/platform · Cursor · just now<\/span><\/div><span class="pill waiting">(?:<span class="status-dot"><\/span>)?Waiting on Alex Morgan, Wes Chen and 1 more<\/span><\/a>/);
    assert.match(html,/href="#work\/r2"><div><strong>Old job[\s\S]*?pill done">(?:<span class="status-dot"><\/span>)?Done</);
    assert.match(html,/Lost job[\s\S]*?pill neutral">(?:<span class="status-dot"><\/span>)?Failed</);assert.match(html,/Stopped[\s\S]*?>Closed</);
    assert.match(html,/Orphan[\s\S]*?>No owner yet</,'never Waiting on undefined');assert.doesNotMatch(html,/undefined/);
    assert.match(html,/<a class="task-row" href="#work\/b1"><div><strong>Browser job<\/strong><span class="muted">acme\/web · Browser · just now<\/span><\/div><span class="pill neutral">(?:<span class="status-dot"><\/span>)?In progress</);
    assert.match(head,/^<span class="page-count">6 tasks<\/span><details class="more-menu"><summary aria-label="More actions">More<\/summary><div class="more-menu-items"><button class="button text" data-action="new">Ask a question<\/button><\/div><\/details>$/);
    // Without a model backend, Outcomes stays reachable while something there waits on a person.
    h.run("state.today={proposals_open:1,objectives_needing_you:0}");h.run('runs()');assert.match(h.get('#page-actions').innerHTML,/<a href="#today">Outcomes<\/a>/);
    h.run("state.today={proposals_open:0,objectives_needing_you:2}");h.run('runs()');assert.match(h.get('#page-actions').innerHTML,/<a href="#today">Outcomes<\/a>/);
    h.run("state.today={proposals_open:0,objectives_needing_you:0}");h.run('runs()');assert.doesNotMatch(h.get('#page-actions').innerHTML,/#today/);
    h.run("delete state.today");
    h.run("state.runs=state.runs.slice(0,1);state.browser_tasks=[];state.execution_config={enabled:true};state.model={proposals:true}");html=h.run('runs()');
    assert.match(html,/<span class="muted">Cursor · just now<\/span>/,'the repository is named only when there is more than one');
    assert.match(h.get('#page-actions').innerHTML,/<span class="page-count">1 task<\/span><button class="button primary" data-action="task">Start a task<\/button><details class="more-menu">[\s\S]*Ask a question[\s\S]*<a href="#today">Outcomes<\/a>/);
    h.run("state.execution_config={};state.workbench={ready:true}");h.run('runs()');assert.match(h.get('#page-actions').innerHTML,/<a class="button primary" href="#work">Start a task<\/a>/);
    h.run("state.auth={enabled:true};state.me={role:'viewer'}");h.run('runs()');assert.doesNotMatch(h.get('#page-actions').innerHTML,/Start a task|Ask a question/);
    h.run("state.runs=[]");html=h.run('runs()');assert.match(html,/<h2>No tasks yet\.<\/h2><p>Connect a coding agent in Settings and give it a task\.<\/p><button class="button soft" data-action="help-connect">Open Settings<\/button>/);
  });
  if(failures.length)throw new Error(`${failures.length} failed; ${passed} passed:\n${failures.join('\n\n')}`);
  console.log(`Fresh minimal run UI: ${passed} checks passed (queued DOM/API doubles, not Chromium).`);
})().catch(error=>{console.error(error);process.exitCode=1;});
