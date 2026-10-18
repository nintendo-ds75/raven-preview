'use strict';
// Step 2 of Connections & setup says when a repository is already indexed.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const c={state:{auth:{enabled:true},me:{role:'admin'},workspace:{name:'Acme'},graph:{repos:['acme/fetchkit'],engineers:2,intents:1}},
  esc:value=>String(value),plural:(n,one,many)=>`${n} ${n===1?one:(many||one+'s')}`,
  agentCards:()=>'',agentStatus:()=>'',deliveriesCard:()=>'',contextSourcesCard:()=>'',workspaceAccountCard:()=>'',workbenchSetupCard:()=>''};
vm.createContext(c);vm.runInContext(source.slice(source.indexOf('function connections()'),source.indexOf('function contextSourcesCard()')),c);
let html=c.connections();
assert.match(html,/Already indexed:<\/strong> acme\/fetchkit · 2 people from its history, 1 record\. Raven can name them now\./);
assert.match(html,/Connect GitHub below to keep it current, or ingest a local clone/);
c.state.graph={repos:[],engineers:0,intents:0};html=c.connections();
assert.doesNotMatch(html,/Already indexed/);
assert.match(html,/Connect GitHub below, or ingest a local clone with <code>\.\/setup --repo \/path\/to\/checkout<\/code>/);
delete c.state.graph;assert.doesNotMatch(c.connections(),/Already indexed/);
console.log('Connections step 2: indexed repositories are named; the empty state keeps the ingest instruction.');
