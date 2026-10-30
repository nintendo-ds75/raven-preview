'use strict';
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict'), path=require('node:path');
const root=path.join(__dirname,'../web'), onboard=fs.readFileSync(path.join(root,'onboarding.js'),'utf8');
const app=fs.readFileSync(path.join(root,'app.js'),'utf8').split('const paths =')[0];
const route='#runs/123456789abc/abcdef123456';
function harness(hash=route) {
 const saved=new Map(), c={URLSearchParams,Date,location:{hash,pathname:'/',search:''},document:{getElementById:()=>null},
  sessionStorage:{setItem:(k,v)=>saved.set(k,v),getItem:k=>saved.get(k),removeItem:k=>saved.delete(k)},history:{replaceState:(_,__,url)=>c.url=url}};
 vm.createContext(c);return {saved,c,run:s=>vm.runInContext(s,c)};
}
let h=harness();h.run(onboard);h.c.location.hash='#inbox';h.run(app);assert.equal(h.c.url,'/'+route);assert.equal(h.saved.size,0);
for(const bad of ['#rvn_secret','#runs/https://evil/thing','#runs/123456789abc/abcdef123456?redirect=evil','#runs/123456789abc/abcdef123456/extra']) {h=harness(bad);h.run(onboard);assert.equal(h.saved.size,0);}
h=harness();h.run(onboard);h.c.location.hash='#work';h.run(app);assert.equal(h.c.url,undefined);assert.equal(h.saved.size,0);
h=harness();h.run(onboard);h.c.location.hash='#runs';h.run(app);assert.equal(h.c.url,'/'+route);
h=harness('');h.saved.set('raven-return-decision',JSON.stringify({route,savedAt:Date.now()-31*60*1000}));h.run(app);assert.equal(h.c.url,undefined);
h=harness('');h.saved.set('raven-return-decision','not JSON');h.run(app);assert.equal(h.c.url,undefined);
h=harness();h.c.sessionStorage.setItem=()=>{throw Error('blocked')};h.run(onboard);h.run(app);
// The app lands on Ask Raven: an empty or unknown hash is #ask, #memory is the inbox's Decided tab,
// and #work without a task goes to #runs unless this server runs tasks in the browser.
const full=fs.readFileSync(path.join(root,'app.js'),'utf8');
const navSource=full.slice(full.indexOf('function navigate()'),full.indexOf('\nfunction ',full.indexOf('function navigate()')+10));
function landing(hash,state={auth:{enabled:false}}) {
 const title={}, c={titles:{ask:['Ask Raven'],map:['Map'],inbox:['Inbox'],runs:['Tasks'],owners:['People'],connect:['Settings'],today:['Outcomes'],work:['Raven']},
  state,view:'',tab:'pending',tabChosen:false,query:'',ownerFilter:'',ownership:null,directory:null,navigationVersion:0,taskTab:'',taskDetail:null,taskError:'',resetScrollOnNavigation:false,
  location:{hash,pathname:'/',search:''},history:{replaceState:(_,__,url)=>c.replaced=url},document:{title:'',querySelectorAll:()=>[]},
  $:()=>title,taskId:()=>'',render(){},window:{}};
 vm.createContext(c);vm.runInContext(navSource+';navigate();',c);return c;
}
let c=landing('');assert.equal(c.view,'ask');assert.equal(c.document.title,'Raven · Ask Raven');assert.equal(c.replaced,undefined);
c=landing('#inbox');assert.equal(c.view,'inbox');
c=landing('#nonsense');assert.equal(c.view,'ask');
c=landing('#map');assert.equal(c.view,'map');
c=landing('#memory');assert.equal(c.view,'inbox');assert.equal(c.tab,'decided');assert.equal(c.tabChosen,true);assert.equal(c.replaced,'/#inbox');
c=landing('#work');assert.equal(c.view,'runs');assert.equal(c.replaced,'/#runs');assert.equal(c.document.title,'Raven · Tasks');
c=landing('#work',{auth:{enabled:false},workbench:{ready:true}});assert.equal(c.view,'work');assert.equal(c.replaced,undefined);assert.equal(c.document.title,'Raven');
c=landing('#work/abc');assert.equal(c.view,'work');assert.equal(c.replaced,undefined);
c=landing('#today');assert.equal(c.view,'today');assert.equal(c.document.title,'Raven · Outcomes');
c=landing('#today/obj1');assert.equal(c.view,'today');
c=landing('#connect');assert.equal(c.view,'connect');assert.equal(c.document.title,'Raven · Settings');
console.log('Login return route: bounded internal route, no secret storage, explicit route preference, expiry, disabled storage and inbox landing checks passed.');
