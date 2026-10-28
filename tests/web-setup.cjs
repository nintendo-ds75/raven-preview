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
  ago: () => 'just now', plural: (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`,
  agentCards: () => '<div>Agent cards</div>', canAdminister: () => true,
  savedStrip: () => '', ownershipGraph: () => '', directory: null, loadDirectory: () => {},
  openModal: (...args) => { context.modal = args; },
  api: async (path, data) => { request = {path, data}; return {token: 'test-only-credential'}; },
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function workbenchSetupCard()'), source.indexOf("document.addEventListener('submit'", source.indexOf('function workbenchSetupCard()'))), context);
vm.runInContext(source.slice(source.indexOf('function sourceReplacementField('), source.indexOf('function applicabilityFields(')), context);
vm.runInContext(source.slice(source.indexOf('function showHelp()'), source.indexOf('function openModal(')), context);
vm.runInContext(source.slice(source.indexOf('function updateAgentConnectPrompt()'), source.indexOf('async function quickAgent(')), context);
vm.runInContext(source.slice(source.indexOf('function updateWorkspaceName()'), source.indexOf('async function refresh(')), context);
vm.runInContext(source.slice(source.indexOf('function deliveriesCard() {'), source.indexOf('function render() {')), context);
// The decision labels are constants; expose them to the checks below.
vm.runInContext(source.slice(source.indexOf('const signedNow'), source.indexOf('function activity()'))
  + '\n;globalThis.labels = {statusSentence, signedNow, settledNow, needsYou, beganAs};', context);
(async () => {
  // Explicit independence is available for human-only and unknown legacy
  // chains too; source content is never required just to retire reliance.
  for (const d of [{sources:[{record_id:'r'}]}, {sources:[],source_revalidation:{has_reliance:true}},
      {sources:[],source_reuse_state:'unknown'}]) {
    assert.match(context.sourceReplacementField(d), /name="evidence_mode" value="independent"/);
  }
  assert.equal(context.sourceReplacementField({sources:[],source_revalidation:{has_reliance:false},source_reuse_state:'human'}), '');
  // A signed prediction is signed: settled, out of Needs you, named by its signer in one sentence, with where it began kept.
  const {signedNow, settledNow, needsYou, beganAs} = context.labels;
  // Copied out of the vm realm so deep equality compares values, not prototypes.
  const statusSentence = d => ({...context.labels.statusSentence(d)});
  const prediction = {status: 'proposed', kind: 'prediction', signoff: 'required', signed_by: '', needs_review: 0, owner_name: 'Library Owner', required_signers: '[]'};
  assert.deepEqual(statusSentence(prediction), {text: 'Sign-off wanted from Library Owner', tone: 'waiting'});
  assert.equal(needsYou(prediction), true);
  const signedPrediction = {...prediction, signoff: 'signed', signed_by: 'Library Owner'};
  assert.equal(signedNow(signedPrediction), true);
  assert.deepEqual(statusSentence(signedPrediction), {text: 'Signed by Library Owner', tone: 'done'});
  assert.equal(needsYou(signedPrediction), false);
  assert.equal(settledNow(signedPrediction), true);
  assert.equal(beganAs(signedPrediction), 'a prediction from an earlier answer');
  assert.equal(statusSentence({...signedPrediction, status: 'assumed'}).text, 'Signed by Library Owner');
  assert.equal(signedNow({...signedPrediction, needs_review: 1}), false);
  assert.deepEqual(statusSentence({...signedPrediction, needs_review: 1}), {text: 'Needs a second look', tone: 'waiting'});
  assert.deepEqual(statusSentence({...signedPrediction, signoff: 'rule'}), {text: 'Covered by a rule', tone: 'done'});
  // Names, never counts or terms: an open question names its owner or says there is none;
  // a half-signed decision names who signed and who is still to sign, two names then a count.
  assert.deepEqual(statusSentence({status: 'pending', owner_name: 'Alex Morgan'}), {text: 'Waiting on Alex Morgan', tone: 'waiting'});
  assert.deepEqual(statusSentence({status: 'pending', owner_name: ''}), {text: 'No owner yet', tone: 'waiting'});
  assert.equal(statusSentence({status: 'resolved', signoff: 'required', signatures: '[{"by":"Marisol Vega"}]', required_signers: '["Marisol Vega","Wes Chen","Alex Morgan","Dana Lee"]'}).text, 'Signed by Marisol Vega, waiting on Wes Chen, Alex Morgan and 1 more');
  assert.deepEqual(statusSentence({status: 'suggested'}), {text: 'Follow-up for the agent', tone: 'neutral'});
  assert.doesNotMatch(source, /New judgment|Needs judgment|Not authorized|Sign-off wanted'/);
  assert.match(source, /<label for="correction">Correct it<\/label>/);
  // Help is one dialog: a sentence, the link to the guide, Open Settings, Close.
  context.showHelp();
  assert.equal(context.modal[0], 'Help');
  assert.match(context.modal[2], /The guide covers setup, tasks, answering and signing\./);
  assert.match(context.modal[2], /<a href="https:\/\/github\.com\/nintendo-ds75\/raven-preview\/blob\/main\/docs\/help\.md"[^>]*>Read the guide ↗<\/a>/);
  assert.match(context.modal[2], /<button[^>]+data-action="help-connect">Open Settings<\/button>/);
  assert.match(context.modal[2], /data-action="close">Close</);
  assert.doesNotMatch(context.modal[2], /Connections &amp; setup|check agent activity/);
  assert.ok(fs.existsSync(require('node:path').join(__dirname, '../docs/help.md')), 'the guide the help dialog links to exists');
  const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /<button[^>]+data-action="help"[^>]+aria-label="Setup and help"/);
  assert.match(html, /id="agent-connect-prompt"/);
  assert.match(html, /Connect an agent <span aria-hidden="true">↓<\/span>/);
  assert.doesNotMatch(html, /Connect an agent <span>↗<\/span>/);
  // The sidebar is exactly four pages, one job each, in this order.
  const nav = html.slice(html.indexOf('<nav aria-label="Workspace navigation">'), html.indexOf('</nav>'));
  const links = [...nav.matchAll(/<a href="#(\w+)" data-view="(\w+)" aria-label="([^"]+)">[\s\S]*?<\/a>/g)].map(m => [m[1], m[2], m[3]]);
  assert.deepEqual(links, [['inbox', 'inbox', 'Inbox'], ['runs', 'runs', 'Tasks'], ['owners', 'owners', 'People'], ['connect', 'connect', 'Settings']]);
  for (const [, , label] of links) assert.match(nav, new RegExp(`<span class="nav-text">${label}</span>`), `${label} is the link's visible text`);
  assert.match(nav, /id="inbox-count"/);
  assert.match(nav, /<span class="nav-dot" id="settings-dot" hidden title="Setup incomplete"><\/span>/);
  for (const gone of ['Workspace tools', 'New conversation', 'Today', 'today-count', 'work-recents', 'workspace-tools', 'class="workspace"', 'PREVIEW', 'online-dot',
      'breadcrumb', 'id="eyebrow"', 'page-description', 'manual-actions', 'new-request', 'model-strip']) {
    assert.ok(!html.includes(gone), `${gone} is gone from the frame`);
  }
  assert.match(html, /<section class="page-head"><h1 id="page-title">Inbox<\/h1><div class="page-actions" id="page-actions"><\/div><\/section>/);
  assert.match(html, /<span class="connection" id="connection"><i><\/i><span id="workspace-name">Local workspace<\/span><\/span>/);
  // The Settings dot: set when a notice, no Slack or a blocker needs the person; cleared otherwise.
  const dot = {hidden: true};
  const dotContext = {state: {auth: {enabled: false}}, $: key => key === '#settings-dot' ? dot : null};
  vm.createContext(dotContext);
  vm.runInContext(source.slice(source.indexOf('function settingsDotState()'), source.indexOf('function loadOwnership()')), dotContext);
  dotContext.settingsDotState(); assert.equal(dot.hidden, true);
  dotContext.state.model = {proposals: false, notice: 'Suggestions are off: no model key.'}; dotContext.settingsDotState(); assert.equal(dot.hidden, false);
  dotContext.state.model = {proposals: true, notice: 'Model: claude-x. Suggestions on for watched repositories.'}; dotContext.settingsDotState(); assert.equal(dot.hidden, true, 'a configured backend is not a gap');
  dotContext.state.model = {}; dotContext.state.delivery = {enabled: false}; dotContext.settingsDotState(); assert.equal(dot.hidden, false);
  dotContext.state.delivery = {enabled: true}; dotContext.state.readiness = [{level: 'warning'}]; dotContext.settingsDotState(); assert.equal(dot.hidden, true);
  dotContext.state.readiness = [{level: 'blocker'}]; dotContext.settingsDotState(); assert.equal(dot.hidden, false);
  dotContext.state.auth = null; dotContext.settingsDotState(); assert.equal(dot.hidden, true);
  assert.ok(!source.includes('renderModelStrip'), 'the model strip is gone; Settings carries the notice');
  context.state.agent_connections = [];
  context.state.runs = [];
  context.updateAgentConnectPrompt();
  // Agent setup lives under Workspace tools rather than a persistent sidebar prompt.
  assert.equal(nodes['#agent-connect-prompt'].hidden, true);
  context.state.agent_connections = [{label: 'Codex', last_used_at: ''}];
  context.updateAgentConnectPrompt();
  assert.equal(nodes['#agent-connect-prompt'].hidden, true);
  context.state.agent_connections = [];
  context.state.runs = [{id: 'first-run'}];
  context.updateAgentConnectPrompt();
  assert.equal(nodes['#agent-connect-prompt'].hidden, true);
  context.state.runs = [];
  assert.match(context.connections(), /Pick your client\./);
  // The top-bar pill names the workspace when sign-in is on, else 'Local workspace'.
  nodes['#workspace-name'] = {};
  context.state.workspace = {name: 'Actual team <workspace>'};
  context.updateWorkspaceName();
  assert.equal(nodes['#workspace-name'].textContent, 'Actual team <workspace>');
  assert.equal(nodes['#workspace-name'].title, 'Actual team <workspace>');
  context.state.workspace.name = 'Renamed workspace';
  context.updateWorkspaceName();
  assert.equal(nodes['#workspace-name'].textContent, 'Renamed workspace');
  context.state.auth.enabled = false; context.updateWorkspaceName();
  assert.equal(nodes['#workspace-name'].textContent, 'Local workspace');
  context.state.auth.enabled = true;
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
    document: {body:{classList:{toggle(){}}},getElementById: () => inviteField},
  });
  assert.equal(inviteField.value, 'test-invitation');
  assert.match(context.connections(), /GitHub/);
  assert.match(context.githubCard(), /class="gray">Not connected/);
  // Sync through the server's GITHUB_TOKEN, no GitHub App: the card said Off while it worked.
  context.state.sync = {github: true, repos: [{repo: 'urllib3/urllib3', last_success_at: '', last_error: ''}]};
  assert.match(context.githubCard(), /class="gray">Not connected/);
  assert.match(context.githubCard(), /no repository has synced yet/);
  context.state.sync.repos[0].last_success_at = '2026-09-29T08:00:00Z';
  assert.match(context.githubCard(), /class="green">Connected/);
  assert.match(context.githubCard(), /GITHUB_TOKEN/);
  assert.match(context.githubCard(), /<strong>1<\/strong> synced repository/);
  assert.match(context.githubCard(), /synced just now/);
  assert.doesNotMatch(context.githubCard(), /awaiting setup/);
  context.state.sync.repos.push({repo: 'acme/app', last_success_at: '', last_error: 'GitHub 401: Bad credentials'});
  assert.match(context.githubCard(), /failing to sync: acme\/app: GitHub 401: Bad credentials/);
  context.state.sync = {github: false, repos: []};
  assert.match(context.githubCard(), /class="gray">Not connected/);
  assert.match(context.githubCard(), /awaiting setup/);
  delete context.state.sync;
  context.state.github_app = {configured: true, mode: 'device', authorized: true, repositories: ['acme/app']};
  assert.match(context.githubCard(), /class="green">Connected/);
  context.state.github_app.authorized = false;
  assert.match(context.githubCard(), /class="gray">Not connected/);
  context.state.github_app = {configured: true, repositories: []};
  assert.match(context.githubCard(), /class="gray">Not connected/);
  context.state.github_app.repositories = ['acme/app'];
  assert.match(context.githubCard(), /class="green">Connected/);
  assert.match(context.githubCard(), /connected repository/);
  context.state.github_app.repositories = Array.from({length: 29}, (_, i) => `acme/repository-${i}`);
  assert.match(context.githubCard(), /<strong>29<\/strong> connected repositories/);
  assert.match(context.githubCard(), /id="github-repositories"/);
  assert.doesNotMatch(context.githubCard(), /Connected: acme/);
  // Slack is card 3: a pill, one line, the link; connected keeps its controls.
  context.state.delivery = {enabled: false};
  assert.match(context.deliveriesCard(), /<span class="step">3<\/span>Slack/);
  assert.match(context.deliveriesCard(), /class="gray">Not connected/);
  assert.match(context.deliveriesCard(), /People can answer in the inbox without it\./);
  assert.match(context.deliveriesCard(), /How to connect ↗/);
  assert.doesNotMatch(context.deliveriesCard(), /Optional notifications|Existing Slack MCP setup|GitHub/);
  context.state.delivery = {enabled: true, directory: {synced_at: '2026-09-29T08:00:00Z', people: 12}, sent: 3, queued: 0, failed: 1, signing_secret: true};
  assert.match(context.deliveriesCard(), /class="green">Connected · 12 contacts synced/);
  assert.match(context.deliveriesCard(), /id="fallback-channel"/);
  assert.match(context.deliveriesCard(), /data-action="deliveries"/);
  delete context.state.delivery;
  // Polling must preserve expanded sections and the repository list position.
  const existing = [{id: 'local-launcher-help', open: true}, {id: 'manual-agent-setup', open: false}, {id: 'github-repositories', open: true}];
  const makeDisclosure = (item, scroll) => ({...item, list: {scrollTop: scroll}, querySelector() { return this.list; }});
  const oldSections = existing.map(item => makeDisclosure(item, 90));
  const newSections = existing.map(item => makeDisclosure({...item, open: false}, 0));
  const app = {innerHTML: '', dataset:{}, contains: () => false, querySelector: () => null};
  const renderContext = {workspaceError:'', state: {auth: {enabled: false}}, document: {body:{classList:{toggle(){}}},activeElement: {tagName: 'BODY'}, querySelectorAll: () => oldSections, getElementById: id => newSections.find(el => el.id === id)},
    $: () => app, taskId: () => '', view: 'connect', settingsDotState() {}, inbox() {}, runs() {}, owners() {}, connections: () => 'updated'};
  vm.createContext(renderContext);
  vm.runInContext(source.slice(source.indexOf('function render() {'), source.indexOf('function openModal(')), renderContext);
  renderContext.state = {};
  renderContext.render();
  assert.match(app.innerHTML, /Loading workspace/);
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
  // Connect shows the exact command the CLI accepts and, for Codex, the entry
  // the wizard writes: `claude mcp add --transport http bridge <url> --header
  // 'Authorization: Bearer …'` was run as printed against a live server and
  // connected; the Codex snippet had lost the `required = true` the wizard
  // writes, so the two paths disagreed.
  context.clientName = id => ({claude: 'Claude Code', codex: 'Codex', cursor: 'Cursor'})[id];
  vm.runInContext(source.slice(source.indexOf('async function quickAgent('), source.indexOf('async function launchAgent(')), context);
  await context.quickAgent('claude');
  assert.equal(request.data.label, 'Claude Code · quick connect');
  assert.match(context.modal[2], /<pre id="mcp-config">claude mcp add --transport http bridge 'http:\/\/localhost:7333\/mcp' --header 'Authorization: Bearer test-only-credential'<\/pre>/);
  assert.match(context.modal[2], /CLAUDE\.md/);
  await context.quickAgent('codex');
  assert.match(context.modal[2], /<pre id="mcp-config">\[mcp_servers\.bridge\]\nurl = "http:\/\/localhost:7333\/mcp"\nrequired = true\nhttp_headers = \{ Authorization = "Bearer test-only-credential" \}<\/pre>/);
  assert.match(context.modal[2], /AGENTS\.md/);
  assert.equal(context.state.mcp_config.mcpServers.bridge.headers, undefined);
  context.state.auth.enabled = false;
  assert.match(context.connections(), /Connect your coding agent/);
  assert.match(context.connections(), /<button class="button primary" data-action="copy">Copy configuration<\/button>/);
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
  assert.match(overview, /<p class="task-goal" id="task-brief">The complete original brief\.<\/p>/);
  assert.match(overview, /Open code review/);
  assert.match(overview, /Signed by Wes/);
  assert.match(overview, /Evidence & answer origin/);
  assert.match(overview, /SRC-42 · support/);
  assert.match(overview, /"sequence": 2/);
  assert.match(overview, /"namespace": "fixture-site"/);
  assert.match(overview, /"source_version_id": "version-2"/);
  assert.match(overview, /<h2 tabindex="-1">Code review<\/h2>/);
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
    assert.match(staleOverview, /class="done">Done</, 'a finished task stays Done; the stale review is one line under it');
    assert.doesNotMatch(staleOverview, /Review needed/);
    assert.match(staleOverview, /needs refreshing for the current questions and answers/);
    assert.match(staleOverview, /Submit the current diff again/);
    assert.doesNotMatch(staleOverview, /An answer changed after the code review/);
    assert.equal(reads,0,'Rendering stale review guidance must not issue requests');
  }
  fixture.tree.review = keptReview;
  const review = vm.runInContext("taskTab='decisions';taskOverview()",taskContext);
  assert.match(review, /Existing code is outside the diff/);
  assert.match(review, /Inspect requirements/);
  const timeline = vm.runInContext('taskTimeline(fixture.trace,fixture.tree.nodes)',taskContext);
  assert.match(timeline, /<strong>Wes<\/strong>/);
  await vm.runInContext('loadTask({quiet:true})',taskContext);
  assert.equal(reads,2,'Focusing a task tab must not freeze live updates');
  console.log('Web setup checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
