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
    body:{append(){}},documentElement:{lang:'en'},activeElement:{tagName:'BODY'},createElement:element};
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
    context.location.hash='#runs/'+tree.task_id;run("view='runs'; taskDetail=syntheticDetail; taskError=''; taskTab='overview';"); }
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
    assert.doesNotMatch(html,/id="note-form"/);assert.match(html,/read.only/i);
    h.run("taskDetail=null;taskError='Access revoked'");html=h.run('taskOverview()');
    assert.match(html,/Access revoked/);assert.doesNotMatch(html,/Synthetic question/);
  });
  await test('zero-node scope clarification is a completion blocker and shows next action',()=>{
    const h=harness();h.selectTask(fixture.tree('scope-run',{nodes:[],counts:{blocking:1},
      scope_clarifications:[{question:'Confirm synthetic customer scope',missing_keys:['customer'],client_ref:'scope-only'}],
      next:'Confirm the missing customer scope before asking a person.'}));const html=h.run('taskOverview()');
    assert.match(html,/scope/i);assert.match(html,/customer/);assert.match(html,/block|waiting|needs clarification/i);
    assert.doesNotMatch(html,/No recorded decision is blocking/);
  });
  await test('overview renders compact disclosures and distinguishes rule, human, stale authority',()=>{
    const h=harness();h.selectTask(fixture.tree('authority-run',{nodes:[
      fixture.node('rule',{authorized:true,blocking:false,signoff:'rule',answer:'Rule answer',required_signers:[],signatures:[]}),
      fixture.node('human',{authorized:true,blocking:false,signoff:'signed',signed_by:'Human Reviewer',signatures:['Human Reviewer'],answer:'Human answer'}),
      fixture.node('stale',{authorized:true,needs_review:true,blocking:true,signoff:'signed',signed_by:'Historical Reviewer',answer:'Stale answer'})]}));
    const html=h.run('taskOverview()');assert.match(html,/<details[^>]+class="[^"]*task-question/);
    assert.match(html,/standing rule/i);assert.match(html,/Human Reviewer/);assert.match(html,/Needs (?:another look|review)|Review needed/i);
    const stale=h.run('taskDecision(syntheticDetail.tree.nodes[2])');assert.doesNotMatch(stale,/is-signed/);
  });
  await test('terminal lifecycle remains visible alongside blocking or stale decision state',()=>{
    for(const [status,label] of [['failed',/Failed/i],['cancelled',/Cancelled/i],['abandoned',/Closed/i],['completed',/Reported complete/i]]) {
      for(const stale of [false,true]) {const h=harness();h.selectTask(fixture.tree('lifecycle-run',{status,
        nodes:[fixture.node('unfinished',{needs_review:stale,authorized:stale,signoff:stale?'signed':'required'})]}));
        const html=h.run('taskOverview()');assert.match(html,label,status+' lifecycle must remain visible');
        assert.match(html,/1 blocker/);assert.match(html,stale?/Review needed/:/Waiting for decisions/);
        assert.match(html,/0 authorized answers/);
      }
    }
  });
  await test('reused sources identify origin without implying new current-run contact',()=>{
    for(const [source,label] of [['record','Source record'],['memory','Earlier decision'],['human','Recorded human answer'],['agent','Agent proposal']]) {
      const h=harness();h.selectTask(fixture.tree('origins-run',{nodes:[fixture.node('reused',{
        source,source_id:'synthetic-source-node',answered_by:'Historical Author',answer:'Earlier answer'})]}));
      const html=h.run('taskOverview()');assert(html.includes(label));assert.match(html,source==='memory'?/Earlier answer respondent/:/Recorded answer respondent/);
      assert.match(html,/Not authorized/);
      assert.doesNotMatch(html,/Message sent to Historical Author|Signed by Historical Author|Answered by Historical Author/);
      assert.match(html,/does not imply a new interaction in this run/);
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
    const html=h.run('taskOverview()');assert.match(html,/Evidence refresh needed/);assert.match(html,/Review needed/);
    assert.match(html,/0 authorized answers/);assert.match(html,/Earlier sign-off does not authorize use/);
    assert.doesNotMatch(html,/id="learned-(?:refresh|review)"/);
  });
  await test('one overview list exposes unapproved findings once with separate approval and source origin',()=>{
    const h=harness();const long='A'.repeat(600)+' exact answer tail';
    const nodes=[fixture.node('record',{source:'record',answer:'Unapproved source finding',blocking:false}),
      fixture.node('memory',{source:'memory',source_id:'earlier',answer:'Unapproved prior answer',blocking:false}),
      fixture.node('signed',{source:'human',answer:'Signed answer',authorized:true,blocking:false,signoff:'signed',signatures:['Synthetic Reviewer']}),
      fixture.node('long',{source:'record',answer:long,blocking:true})];
    h.selectTask(fixture.tree('all-findings',{nodes}));const html=h.run('taskOverview()');
    assert.match(html,/Questions &amp; findings|Questions & findings/);
    for(const n of nodes)assert.equal((html.match(new RegExp('data-node-id="'+n.node_id+'"','g'))||[]).length,1,n.node_id+' appears once');
    for(const [id,label,answer] of [['record','Source record','Unapproved source finding'],['memory','Earlier decision','Unapproved prior answer']]) {
      const summary=html.match(new RegExp('<summary id="finding-'+id+'-summary">([\\s\\S]*?)</summary>'))?.[1];
      assert(summary);assert(summary.includes(label));assert(summary.includes(answer));assert.match(summary,/Not authorized/);
    }
    const summary=html.match(/<summary id="finding-long-summary">([\s\S]*?)<\/summary>/)[1];
    assert.doesNotMatch(summary,/exact answer tail/);assert.match(summary,/…/);assert(html.includes(long),'expanded detail retains full answer');
    assert.match(html,/1 authorized answer/);
  });
  await test('a late blocker remains visible ahead of older approved findings with stable row order',()=>{
    const h=harness();const older=Array.from({length:100},(_,i)=>fixture.node('older-'+i,{
      blocking:false,authorized:true,signoff:'signed',answer:'Approved older answer',signatures:['Synthetic Reviewer']}));
    h.selectTask(fixture.tree('prioritize-blockers',{nodes:[...older,
      fixture.node('late-blocker'),fixture.node('late-review',{blocking:false,needs_review:true})]}));
    const html=h.run('taskOverview()');const ids=[...html.matchAll(/data-node-id="([^"]+)"/g)].map(match=>match[1]);
    assert.deepEqual(ids.slice(0,4),['late-blocker','late-review','older-0','older-1']);assert.equal(ids.length,12);
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
    for(const tab of ['overview','history']) {h.run('taskTab='+JSON.stringify(tab));const html=h.run('taskOverview()');
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
    const html=h.run('taskOverview()');assert.match(html,/Current observed version/);assert.match(html,/not live verification/);
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
    let html=h.run('taskOverview()');assert.match(html,/Reading available context/);assert.match(html,/context reading is still pending/);
    assert.match(html,/role="status"/);assert.doesNotMatch(html,/No questions or findings recorded yet/);
    h.selectTask(fixture.tree('reading-node',{model_pending:false,nodes:[fixture.node('reading',{
      model_pending:true,source:'record',evidence:'An observed source finding remains visible',authorized:false})]}));
    html=h.run('taskOverview()');assert.match(html,/Reading available context/);assert.match(html,/An observed source finding remains visible/);
    assert.match(html,/Not authorized/);assert.doesNotMatch(html,/Human sign-off recorded/);
  });
  if(failures.length)throw new Error(`${failures.length} failed; ${passed} passed:\n${failures.join('\n\n')}`);
  console.log(`Fresh minimal run UI: ${passed} checks passed (queued DOM/API doubles, not Chromium).`);
})().catch(error=>{console.error(error);process.exitCode=1;});
