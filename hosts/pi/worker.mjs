/** Pi is the execution harness; Raven alone executes the task tools. */
import readline from 'node:readline';
import path from 'node:path';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';

export async function run(config, sdk, emit, commands) {
  const home = process.env.HOME;
  const agentDir = path.join(home, 'pi');
  const sessionDir = path.join(agentDir, 'sessions');
  fs.mkdirSync(sessionDir, { recursive: true, mode: 0o700 });
  let manager;
  if (config.session_id) {
    const saved = fs.realpathSync(config.session_id);
    if (path.dirname(saved) !== fs.realpathSync(sessionDir)) throw new Error('session_scope');
    manager = sdk.SessionManager.open(saved);
  } else manager = sdk.SessionManager.create(config.cwd, sessionDir);
  const runtime = await sdk.ModelRuntime.create({allowModelNetwork:false, refreshOnCreate:false, authPath: path.join(agentDir, 'auth.json'), modelsPath: path.join(agentDir, 'models.json')});
  const provider = config.model_provider;
  const priorModel = manager.buildSessionContext?.().model;
  if (priorModel && (priorModel.provider !== provider || priorModel.modelId !== config.model)) throw new Error('session_model_changed');
  const binding = JSON.stringify({provider, model:config.model, base_url:config.base_url || '', api:config.base_url ? config.model_api : ''});
  const bindingFile = path.join(agentDir, 'model-binding.json');
  if (fs.existsSync(bindingFile) && fs.readFileSync(bindingFile,'utf8') !== binding) throw new Error('session_endpoint_changed');
  if (config.base_url) {
    const endpoint = new URL(config.base_url);
    if (!['https:','http:'].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash
        || endpoint.protocol === 'http:' && !['localhost','127.0.0.1','[::1]'].includes(endpoint.hostname)) throw new Error('unsafe_model_endpoint');
    if (!['openai-completions','openai-responses','anthropic-messages'].includes(config.model_api)) throw new Error('unsupported_model_api');
    runtime.registerProvider(provider,{name:provider,baseUrl:config.base_url,api:config.model_api,models:[{
      id:config.model,name:config.model,reasoning:false,input:['text'],contextWindow:32768,maxTokens:4096,
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0}
    }]});
  }
  const model = runtime.getModel(provider, config.model);
  if (!model) throw new Error('unknown_model');
  fs.writeFileSync(bindingFile,binding,{mode:0o600});
  await runtime.setRuntimeApiKey(provider, process.env.RAVEN_PI_API_KEY);
  const pending = new Map();
  let session, interrupted = false, failed = false, settled = false, turns = 0, cost = 0;
  let sessionPublished = false, userObserved = false, delivered = false;
  const publishSession = () => {
    if (!session?.sessionFile || !fs.existsSync(session.sessionFile)) return;
    if (!sessionPublished) { emit({kind:'session',id:session.sessionFile}); sessionPublished = true; }
    if (userObserved && !delivered) { emit({kind:'delivery',accepted:true}); delivered = true; }
  };
  const release = commands(async value => {
    if (value.kind === 'interrupt') {
      interrupted = true;
      for (const promise of pending.values()) promise.reject(new Error('interrupted'));
      pending.clear();
      if (session) await session.abort();
    } else if (value.kind === 'tool_result' && pending.has(value.id)) {
      const promise = pending.get(value.id);
      pending.delete(value.id);
      promise.resolve(value.result);
    }
  });
  const tools = config.tools.map(spec => ({
    name: spec.name, label: spec.name, description: spec.description, parameters: spec.input_schema,
    async execute(id, args, signal) {
      if (!id || pending.has(id) || interrupted || signal?.aborted) throw new Error('invalid_tool_identity');
      const result = await new Promise((resolve, reject) => {
        pending.set(id, {resolve, reject});
        emit({kind:'tool', call:{id, name:spec.name, input:args}});
      });
      return {content:[{type:'text', text:JSON.stringify(result)}], details:{}};
    }
  }));
  const resourceLoader = {
    getExtensions: () => ({extensions:[], errors:[], runtime:sdk.createExtensionRuntime()}),
    getSkills: () => ({skills:[], diagnostics:[]}), getPrompts: () => ({prompts:[], diagnostics:[]}),
    getThemes: () => ({themes:[], diagnostics:[]}), getAgentsFiles: () => ({agentsFiles:[]}),
    getSystemPrompt: () => config.system, getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [], getAppendSystemPromptSources: () => [],
    extendResources: () => {}, reload: async () => {}
  };
  try {
    const created = await sdk.createAgentSession({cwd:config.cwd, agentDir, model, modelRuntime:runtime,
      resourceLoader, tools:tools.map(tool => tool.name), customTools:tools, sessionManager:manager,
      settingsManager:sdk.SettingsManager.inMemory({compaction:{enabled:false}, retry:{enabled:false}, cacheWarming:'off'}),
      thinkingLevel:'off'});
    session = created.session;
    if (created.modelFallbackMessage) throw new Error('model_fallback_refused');
    const active = session.getActiveToolNames();
    if (active.length !== tools.length || active.some(name => !tools.some(tool => tool.name === name))) throw new Error('native_tools_refused');
    publishSession();
    session.subscribe(event => {
      if (event.type === 'message_end' && event.message?.role === 'user') {
        userObserved = true;
        // Pi persists after notifying subscribers. Publish only once native
        // storage actually exists, never an allocated-but-unsaved path.
        queueMicrotask(publishSession);
      }
      if (event.type === 'agent_settled') { settled = true; interrupted ||= Boolean(event.aborted); }
      if (event.type === 'turn_start' && ++turns > config.max_turns) {
        interrupted = true;
        void session.abort();
      }
      if (event.type === 'message_end' && event.message?.role === 'assistant') {
        const message = event.message;
        failed ||= ['error','aborted'].includes(message.stopReason);
        cost += Number(message.usage?.cost?.total || 0);
        for (const block of message.content || []) if (block.type === 'text') emit({kind:'text', text:block.text});
        if (cost >= config.budget_usd) { interrupted = true; void session.abort(); }
      }
    });
    if (!interrupted) {
      await session.prompt(config.prompt);
      if (!settled) throw new Error('missing_final_receipt');
    }
    publishSession();
    emit({kind:'result', status:interrupted ? 'interrupted' : failed ? 'failed' : 'completed',
      is_error:interrupted || failed, cost_usd:cost, turns});
  } finally {
    release();
    for (const promise of pending.values()) promise.reject(new Error('host_stopped'));
    session?.dispose();
  }
}

async function main() {
  const input = readline.createInterface({input:process.stdin});
  const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
  let started = false, receive = null, ended = false;
  const queued = [];
  const dispatch = value => { if (receive) void receive(value); else queued.push(value); };
  input.on('close', () => { ended = true; dispatch({kind:'interrupt'}); });
  input.on('line', line => {
    let value;
    try { value = JSON.parse(line); } catch { process.exitCode = 1; input.close(); return; }
    if (started) { dispatch(value); return; }
    started = true;
    import('@earendil-works/pi-coding-agent').then(sdk => run(value, sdk, emit, handler => {
      receive = handler;
      for (const command of queued.splice(0)) void handler(command);
      if (ended) void handler({kind:'interrupt'});
      return () => { receive = () => {}; };
    })).catch(() => emit({kind:'error', code:'pi_host_failed'})).finally(() => { input.close(); process.stdout.write('', () => process.exit()); });
  });
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
