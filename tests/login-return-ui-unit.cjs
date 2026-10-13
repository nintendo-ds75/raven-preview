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
h=harness('');h.saved.set('raven-return-decision',JSON.stringify({route,savedAt:Date.now()-31*60*1000}));h.run(app);assert.equal(h.c.url,undefined);
h=harness('');h.saved.set('raven-return-decision','not JSON');h.run(app);assert.equal(h.c.url,undefined);
h=harness();h.c.sessionStorage.setItem=()=>{throw Error('blocked')};h.run(onboard);h.run(app);
console.log('Login return route: bounded internal route, no secret storage, explicit route preference, expiry and disabled storage checks passed.');
