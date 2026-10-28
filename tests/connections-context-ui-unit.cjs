'use strict';
// Settings: the 'Not set up yet' list, the four numbered cards, the Routing card and Advanced.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const c={state:{auth:{enabled:true},me:{role:'admin'},workspace:{name:'Acme'},graph:{repos:['acme/fetchkit'],engineers:2,intents:1}},
  esc:value=>String(value),plural:(n,one,many)=>`${n} ${n===1?one:(many||one+'s')}`,pill:(text,color)=>`<span class="pill ${color}">${text}</span>`,
  agentCards:()=>'',agentStatus:()=>'',githubCard:(extra='')=>'<h3>GitHub</h3>'+(extra?`<div class="button-row">${extra}</div>`:''),workspaceAccountCard:()=>'<button data-action="invite-person">Invite teammate</button>',
  workbenchSetupCard:()=>'',savedStrip:()=>'<p class="saved-strip">What Raven saved</p>',ownershipGraph:()=>'<table></table>',
  canAdminister:()=>true,directory:null,loadDirectory(){c.loaded=(c.loaded||0)+1;},AGENT_RULE:'## Raven',deliveriesCard:()=>'<section>Slack</section>'};
vm.createContext(c);
vm.runInContext(source.slice(source.indexOf('function setupChecklist()'),source.indexOf('async function setupAgent()')),c);
let html=c.connections();
// Order: checklist (empty here), then the cards, unnumbered: once set up they are not a sequence.
assert.doesNotMatch(html,/Not set up yet/);
assert.doesNotMatch(html,/class="step"/,'no step numbers');
const order=['<h2>Connect your coding agent</h2>','<h2>Context</h2>','<section>Slack</section>','<h2>People access</h2>','<h2>Routing</h2>','<summary>Advanced</summary>'];
let at=-1;for(const marker of order){const next=html.indexOf(marker);assert.ok(next>at,`${marker} in order`);at=next;}
assert.match(html,/<p>Pick your client\.<\/p>/);
assert.match(html,/Already indexed:<\/strong> acme\/fetchkit · 2 people from its history, 1 record\. Raven can name them now\./);
// 'Add a local clone' is a text button in the GitHub button row; the ways to add context are one list under 'Add context'.
assert.match(html,/<h3>GitHub<\/h3><div class="button-row"><button class="button text" data-action="ingest">Add a local clone<\/button><\/div>/);
assert.match(html,/<h3>Add context<\/h3><details class="history" id="settings-evidence"><summary>Paste a message or email<\/summary><div id="evidence-mount"><\/div><\/details><details class="history" id="settings-meetings"><summary>Meetings<\/summary><div id="meetings-mount"><\/div><\/details>/);
assert.doesNotMatch(html,/Other sources|none yet|Available to add/,'no empty sources heading');
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
assert.match(html,/<h3>Add context<\/h3><details class="history" id="settings-evidence">/,'a member still gets the paste panel under Add context');
c.canAdminister=()=>true;
// The checklist: rows grouped under their level word, one sentence each with the rest behind 'Why'; omitted when empty.
c.state.model={backend:'none',proposals:false,notice:'Suggestions and briefs are off: no model key. Add a key with ./setup --configure and restart.'};
c.state.delivery={enabled:false};
c.state.readiness=[{key:'no_slack',level:'info',what:'Slack is optional.',do:'Use the inbox.'},{key:'no_triage_channel',level:'warning',what:'No Slack channel is configured.',do:'Set SLACK_FALLBACK_CHANNEL.'},{key:'ingest_failed',level:'blocker',what:'Raven could not read the repository.',do:'Fix the path, then restart Raven.'},{key:'no_inference',level:'warning',what:'Suggestions are unavailable. Configure a backend; explicit commands still work.',do:'Set up Anthropic on the server. Do not send API keys in chat.'}];
const list=c.setupChecklist();
assert.match(list,/^<h2 id="setup-checklist-title">Not set up yet<\/h2><section class="setup-list" aria-labelledby="setup-checklist-title"><h3>Set this up first<\/h3><ul><li><span>Raven could not read the repository\.<\/span><details class="setup-why"><summary>Why<\/summary><p>Fix the path, then restart Raven\.<\/p><\/details><\/li><\/ul><h3>Worth fixing<\/h3><ul>/,'blockers first, each level word once');
assert.match(list,/<h3>Worth fixing<\/h3><ul><li><span>Suggestions and briefs are off: no model key\.<\/span><details class="setup-why"><summary>Why<\/summary><p>Add a key with \.\/setup --configure and restart\.<\/p><\/details><\/li><li><span>No Slack channel is configured\.<\/span><details class="setup-why"><summary>Why<\/summary><p>Set SLACK_FALLBACK_CHANNEL\.<\/p><\/details><\/li><li><span>Suggestions are unavailable\.<\/span><details class="setup-why"><summary>Why<\/summary><p>Configure a backend; explicit commands still work\. Set up Anthropic on the server\. Do not send API keys in chat\.<\/p><\/details><\/li><\/ul>/,'one sentence per row; the rest behind Why');
assert.match(list,/<h3>Note<\/h3><ul><li><span>Slack not connected: people answer in the inbox\.<\/span><\/li><\/ul><\/section>$/,'a one-sentence row has no Why');
assert.equal((list.match(/Worth fixing/g)||[]).length,1,'the level word appears once per group');
assert.doesNotMatch(list,/Slack is optional/,'the readiness Slack row is the same fact; it is said once');
assert.doesNotMatch(list,/readiness-row|class="blocker"|class="warning"|<strong>/,'the level is a heading, not a colour or a badge');
assert.ok(c.connections().startsWith('<h2 id="setup-checklist-title">Not set up yet</h2>'),'the checklist is first');
c.state.model={};c.state.delivery={enabled:true};c.state.readiness=[];
assert.equal(c.setupChecklist(),'');
// A configured backend's notice is information, not a gap: no checklist row, nothing to fix.
c.state.model={backend:'anthropic',proposals:true,notice:'Model: claude-x. Suggestions on for watched repositories.'};
assert.equal(c.setupChecklist(),'','a configured server has nothing to set up');
c.state.model={backend:'claude-cli',proposals:false,investigation:true,notice:'Manual investigation only; automatic suggestions need an API key.'};
assert.match(c.setupChecklist(),/<h3>Worth fixing<\/h3><ul><li><span>Manual investigation only; automatic suggestions need an API key\.<\/span><\/li><\/ul>/);
c.state.model={};
// A viewer cannot paste evidence, so Settings draws no empty disclosure for them.
c.state.me={role:'viewer'};c.canAdminister=()=>false;
assert.doesNotMatch(c.connections(),/settings-evidence|evidence-mount/,'no empty evidence disclosure for a viewer');
c.state.me={role:'member'};assert.match(c.connections(),/id="settings-evidence"/);
c.state.me={role:'admin'};c.canAdminister=()=>true;
// Card 2 keeps the indexed line only when a repository is indexed; local mode names the single-operator state.
c.state.graph={repos:[],engineers:0,intents:0};assert.doesNotMatch(c.connections(),/Already indexed/);
delete c.state.graph;assert.doesNotMatch(c.connections(),/Already indexed/);
c.state.auth.enabled=false;html=c.connections();
assert.match(html,/<p>Add this to your agent’s MCP settings\.<\/p><pre id="mcp-config">/);
assert.match(html,/<p>Then add this to <code>CLAUDE\.md<\/code> or <code>AGENTS\.md<\/code>\.<\/p><pre id="agent-rule">## Raven<\/pre>/);
assert.match(html,/<h2>People access<\/h2><p>Sign-in is off: single-operator workspace\. Turn it on before sharing this server\.<\/p>/);
assert.doesNotMatch(html,/Authentication is off|Three steps|Set up Raven/);
// Other sources: rows when a source report exists; the kinds left to add are the first item under 'Add context'.
c.state.context_sources={sources:[{kind:'slack',label:'#eng',repo:'acme/app',state:'ok',records:4}],not_configured:[{kind:'jira',credentials_present:false,enable:'JIRA_BASE_URL=…'}]};
const sources=c.contextSourcesCard();
assert.match(sources,/^<div class="github-heading" id="context-sources"><h3>Other sources<\/h3><\/div><ul><li><strong>slack<\/strong> #eng → acme\/app/);
assert.doesNotMatch(sources,/Records are evidence|attested|Available to add|none yet/);
assert.match(c.connections(),/<h3>Add context<\/h3><details class="history" id="context-sources-available"><summary>From a site, Jira or Slack<\/summary><ul><li><strong>jira<\/strong> not configured \(credentials not set\)<br><code>JIRA_BASE_URL=…<\/code><\/li><\/ul><\/details><details class="history" id="settings-evidence">/);
c.state.context_sources={sources:[],not_configured:[]};assert.equal(c.contextSourcesCard(),'');
// The confirmations and the Ask dialog use the words on the controls and the page names; the old ones are gone.
assert.match(source,/notify\(event\.target\.value \? 'Fallback person set\. Questions nobody is known to own go to them\.' : 'Fallback person cleared\.'\)/);
assert.match(source,/notify\(event\.target\.checked \? 'Only verified owners are asked now\.' : 'Git history can suggest owners again\.'\)/);
assert.match(source,/Connect or ingest a repository in Settings\./);
assert.doesNotMatch(source,/Coordinator set|Coordinator cleared|Pilot mode|Connections & setup/);
console.log('Settings: checklist, four cards, Routing ids and Advanced render in order with the spec copy.');
