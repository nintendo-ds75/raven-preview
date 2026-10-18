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
// The invitation names a person seeded without an email; people with an
// email link on their own and are not offered, and nothing links by name.
vm.runInContext(source.slice(source.indexOf('function inviteForm('), source.indexOf('function readOnlyReason(')), context);
const people = [{id: 'p1', name: 'Alex Morgan', active: 1, email: '', github_id: '', team: 'billing'},
  {id: 'p2', name: 'Dana Lee', active: 1, email: 'dana@example.test', github_id: ''},
  {id: 'p3', name: 'Gone Person', active: 0, email: '', github_id: ''},
  {id: 'p4', name: 'Git Hub', active: 1, email: '', github_id: '42'}];
const form = context.inviteForm(people);
assert.match(form, /name="person_id"/);
assert.match(form, /<option value="p1">Alex Morgan · billing — already on the map, no email<\/option>/);
assert.doesNotMatch(form, /Dana Lee|Gone Person|Git Hub/);
assert.match(form, /a matching name is not enough/);
assert.doesNotMatch(context.inviteForm([people[1]]), /person_id/);
// A signed-in person records on their own standing; only the local operator
// records on behalf of the named owner. The copy says so.
assert.doesNotMatch(source, /, on behalf of \$\{esc\(d\.owner_name\)\}/);
assert.match(source, /on your own standing; \$\{esc\(d\.owner_name\)\} is its assigned owner/);
assert.equal((source.match(/\$\{me \? `recorded as \$\{esc\(me\)\}` : `recorded on behalf of \$\{esc\(signer\)\}`\}/g) || []).length, 2);
console.log('inbox-identity-ui-unit: ok');
