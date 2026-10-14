'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const c={state:{auth:{enabled:true},me:{role:'admin'},workspace:{name:'Example'}},esc:value=>String(value)};
vm.createContext(c);vm.runInContext(source.slice(source.indexOf('function workspaceAccountCard()'),source.indexOf('async function setupAgent()')),c);
let html=c.workspaceAccountCard();
assert.match(html,/Invite teammates to review context, ask questions and sign off on decisions in Raven/);
assert.match(html,/Default notification links require a Raven account/);
assert.match(html,/expire after 48 hours/);
assert.doesNotMatch(html,/Optional browser access|without a Raven account/);
assert.match(html,/data-action="invite-person"/);
c.state.me.role='member';assert.equal(c.workspaceAccountCard(),'');
c.state.me.role='admin';c.state.auth.enabled=false;assert.equal(c.workspaceAccountCard(),'');
// Keep the customer-facing expiry tied to the real invitation implementation.
const accounts=fs.readFileSync(path.join(__dirname,'../bridge/accounts.py'),'utf8');
assert.match(accounts,/int\(time\.time\(\)\) \+ 172800/);
assert.match(accounts,/'expires_in': 172800/);
console.log('Account setup copy: authenticated notification links, invite expiry and admin-only control checks passed.');
