// The inbox badge and "Assigned to me" follow the server's per-decision
// standing (by person id), not the signed-in person's display name.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const context = {state: {auth: {enabled: true}, me: {id: 'p2', name: 'Alex Morgan', role: 'member'}},
  esc: v => String(v), pill: t => t, icon: () => '', ago: () => 'just now', avatar: () => '', plural: (n, w) => `${n} ${w}`};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const kindLabels'), source.indexOf('function activity()'))
  + '\n;globalThis.inbox = {assignedToMe, needsYou, personalInbox};', context);
const {assignedToMe, needsYou, personalInbox} = context.inbox;
const seeded = {id: 'd1', status: 'pending', owner_name: 'Alex Morgan', required_signers: '[]', can_act: false};
const theirs = {id: 'd2', status: 'pending', owner_name: 'Someone Else', required_signers: '[]', can_act: true};
const settled = {id: 'd3', status: 'approved', signoff: 'signed', owner_name: 'Alex Morgan', can_act: false};
const legacy = {id: 'd4', status: 'pending', owner_name: 'Alex Morgan', required_signers: '["Alex Morgan"]'};
assert.equal(needsYou(seeded), true);
// A namesake's decision is not assigned to the signed-in person, whatever the name says.
assert.equal(assignedToMe(seeded), false);
// A decision the server says they may act on is, whatever its owner's name.
assert.equal(assignedToMe(theirs), true);
assert.equal(assignedToMe(settled), false);
// Without the server's word, nothing is assumed from the name or the signer list.
assert.equal(assignedToMe(legacy), false);
assert.equal(personalInbox(), true);
assert.equal([seeded, theirs, settled, legacy].filter(assignedToMe).length, 1);
// The source no longer compares display names for the inbox.
assert.doesNotMatch(source, /d\.owner_name === state\.me\?\.name/);
console.log('inbox-identity-ui-unit: ok');
