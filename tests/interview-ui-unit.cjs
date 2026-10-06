/* Logic-only DOM stubs: this does not claim real browser/audio verification. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = fs.readFileSync(path.join(__dirname, '../web/interview.js'), 'utf8');
const row = () => ({id:'interview', task_id:'task', decision_id:'decision', version:1, status:'draft',
  decision_revision:'rev', transcript:'', answer:'Keep five seconds.', rationale:'Compatibility.',
  pending_response:'', capture_method:'typed', turns:[], guidance:{}, applicability:{requires:{client:'new'}},
  interviewer:'deterministic-guided-prompts', prompts:['Which timeout?', 'Which exceptions?'],
  scope:{task_id:'task',question:'Which timeout?',context:'Keep old clients.',repo:'org/repo',path:'src/client.py',paths:['src/client.py']}});
function harness(link = false) {
  const handlers = {document:{}, window:{}, dialog:{}};
  const elements = new Map();
  const get = sel => {
    if (!elements.has(sel)) elements.set(sel, {textContent:'',hidden:false,disabled:false,value:'',maxLength:12000});
    return elements.get(sel);
  };
  const dialog = {open:false, innerHTML:'', setAttribute(){}, querySelector:sel => sel === '#interview-form' ? null : get(sel),
    addEventListener:(event,fn) => handlers.dialog[event] = fn, showModal(){this.open=true;}, close(){this.open=false;}};
  const calls = [], notifications = [];
  const ctx = {
    console, Promise, Object, String, JSON, FormData:class {}, crypto:{randomUUID:()=>'random-key'},
    document:{body:{append(){}},documentElement:{lang:'en'},createElement:()=>dialog,
      addEventListener:(event,fn)=>handlers.document[event] = fn,querySelector:()=>null},
    addEventListener:(event,fn)=>handlers.window[event] = fn,
    state:link ? undefined : {auth:{enabled:true},me:{id:'person',name:'Ada Owner',kind:'session'}},
    esc:value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
    $:()=>null, notify:text=>notifications.push(text), refresh:async()=>{},
    api:async (url,body)=>{calls.push({url,body}); return body ? {...row(), version:2} : {interviews:[row()]};},
    speechSynthesis:{cancel(){ctx.cancels++;},speak(){}},cancels:0,isSecureContext:true,
    SpeechSynthesisUtterance:function(text){this.text=text;},
  };
  if (link) ctx.ravenInterviewBridge = {user:()=>({name:'Ada via task link'}),request:(...args)=>ctx.api(...args),notify:ctx.notify,refresh:ctx.refresh};
  ctx.window = ctx;
  vm.createContext(ctx); vm.runInContext(code,ctx);
  return {ctx,handlers,dialog,calls,notifications,get,run:js=>vm.runInContext(js,ctx)};
}
(async()=>{
  for (const link of [false,true]) {
    const h = harness(link);
    await h.run("openInterview('task','decision')");
    assert(h.dialog.open);
    assert.match(h.dialog.innerHTML,/Start microphone/);
    assert.match(h.dialog.innerHTML,/model provider/);
    h.run('interviewBody(interviewSession, true)');
    assert.match(h.dialog.innerHTML,link ? /personal task link/ : /signed-in identity/);
    assert.equal(h.calls.filter(c=>c.url.endsWith('/confirm')).length,0);
    h.run('interviewSession.row.answer = "<script>unsafe()</script>"; interviewBody(interviewSession, true)');
    assert(!h.dialog.innerHTML.includes('<script>unsafe'));
    assert(h.dialog.innerHTML.includes('&lt;script&gt;'));
    await h.run('closeInterview()');
    assert(!h.dialog.open);
    assert(h.cancels !== 0);
  }
  // A delayed model response preserves newer edits rather than re-rendering.
  {
    const h = harness(); await h.run("openInterview('task','decision')");
    let release;
    h.ctx.api = () => new Promise(resolve=>{release=resolve;});
    const pending = h.run('advanceInterview(interviewSession)');
    h.run('interviewSession.editVersion++');
    const next = {...row(), version:2,guidance:{mode:'model-assisted'}};
    release(next);
    assert.equal(await pending,false);
    assert.match(h.get('[data-interview-status]').textContent,/newer edits are preserved/);
    assert.equal(h.run('interviewSession.row.version'),2);
  }
  // Navigation during launch never reopens a stale dialog or creates a draft.
  {
    const h = harness(); let release;
    h.ctx.api = () => new Promise(resolve=>{release=resolve;});
    const pending = h.run("openInterview('task','decision')");
    h.handlers.window.hashchange();
    release({interviews:[]}); await pending;
    assert.equal(h.dialog.open,false);
  }
  // Every audio stop detaches callbacks before abort and cancels spoken output.
  {
    const h = harness(); await h.run("openInterview('task','decision')");
    let aborted=false;
    h.ctx.rec = {onresult:()=>{},onerror:()=>{},onend:()=>{},abort(){aborted=true;}};
    h.run('interviewSession.recognition = rec');
    h.handlers.window.pagehide();
    assert(aborted);
    assert.equal(h.ctx.rec.onresult,null);
    assert.equal(h.run('interviewSession.recognition'),null);
    assert(h.ctx.cancels > 0);
  }
  const clickConfirm = h => {
    const review = h.run('interviewSession.review');
    const button = {disabled:false,isConnected:true,dataset:{interviewAction:'confirm',
      interviewId:review.id,interviewVersion:String(review.version)}};
    return {button,click:()=>h.handlers.dialog.click({target:{closest:()=>button}})};
  };
  // Confirmation names the exact rendered interview version, including task-link mode.
  for (const link of [false,true]) {
    const h = harness(link); await h.run("openInterview('task','decision')");
    h.run('interviewBody(interviewSession, true)');
    const {click} = clickConfirm(h); await click();
    const requests = h.calls.filter(c=>c.url.endsWith('/confirm'));
    assert.equal(requests.length,1);
    assert.equal(requests[0].body.expected_version,1);
    assert.equal(requests[0].body.expected_updated_at,'rev');
    assert(!h.dialog.open);
  }
  // A save that completes while a button click waits cannot replace what was reviewed.
  {
    const h = harness(); await h.run("openInterview('task','decision')");
    h.run('interviewBody(interviewSession, true)');
    const {click} = clickConfirm(h);
    h.run('interviewSession.writes = Promise.resolve().then(() => { interviewSession.row.version = 2; interviewSession.row.answer = "Bill everything."; })');
    await click();
    assert.equal(h.calls.filter(c=>c.url.endsWith('/confirm')).length,0);
    assert(h.dialog.open);
  }
  // An old button is inert after replacement, editing, or another active interview.
  for (const mutation of [
    'interviewSession.row.version++; interviewBody(interviewSession, true)',
    'interviewBody(interviewSession, false)',
    'interviewSession.row.id="another-interview"; interviewBody(interviewSession, true)',
  ]) {
    const h = harness(); await h.run("openInterview('task','decision')");
    h.run('interviewBody(interviewSession, true)');
    const {click} = clickConfirm(h); h.run(mutation); await click();
    assert.equal(h.calls.filter(c=>c.url.endsWith('/confirm')).length,0);
  }
  // Navigation while waiting cancels submission; a late result never closes a newer interview.
  {
    const h = harness(); await h.run("openInterview('task','decision')");
    h.run('interviewBody(interviewSession, true)');
    const {click} = clickConfirm(h);
    let release; h.ctx.waiting = new Promise(resolve=>{release=resolve;});
    h.run('interviewSession.writes = waiting');
    const pending = click();
    h.run('interviewSession = null; ++interviewGeneration'); release(); await pending;
    assert.equal(h.calls.filter(c=>c.url.endsWith('/confirm')).length,0);
  }
  {
    const h = harness(); await h.run("openInterview('task','decision')");
    h.run('interviewBody(interviewSession, true)');
    const {click} = clickConfirm(h); let release;
    h.ctx.api = (url,body)=>{h.calls.push({url,body}); return new Promise(resolve=>{release=resolve;});};
    const pending = click(); for (let i=0;i<10 && !release;i++) await Promise.resolve();
    h.run('interviewSession = {...interviewSession, row:{...interviewSession.row,id:"new-active"}}');
    release({...row(),status:'confirmed',version:2}); await pending;
    assert.equal(h.run('interviewSession.row.id'),'new-active');
    assert(h.dialog.open);
  }
  console.log('Interview UI logic checks passed (DOM/audio stubs, not browser verification).');
})().catch(e=>{console.error(e);process.exitCode=1;});
