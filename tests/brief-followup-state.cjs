/* Live pytest loop regression: a required, unadopted follow-up was hidden
 * from the completed-answer headline. Exercise the shipped render functions. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2] || 'web/brief.js', 'utf8');
const context = {location: {hash: '#synthetic-fixture'}, console,
  addEventListener() {}, setInterval() {}, fetch: () => new Promise(() => {}),
  ResizeObserver: class {observe() {} disconnect() {}},
  document: {addEventListener() {}},
  matchMedia: () => ({matches: false, addEventListener() {}})};
context.window = context;
vm.createContext(context);
vm.runInContext(source, context);
context.fixture = {viewer: {name: 'Lina'}, requester: {name: 'Requester'}, nodes: [
  {node_id: 'signed', raw_status: 'answered', state: 'signed', blocking: false,
    owner: 'Lina', signatures: ['Lina'], question: 'Reporting policy?'},
  {node_id: 'followup', raw_status: 'suggested', state: 'waiting', blocking: true,
    owner: '', signatures: [], question: 'Confirm documentation?', can_act: false}]};
vm.runInContext('data = fixture', context);
let hero = vm.runInContext("heroLine('mine')", context);
assert.match(hero, /required follow-up/);
assert.doesNotMatch(hero, /Nothing on this task waits/);
assert.match(vm.runInContext('decisionsCard(data.nodes)', context), /Required follow-up/);
context.fixture.nodes[1].blocking = false;
hero = vm.runInContext("heroLine('mine')", context);
assert.doesNotMatch(hero, /required follow-up/);
context.fixture.nodes[1] = {raw_status: 'pending', blocking: true, owner: '', signatures: []};
hero = vm.runInContext("heroLine('mine')", context);
assert.match(hero, /unassigned/);
assert.doesNotMatch(hero, /Nothing on this task waits/);
console.log('Required and unassigned follow-up rendering passed');
