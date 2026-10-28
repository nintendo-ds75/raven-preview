// Optional installed SDK initialization only: no model call or provider credential.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  const root=path.join(__dirname,'../hosts/pi/node_modules/@earendil-works/pi-coding-agent');
  if(!fs.existsSync(root)){console.log('SKIP Pi installed SDK smoke: run npm ci --prefix hosts/pi');return;}
  assert.equal(JSON.parse(fs.readFileSync(path.join(root,'package.json'))).version,'1.4.0');
  const sdk=await import(path.join(root,'dist/index.js'));
  const {run}=await import('../hosts/pi/worker.mjs');
  const home=fs.mkdtempSync(path.join(os.tmpdir(),'raven-pi-sdk-'));
  const oldHome=process.env.HOME, oldKey=process.env.RAVEN_PI_API_KEY;
  process.env.HOME=home; process.env.RAVEN_PI_API_KEY='offline-test-only';
  try {
    const events=[];
    const noInference={...sdk,ModelRuntime:{create:async options=>{
      assert.equal(options.allowModelNetwork,false);
      const runtime=await sdk.ModelRuntime.create(options);
      runtime.streamSimple=()=>{throw new Error('Inference forbidden by offline SDK smoke');};
      return runtime;
    }}};
    await run({cwd:home,session_id:'',model_provider:'openai',model:'gpt-4.1',system:'safe',
      tools:[{name:'raven_read',description:'Read only via parent',input_schema:{type:'object',properties:{}}}],
      max_turns:1,budget_usd:1,prompt:'never executed'},noInference,event=>events.push(event),handler=>{
        handler({kind:'interrupt'});return ()=>{};
      });
    assert(!events.some(event=>event.kind==='session'), 'Pre-prompt stop must not save a nonexistent session');
    assert(!events.some(event=>event.kind==='delivery'));
    assert.equal(events.at(-1).status,'interrupted');
    assert.equal(events.at(-1).turns,0);
    const {createAssistantMessageEventStream}=await import(path.join(root,'../pi-ai/dist/utils/event-stream.js'));
    let calls=0, receive;
    const scriptedRuntime={...sdk,ModelRuntime:{create:async options=>{
      const runtime=await sdk.ModelRuntime.create(options);
      runtime.streamSimple=(model, context)=>{
        calls++;
        const stream=createAssistantMessageEventStream();
        const message={role:'assistant',api:model.api,provider:model.provider,model:model.id,timestamp:Date.now(),
          content:calls===1 ? [{type:'toolCall',id:'sdk-call',name:'raven_read',arguments:{}}] : [{type:'text',text:'verified'}],
          stopReason:calls===1?'toolUse':'stop',usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,
            cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
        if(calls===2)assert(context.messages.some(message=>message.role==='toolResult'));
        queueMicrotask(()=>{stream.push({type:'start',partial:message});stream.push({type:'done',reason:message.stopReason,message});});
        return stream;
      };
      return runtime;
    }}};
    const loop=[];
    await run({cwd:home,session_id:'',model_provider:'openai',model:'gpt-4.1',system:'safe',
      tools:[{name:'raven_read',description:'Read via parent',input_schema:{type:'object',properties:{}}}],
      max_turns:4,budget_usd:1,prompt:'Use parent tool'},scriptedRuntime,event=>{
        loop.push(event);
        if(event.kind==='tool')receive({kind:'tool_result',id:event.call.id,result:{body:'fixture'}});
      },handler=>{receive=handler;return ()=>{};});
    assert(loop.some(event=>event.kind==='delivery' && event.accepted));
    assert.equal(calls,2);
    assert.equal(loop.filter(event=>event.kind==='tool').length,1);
    assert.equal(loop.at(-1).status,'completed');
    assert.equal(loop.at(-1).turns,2);
    const resumed=[];
    const resumeConfig={cwd:home,session_id:loop[0].id,model_provider:'openai',model:'gpt-4.1',system:'safe',
      tools:[{name:'raven_read',description:'Read via parent',input_schema:{type:'object',properties:{}}}],
      max_turns:4,budget_usd:1,prompt:'Continue saved context'};
    await run(resumeConfig,scriptedRuntime,event=>resumed.push(event),()=>()=>{});
    assert.equal(resumed[0].id,loop[0].id);
    assert.equal(resumed.at(-1).status,'completed');
    assert.equal(calls,3);
    await assert.rejects(run({...resumeConfig,model_provider:'google'},scriptedRuntime,()=>{},()=>()=>{}),/session_model_changed/);
    process.env.HOME=path.join(home,'self-hosted');
    const local=[];
    await run({...resumeConfig,session_id:'',model_provider:'raven-local',model:'open-weight-fixture',
      base_url:'http://127.0.0.1:11434/v1',model_api:'openai-completions'},scriptedRuntime,event=>local.push(event),()=>()=>{});
    assert.equal(local.at(-1).status,'completed');
    await assert.rejects(run({...resumeConfig,session_id:'',model_provider:'raven-local',model:'open-weight-fixture',
      base_url:'https://other.invalid/v1',model_api:'openai-completions'},scriptedRuntime,()=>{},()=>()=>{}),/session_endpoint_changed/);
    const {spawnSync}=require('node:child_process');
    const child=spawnSync(process.execPath,[path.join(__dirname,'../hosts/pi/worker.mjs')],{
      input:JSON.stringify({...resumeConfig,session_id:''})+'\n'+JSON.stringify({kind:'interrupt'})+'\n',
      env:{PATH:process.env.PATH,HOME:path.join(home,'early-stop'),RAVEN_PI_API_KEY:'offline-test-only'},
      encoding:'utf8',timeout:10000});
    assert.equal(child.status,0,child.stderr);
    const receipts=child.stdout.trim().split('\n').map(line=>JSON.parse(line));
    assert.equal(receipts.at(-1).status,'interrupted');
    assert.equal(receipts.at(-1).turns,0);
    console.log('Installed Pi 1.4.0 initialization/early abort/tool loop/resume passed with scripted inference only');
  } finally {
    process.env.HOME=oldHome;
    if(oldKey===undefined)delete process.env.RAVEN_PI_API_KEY;else process.env.RAVEN_PI_API_KEY=oldKey;
    fs.rmSync(home,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
