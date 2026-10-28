'use strict';
// Settings: the 'Not set up yet' list, the four numbered cards, the Routing card and Advanced.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const c={state:{auth:{enabled:true},me:{role:'admin'},workspace:{name:'Acme'},graph:{repos:['acme/fetchkit'],engineers:2,intents:1}},
  esc:value=>String(value),plural:(n,one,many)=>`${n} ${n===1?one:(many||one+'s')}`,pill:(text,color)=>`<span class="pill ${color}">${text}</span>`,
  agentCards:()=>'',agentStatus:()=>'',githubCard:()=>'<h3>GitHub</h3>',workspaceAccountCard:()=>'<button data-action="invite-person">Invite teammate</button>',
  workbenchSetupCard:()=>'',savedStrip:()=>'<p class="saved-strip">What Raven saved</p>',ownershipGraph:()=>'<table></table>',
  canAdminister:()=>true,directory:null,loadDirectory(){c.loaded=(c.loaded||0)+1;},AGENT_RULE:'## Raven',deliveriesCard:()=>'<section>Slack</section>'};
vm.createContext(c);
vm.runInContext(source.slice(source.indexOf('function setupChecklist()'),source.indexOf('async function setupAgent()')),c);
let html=c.connections();
// Order: checklist (empty here), card 1, card 2, card 3, card 4, Routing, Advanced.
assert.doesNotMatch(html,/Not set up yet/);
const order=['<span class="step">1</span>Connect your coding agent','<span class="step">2</span>Context','<section>Slack</section>','<span class="step">4</span>People access','<h2>Routing</h2>','<summary>Advanced</summary>'];
let at=-1;for(const marker of order){const next=html.indexOf(marker);assert.ok(next>at,`${marker} in order`);at=next;}
assert.match(html,/<p>Pick your client\.<\/p>/);
assert.match(html,/Already indexed:<\/strong> acme\/fetchkit · 2 people from its history, 1 record\. Raven can name them now\./);
assert.match(html,/<button class="button text" data-action="ingest">Add a local clone<\/button>/);
assert.match(html,/<details class="history" id="settings-evidence"><summary>Paste a message or email<\/summary><div id="evidence-mount"><\/div><\/details>/);
assert.match(html,/<details class="history" id="settings-meetings"><summary>Meetings<\/summary><div id="meetings-mount"><\/div><\/details>/);
assert.match(html,/<div class="github-heading" id="context-sources"><h3>Other sources<\/h3><span class="muted">none yet<\/span><\/div>/);
assert.match(html,/Invite teammate/);
assert.match(html,/<details class="history settings-advanced" id="repository-signals"><summary>Advanced<\/summary><p class="saved-strip">What Raven saved<\/p><h3>Who git thinks owns what<\/h3><table><\/table><p><button class="button" data-action="export">Export history<\/button><\/p><\/details>/);
// Routing waits for the directory, then shows the four controls with their ids.
assert.match(html,/<h2>Routing<\/h2><p class="context" role="status">Loading…<\/p>/);
assert.equal(c.loaded,1);
c.directory={people:[{id:'p1',name:'Alex Morgan',team:'Infrastructure'}],settings:{coordinator:{id:'p1'},require_verified_route:true,auto_rules:false,brief_mode:'off'}};
html=c.connections();
assert.match(html,/<label for="coordinator-select">Fallback person<\/label><select id="coordinator-select"><option value="">Nobody<\/option><option value="p1" selected>Alex Morgan · Infrastructure<\/option><\/select><p class="field-help">Gets questions nobody is known to own\.<\/p>/);
assert.match(html,/<input type="checkbox" id="verified-only" checked><span>Only ask verified owners<\/span>/);
assert.match(html,/<input type="checkbox" id="auto-rules" ><span>Reuse signed answers automatically<\/span>/);
assert.match(html,/<label for="brief-mode">Slack links open<\/label><select id="brief-mode"><option value="off" selected>The inbox<\/option><option value="static" >The task page<\/option><\/select>/);
assert.doesNotMatch(html,/read-only-settings/);
c.canAdminister=()=>false;html=c.connections();
assert.match(html,/<fieldset class="read-only-settings" disabled><legend>Managed by an administrator<\/legend><section class="setup-card" id="settings-routing"><h2>Routing<\/h2>/);
assert.doesNotMatch(html,/data-action="ingest"|settings-meetings|People access/);
c.canAdminister=()=>true;
// The checklist: the server's notices as written, Slack as one line, readiness as level words; omitted when empty.
c.state.model={notice:'Suggestions and briefs are off: no model key. Add a key with ./setup --configure and restart.'};
c.state.delivery={enabled:false};
c.state.readiness=[{key:'no_slack',level:'info',what:'Slack is optional.',do:'Use the inbox.'},{key:'no_triage_channel',level:'warning',what:'No Slack channel is configured.',do:'Set SLACK_FALLBACK_CHANNEL.'},{key:'ingest_failed',level:'blocker',what:'Raven could not read the repository.',do:'Fix the path, then restart Raven.'}];
const list=c.setupChecklist();
assert.match(list,/^<h2 id="setup-checklist-title">Not set up yet<\/h2><ul class="setup-list" aria-labelledby="setup-checklist-title">/);
assert.match(list,/<li><strong>Worth fixing<\/strong><span>Suggestions and briefs are off: no model key\. Add a key with \.\/setup --configure and restart\.<\/span><\/li>/);
assert.match(list,/<li><strong>Note<\/strong><span>Slack not connected: people answer in the inbox\.<\/span><\/li>/);
assert.doesNotMatch(list,/Slack is optional/,'the readiness Slack row is the same fact; it is said once');
assert.match(list,/<li><strong>Worth fixing<\/strong><span>No Slack channel is configured\. Set SLACK_FALLBACK_CHANNEL\.<\/span><\/li>/);
assert.match(list,/<li><strong>Set this up first<\/strong><span>Raven could not read the repository\. Fix the path, then restart Raven\.<\/span><\/li>/);
assert.doesNotMatch(list,/readiness-row|class="blocker"|class="warning"/,'the level is a word, not a colour');
assert.ok(c.connections().startsWith('<h2 id="setup-checklist-title">Not set up yet</h2>'),'the checklist is first');
c.state.model={};c.state.delivery={enabled:true};c.state.readiness=[];
assert.equal(c.setupChecklist(),'');
// Card 2 keeps the indexed line only when a repository is indexed; local mode names the single-operator state.
c.state.graph={repos:[],engineers:0,intents:0};assert.doesNotMatch(c.connections(),/Already indexed/);
delete c.state.graph;assert.doesNotMatch(c.connections(),/Already indexed/);
c.state.auth.enabled=false;html=c.connections();
assert.match(html,/<p>Add this to your agent’s MCP settings\.<\/p><pre id="mcp-config">/);
assert.match(html,/<p>Then add this to <code>CLAUDE\.md<\/code> or <code>AGENTS\.md<\/code>\.<\/p><pre id="agent-rule">## Raven<\/pre>/);
assert.match(html,/<span class="step">4<\/span>People access<\/h2><p>Sign-in is off: single-operator workspace\. Turn it on before sharing this server\.<\/p>/);
assert.doesNotMatch(html,/Authentication is off|Three steps|Set up Raven/);
// Other sources: rows when a source report exists, the 'Available to add' details for the rest.
c.state.context_sources={sources:[{kind:'slack',label:'#eng',repo:'acme/app',state:'ok',records:4}],not_configured:[{kind:'jira',credentials_present:false,enable:'JIRA_BASE_URL=…'}]};
const sources=c.contextSourcesCard();
assert.match(sources,/<h3>Other sources<\/h3><\/div><ul><li><strong>slack<\/strong> #eng → acme\/app/);
assert.match(sources,/<details class="history" id="context-sources-available"><summary>Available to add<\/summary><ul><li><strong>jira<\/strong> not configured \(credentials not set\)<br><code>JIRA_BASE_URL=…<\/code><\/li><\/ul><\/details>/);
assert.doesNotMatch(sources,/Records are evidence|attested/);
console.log('Settings: checklist, four cards, Routing ids and Advanced render in order with the spec copy.');
