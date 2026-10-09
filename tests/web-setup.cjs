// Dependency-free checks of the web onboarding flow with isolated DOM/API doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const nodes = {'#agent-client': {value: 'Cursor'}, '#agent-details': {innerHTML: ''},
  '#agent-connect-prompt': {hidden: false}};
let request;
const context = {
  state: {auth: {enabled: true}, mcp_config: {mcpServers: {bridge: {type: 'http', url: 'http://localhost:7333/mcp'}}}},
  $: key => nodes[key], esc: value => String(value), structuredClone,
  pill: (text, color) => `<span class="${color}">${text}</span>`,
  ago: () => 'just now',
  agentCards: () => '<div>Agent cards</div>',
  openModal: (...args) => { context.modal = args; },
  api: async (path, data) => { request = {path, data}; return {token: 'test-only-credential'}; },
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function sourceReplacementField('), source.indexOf('function applicabilityFields(')), context);
vm.runInContext(source.slice(source.indexOf('function showHelp()'), source.indexOf('function openModal(')), context);
vm.runInContext(source.slice(source.indexOf('function updateAgentConnectPrompt()'), source.indexOf('async function quickAgent(')), context);
vm.runInContext(source.slice(source.indexOf('function updateWorkspaceName()'), source.indexOf('async function refresh(')), context);
vm.runInContext(source.slice(source.indexOf('function deliveriesCard() {'), source.indexOf('function render() {')), context);
// The decision labels are constants; expose them to the checks below.
vm.runInContext(source.slice(source.indexOf('const kindLabels'), source.indexOf('function activity()'))
  + '\n;globalThis.labels = {statusLabel, signedNow, settledNow, needsYou, beganAs, signedAs};', context);
(async () => {
  // Explicit independence is available for human-only and unknown legacy
  // chains too; source content is never required just to retire reliance.
  for (const d of [{sources:[{record_id:'r'}]}, {sources:[],source_revalidation:{has_reliance:true}},
      {sources:[],source_reuse_state:'unknown'}]) {
    assert.match(context.sourceReplacementField(d), /name="evidence_mode" value="independent"/);
  }
  assert.equal(context.sourceReplacementField({sources:[],source_revalidation:{has_reliance:false},source_reuse_state:'human'}), '');
  // A signed prediction is signed: settled, out of Needs you, labelled by its signature, with where it began kept.
  const {statusLabel, signedNow, settledNow, needsYou, beganAs, signedAs} = context.labels;
  const prediction = {status: 'proposed', kind: 'prediction', signoff: 'required', signed_by: '', needs_review: 0};
  assert.equal(statusLabel(prediction), 'Prediction, unconfirmed');
  assert.equal(needsYou(prediction), true);
  const signedPrediction = {...prediction, signoff: 'signed', signed_by: 'Library Owner'};
  assert.equal(signedNow(signedPrediction), true);
  assert.equal(statusLabel(signedPrediction), 'Signed prediction');
  assert.equal(needsYou(signedPrediction), false);
  assert.equal(settledNow(signedPrediction), true);
  assert.equal(signedAs(signedPrediction), 'Signed · Library Owner');
  assert.equal(beganAs(signedPrediction), 'a prediction from an earlier answer');
  assert.equal(statusLabel({...signedPrediction, status: 'assumed'}), 'Signed default');
  assert.equal(signedNow({...signedPrediction, needs_review: 1}), false);
  assert.equal(statusLabel({...signedPrediction, needs_review: 1}), 'Needs review');
  assert.equal(signedAs({...signedPrediction, signoff: 'rule'}), 'Covered by a reusable rule');
  assert.match(source, /Signed answer · edit to make a correction/);
  context.showHelp();
  assert.equal(context.modal[0], 'Raven help');
  assert.match(context.modal[2], /data-action="help-connect"/);
  assert.match(context.modal[2], /check agent activity/);
  const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /<button[^>]+data-action="help"[^>]+aria-label="Setup and help"/);
  assert.match(html, /id="agent-connect-prompt"/);
  assert.match(html, /Connect an agent <span aria-hidden="true">↓<\/span>/);
  assert.doesNotMatch(html, /Connect an agent <span>↗<\/span>/);
  assert.match(source, /MCP connections do not require the local launcher/);
  context.state.agent_connections = [];
  context.state.runs = [];
  context.updateAgentConnectPrompt();
  assert.equal(nodes['#agent-connect-prompt'].hidden, false);
  context.state.agent_connections = [{label: 'Codex', last_used_at: ''}];
  context.updateAgentConnectPrompt();
  assert.equal(nodes['#agent-connect-prompt'].hidden, true);
  context.state.agent_connections = [];
  context.state.runs = [{id: 'first-run'}];
  context.updateAgentConnectPrompt();
  assert.equal(nodes['#agent-connect-prompt'].hidden, true);
  context.state.runs = [];
  assert.match(context.connections(), /Give it a task in your own words/);
  nodes['#workspace-name'] = {};
  context.state.workspace = {name: 'Actual team <workspace>'};
  context.updateWorkspaceName();
  assert.equal(nodes['#workspace-name'].textContent, 'Actual team <workspace>');
  assert.equal(nodes['#workspace-name'].title, 'Actual team <workspace>');
  context.state.workspace.name = 'Renamed workspace';
  context.updateWorkspaceName();
  assert.equal(nodes['#workspace-name'].textContent, 'Renamed workspace');
  assert.match(context.agentStatus(), /No active agent credentials/);
  context.state.agent_connections = [{label: 'Codex', last_used_at: ''}];
  assert.match(context.agentStatus(), /Waiting for first use/);
  context.state.agent_connections[0].last_used_at = '2026-09-21T12:00:00Z';
  assert.match(context.agentStatus(), /Activity detected/);
  assert.match(context.connections(), /Last seen just now/);
  assert.match(context.connections(), /aria-live="polite"/);
  context.state.me = {role: 'admin'};
  context.state.workspace = {name: '', needs_setup: true};
  assert.match(context.workspaceAccountCard(), /Create workspace/);
  assert.match(context.workspaceAccountCard(), /Invite teammate/);
  context.state.me.role = 'member';
  assert.equal(context.workspaceAccountCard(), '');
  context.state.me.role = 'admin';
  context.state.workspace = {name: 'Team workspace', needs_setup: false};
  assert.doesNotMatch(context.workspaceAccountCard(), />Create workspace</);
  const inviteField = {value: ''};
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, '../web/onboarding.js'), 'utf8'), {
    URLSearchParams, location: {hash: '#invite=test-invitation'},
    document: {getElementById: () => inviteField},
  });
  assert.equal(inviteField.value, 'test-invitation');
  assert.match(context.connections(), /GitHub/);
  assert.match(context.deliveriesCard(), /class="gray">Off/);
  // Sync through the server's GITHUB_TOKEN, no GitHub App: the card said Off while it worked.
  context.state.sync = {github: true, repos: [{repo: 'urllib3/urllib3', last_success_at: '', last_error: ''}]};
  assert.match(context.deliveriesCard(), /class="gray">Token set/);
  assert.match(context.deliveriesCard(), /no repository has synced yet/);
  context.state.sync.repos[0].last_success_at = '2026-09-29T08:00:00Z';
  assert.match(context.deliveriesCard(), /class="green">On/);
  assert.match(context.deliveriesCard(), /GITHUB_TOKEN/);
  assert.match(context.deliveriesCard(), /<strong>1<\/strong> synced repository/);
  assert.match(context.deliveriesCard(), /synced just now/);
  assert.doesNotMatch(context.deliveriesCard(), /awaiting setup/);
  context.state.sync.repos.push({repo: 'acme/app', last_success_at: '', last_error: 'GitHub 401: Bad credentials'});
  assert.match(context.deliveriesCard(), /failing to sync: acme\/app: GitHub 401: Bad credentials/);
  context.state.sync = {github: false, repos: []};
  assert.match(context.deliveriesCard(), /class="gray">Off/);
  assert.match(context.deliveriesCard(), /awaiting setup/);
  delete context.state.sync;
  context.state.github_app = {configured: true, mode: 'device', authorized: true, repositories: ['acme/app']};
  assert.match(context.deliveriesCard(), /class="green">On/);
  context.state.github_app.authorized = false;
  assert.match(context.deliveriesCard(), /class="gray">Off/);
  context.state.github_app = {configured: true, repositories: []};
  assert.match(context.deliveriesCard(), /class="gray">Off/);
  context.state.github_app.repositories = ['acme/app'];
  assert.match(context.deliveriesCard(), /class="green">On/);
  assert.match(context.deliveriesCard(), /connected repository/);
  context.state.github_app.repositories = Array.from({length: 29}, (_, i) => `acme/repository-${i}`);
  assert.match(context.deliveriesCard(), /<strong>29<\/strong> connected repositories/);
  assert.match(context.deliveriesCard(), /id="github-repositories"/);
  assert.doesNotMatch(context.deliveriesCard(), /Connected: acme/);
  // Polling must preserve expanded sections and the repository list position.
  const existing = [{id: 'local-launcher-help', open: true}, {id: 'manual-agent-setup', open: false}, {id: 'github-repositories', open: true}];
  const makeDisclosure = (item, scroll) => ({...item, list: {scrollTop: scroll}, querySelector() { return this.list; }});
  const oldSections = existing.map(item => makeDisclosure(item, 90));
  const newSections = existing.map(item => makeDisclosure({...item, open: false}, 0));
  const app = {innerHTML: '', dataset:{}};
  const renderContext = {workspaceError:'', state: {auth: {enabled: false}}, document: {activeElement: {tagName: 'BODY'}, querySelectorAll: () => oldSections, getElementById: id => newSections.find(el => el.id === id)},
    $: () => app, taskId: () => '', view: 'connect', inbox() {}, runs() {}, memory() {}, owners() {}, connections: () => 'updated'};
  vm.createContext(renderContext);
  vm.runInContext(source.slice(source.indexOf('function render() {'), source.indexOf('function openModal(')), renderContext);
  renderContext.state = {};
  renderContext.render();
  assert.match(app.innerHTML, /Loading workspace/);
  assert.equal(app.hidden, true);
  renderContext.state = {auth: {enabled: false}};
  renderContext.render();
  assert.equal(newSections[0].open, true);
  assert.equal(newSections[1].open, false);
  assert.equal(newSections[2].open, true);
  assert.equal(newSections[2].list.scrollTop, 90);
  await context.setupAgent();
  assert.equal(context.modal[0], 'Connect Cursor');
  const button = {dataset: {client: 'Cursor'}};
  await context.agentCredential(button);
  assert.equal(request.path, '/api/tokens');
  assert.equal(request.data.label, 'Cursor · web setup');
  assert.match(nodes['#agent-details'].innerHTML, /Bearer test-only-credential/);
  assert.match(nodes['#agent-details'].innerHTML, /http:\/\/localhost:7333\/mcp/);
  assert.equal(button.disabled, true);
  assert.equal(context.state.mcp_config.mcpServers.bridge.headers, undefined);
  context.state.auth.enabled = false;
  assert.match(context.connections(), /Local agent connection/);
  await assert.rejects(context.setupAgent(), /Enable server authentication/);
  // Task orientation and progressive disclosure are rendered from saved data.
  const taskSource = fs.readFileSync(require('node:path').join(__dirname, '../web/task.js'), 'utf8');
  const taskNode = {node_id:'n1', question:'Which usage counts?', answer:'Exclude internal traffic.',
    owner:'Wes', answered_by:'Wes', signed_by:'Wes', signatures:['Wes'], required_signers:[],
    authorized:true, status:'answered', signoff:'signed',
    sources:[{ref:'SRC-42',namespace:'fixture-site',role:'support',sequence:2,source_version_id:'version-2'}]};
  const fixture = {tree:{task_id:'t1',title:'Add billing',goal:'The complete original brief.',nodes:[taskNode],
    status:'completed',notes:[],facts:{}, review:{status:'done',follows:[{node_id:'n1',verdict:'unclear',why:'Existing code is outside the diff.',requirements:[]}]}},
    trace:{notifications:[],events:[{id:1,kind:'owner_approved',at:'2026-10-04T00:00:00Z',decision_id:'n1',detail:{actor:'Wes',answer:'Exclude internal traffic.'}}]}};
  let reads = 0;
  const taskContext = {fixture, state:{auth:{enabled:true},me:{role:'viewer'}},view:'runs',location:{hash:'#runs/t1'},
    document:{activeElement:{tagName:'BUTTON'}}, $: key => key === '#app' ? {contains:()=>true} : {open:false},
    api: async path => {reads++;return path.endsWith('/tree') ? fixture.tree : fixture.trace;},render(){},
    esc: context.esc, pill: context.pill,icon:()=>'',avatar:()=>'',eventLabels:{owner_approved:'Decision recorded'}};
  vm.createContext(taskContext);
  vm.runInContext(source.slice(source.indexOf('const plural ='), source.indexOf('const CLIENT_NAMES')), taskContext);
  vm.runInContext(source.slice(source.indexOf('const RUN_STATUS'), source.indexOf('const initials')), taskContext);
  vm.runInContext(fs.readFileSync(require('node:path').join(__dirname, '../web/presentation.js'), 'utf8'), taskContext);
  // task.js uses the actual shared renderer loaded by app.js in the page.
  vm.runInContext(source.slice(source.indexOf('function sourceEvidence('), source.indexOf('function sourceRevalidationFields(')), taskContext);
  vm.runInContext(taskSource + ';taskDetail=fixture;',taskContext);
  let overview = vm.runInContext('taskOverview()',taskContext);
  assert.match(overview, /Read the task brief/);
  assert.match(overview, /Open code review/);
  assert.match(overview, /Signed by Wes/);
  assert.match(overview, /Evidence & answer origin/);
  assert.match(overview, /SRC-42 · support/);
  assert.match(overview, /"sequence": 2/);
  assert.match(overview, /"namespace": "fixture-site"/);
  assert.match(overview, /"source_version_id": "version-2"/);
  assert.doesNotMatch(overview, /Existing code is outside the diff/);
  assert.doesNotMatch(overview, /id="note-form"/);
  // Stale reviews can reflect changed answers, reframed questions, or legacy
  // input metadata that was never recorded. Do not invent a specific cause.
  const keptReview = fixture.tree.review;
  for (const reason of ['its signed answer changed after this reading',
      'its question or signed decision context changed after this reading',
      'this older reading did not record its question and signed decision context']) {
    fixture.tree.review = {...keptReview, status:'stale', read_status:'done',
      stale:[{node_id:'n1',why:reason}]};
    const staleOverview = vm.runInContext('taskOverview()',taskContext);
    assert.match(staleOverview, /Review needed/);
    assert.match(staleOverview, /needs refreshing for the current questions and answers/);
    assert.match(staleOverview, /Submit the current diff again/);
    assert.doesNotMatch(staleOverview, /An answer changed after the code review/);
    assert.equal(reads,0,'Rendering stale review guidance must not issue requests');
  }
  fixture.tree.review = keptReview;
  const review = vm.runInContext("taskTab='review';taskOverview()",taskContext);
  assert.match(review, /Existing code is outside the diff/);
  assert.match(review, /Inspect requirements/);
  const timeline = vm.runInContext('taskTimeline(fixture.trace,fixture.tree.nodes)',taskContext);
  assert.match(timeline, /<strong>Wes<\/strong>/);
  await vm.runInContext('loadTask({quiet:true})',taskContext);
  assert.equal(reads,2,'Focusing a task tab must not freeze live updates');
  console.log('Web setup checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
