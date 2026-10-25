// Fake SDK contract; no live inference, with real worker and filesystem checks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
(async () => {
  const {run} = await import('../hosts/pi/worker.mjs');
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'raven-pi-'));
  const old = process.env.HOME;
  process.env.HOME = home;
  process.env.RAVEN_PI_API_KEY = 'test-only';
  try {
    let options, receive, listener, aborted = false;
    const events = [];
    const sdk = {
      SessionManager: {create: () => ({}), open: () => ({})},
      ModelRuntime: {create: async () => ({getModel: (provider, id) => ({provider,id}), setRuntimeApiKey: async () => {}})},
      SettingsManager: {inMemory: settings => settings}, createExtensionRuntime: () => ({}),
      createAgentSession: async value => {
        options = value;
        return {session: {
          sessionFile:path.join(home,'pi/sessions/session.jsonl'),
          getActiveToolNames: () => ['read_file'], subscribe: callback => {listener=callback;},
          prompt: async () => {
            listener({type:'turn_start'});
            const result = await value.customTools[0].execute('call-1', {path:'a.py'});
            assert.equal(JSON.parse(result.content[0].text).body, 'hello');
            listener({type:'message_end', message:{role:'assistant', content:[{type:'thinking',text:'private'},{type:'text',text:'done'}], usage:{cost:{total:0.1}}, stopReason:'stop'}});
            listener({type:'agent_settled',aborted:false});
          }, abort: async () => {aborted=true;}, dispose: () => {}
        }};
      }
    };
    const config={cwd:home, model_provider:'google', model:'fake-model', system:'safe', session_id:'',
      tools:[{name:'read_file', description:'read', input_schema:{type:'object'}}], max_turns:4, budget_usd:1, prompt:'task'};
    await run(config,sdk,event => {
      events.push(event);
      if(event.kind==='tool') receive({kind:'tool_result',id:event.call.id,result:{body:'hello'}});
    }, handler => {receive=handler; return () => {};});
    assert.deepEqual(options.tools,['read_file']);
    assert.equal(options.resourceLoader.getExtensions().extensions.length,0);
    assert.equal(options.resourceLoader.getSkills().skills.length,0);
    assert.equal(options.resourceLoader.getAgentsFiles().agentsFiles.length,0);
    assert.equal(options.model.provider,'google');
    assert.equal(events.at(-1).is_error,false);
    assert.equal(events.at(-1).cost_usd,0.1);
    assert(!JSON.stringify(events).includes('private'));
    assert(!aborted);
    const escape=path.join(home,'outside.jsonl'); fs.writeFileSync(escape,'');
    await assert.rejects(run({...config,session_id:escape},sdk,()=>{},()=>()=>{}),/session_scope/);
    console.log('Pi harness fake SDK contracts passed');
  } finally { process.env.HOME=old; delete process.env.RAVEN_PI_API_KEY; fs.rmSync(home,{recursive:true,force:true}); }
})().catch(error=>{console.error(error);process.exitCode=1;});
