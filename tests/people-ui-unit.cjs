/* The People page is person cards with their authority rows: no table, no
 * strip, no routing card. Every authority record the directory returns is
 * on a card; a row nobody's card carries goes under a last 'Other' card.
 * This exercises the shipped app.js with DOM doubles, not a browser. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../web/style.css'), 'utf8');

function harness({admin = true, people, authority, decisions = []} = {}) {
  const actions = {innerHTML: ''};
  const loads = [];
  const c = {
    state: {auth: {enabled: false}, me: admin ? {role: 'admin'} : {role: 'member'}, decisions},
    directory: people === undefined ? null : {people, authority, teams: [{id: 't1', name: 'Payments'}]},
    $: selector => selector === '#page-actions' ? actions : null,
    loadDirectory: () => loads.push(1),
    esc: v => String(v ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch])),
    avatar: (name, i) => `<span class="avatar">${name[0]}${i}</span>`,
    plural: (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`,
    empty: (title, text, action = '', button = '') => `<div class="empty"><h2>${title}</h2><p>${text}</p>${action ? `<button data-action="${action}">${button}</button>` : ''}</div>`,
    openModal: (title, subtitle, body) => { c.modal = {title, subtitle, body}; },
  };
  if (!admin) c.state.auth.enabled = true;
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('const roleLabels = {'), source.indexOf('function deliveriesCard() {'))
    + '\n;globalThis.fns = {owners, personCard, newAuthority, newPerson, newTeam, canAdminister};', c);
  return {c, actions, loads, ...c.fns};
}

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const cardsOf = html => html.split('<article ').slice(1).map(c => '<article ' + c);
const people = [
  {id: 'p1', name: 'Alex Morgan', team: 'Infrastructure', teams: [], active: 1, email: '', github_login: 'alexm', slack_id: ''},
  {id: 'p2', name: 'Marisol Vega', team: 'Pricing', teams: ['Payments'], active: 1, email: '', github_login: '', slack_id: ''},
];
const authority = [
  {id: 'a1', person_id: 'p1', who: 'Alex Morgan', is_team: false, role: 'decides', scope_kind: 'path', scope: 'auth/*', repo: '', source: 'config', asserted_by: '', accepted: 1, effective_to: ''},
  {id: 'a2', person_id: 'p2', who: 'Marisol Vega', is_team: false, role: 'knows', scope_kind: 'category', scope: 'billing', repo: 'acme/platform', source: 'answer', asserted_by: 'Marisol Vega', accepted: 1, effective_to: ''},
  {id: 'a3', person_id: 'p2', who: 'Marisol Vega', is_team: false, role: 'decides', scope_kind: 'path', scope: 'pricing/*', repo: '', source: 'config', asserted_by: '', accepted: 1, effective_to: '2020-01-01'},
];

// 1. Admin: cards, rows with their source text, End only on live rows.
{
  const h = harness({people, authority, decisions: [
    {id: 'd1', status: 'pending', owner_id: 'o-alex', owner_name: 'Alex Morgan'},
    {id: 'd2', status: 'pending', owner_id: 'o-alex', owner_name: 'alex morgan'},
    {id: 'd3', status: 'approved', owner_id: 'o-alex', owner_name: 'Alex Morgan'},
  ]});
  const html = h.owners();
  assert.equal(h.loads.length, 0);
  assert.doesNotMatch(html, /table-wrap|<table|routing-settings|saved-strip|readiness|repository-signals|Owners by path pattern|notice/);
  assert.match(h.actions.innerHTML, /<span class="page-count muted">2 people<\/span>/);
  assert.match(h.actions.innerHTML, /<button class="button primary" data-action="person">Add person<\/button>/);
  assert.match(h.actions.innerHTML, /<details class="more-menu"><summary aria-label="More actions">More<\/summary><div class="more-menu-items"><button class="button text" data-action="team">Add team<\/button><\/div><\/details>/);
  assert.equal((h.actions.innerHTML.match(/class="button primary"/g) || []).length, 1, 'one primary button');
  const cards = cardsOf(html);
  assert.equal(cards.length, 2, 'one card per person, nothing else');
  const [alex, marisol] = cards.map(text);
  assert.match(alex, /^A0 Alex Morgan Infrastructure @alexm Waiting on them: 2 Decides decides auth\/\* · every repository · from config End Say what they decide$/);
  assert.match(marisol, /Marisol Vega Pricing · Payments Decides/);
  assert.doesNotMatch(marisol, /Waiting on them/);
  assert.match(marisol, /knows billing · acme\/platform · from an answer · asserted by Marisol Vega End/);
  // The expired row is listed last, muted, says when it ended, and cannot be ended again.
  assert.match(marisol, /decides pricing\/\* · every repository · from config · ended 2020-01-01 Say what they decide$/);
  assert.match(cards[1], /<div class="authority-row ended"><span><strong>decides pricing\/\*<\/strong>/);
  assert.doesNotMatch(cards[1], /ended"><span>(?:(?!<\/div>).)*data-action="end-authority"/);
  // The End button names the row it retires (a destructive action among identical buttons); its visible text leads the name.
  assert.match(cards[0], /<button class="button small soft" data-action="end-authority" data-id="a1" aria-label="End: Alex Morgan decides auth\/\* in every repository">End<\/button>/);
  assert.match(cards[1], /data-id="a2" aria-label="End: Marisol Vega knows billing in acme\/platform">End</);
  assert.match(cards[1], /data-action="end-authority" data-id="a2"/);
  assert.doesNotMatch(cards[1], /data-id="a3"/);
  // Headings in order under the page's h1: the name is an h2, Decides an h3.
  assert.match(cards[0], /<h2>Alex Morgan<\/h2>/);
  assert.match(cards[0], /<h3>Decides<\/h3>/);
  assert.doesNotMatch(html, /<h4>/);
  assert.match(cards[1], /<button class="button text" data-action="authority" data-person="p2" aria-label="Say what they decide: Marisol Vega">Say what they decide<\/button>/);
  // No identities line when there are none.
  assert.doesNotMatch(cards[1], /<small>/);
  // 'Say what they decide' opens the authority form with the person chosen and a Save button.
  h.newAuthority('p2');
  assert.equal(h.c.modal.title, 'Say what they decide');
  assert.equal(h.c.modal.subtitle, '');
  assert.match(h.c.modal.body, /<option value="person:p2" selected>Marisol Vega<\/option>/);
  assert.match(h.c.modal.body, /<option value="person:p1">Alex Morgan<\/option>/);
  assert.match(h.c.modal.body, /<option value="team:t1">Team · Payments<\/option>/);
  assert.match(h.c.modal.body, /<button class="button primary" type="submit">Save<\/button>/);
  h.newPerson(); assert.equal(h.c.modal.title, 'Add a person'); assert.equal(h.c.modal.subtitle, '');
  h.newTeam(); assert.equal(h.c.modal.title, 'Add a team'); assert.equal(h.c.modal.subtitle, '');
}

// 2. A member: the rows are there to read; no End, no Add, no Say what they decide.
{
  const h = harness({admin: false, people, authority});
  const html = h.owners();
  assert.equal(h.canAdminister(), false);
  assert.doesNotMatch(html, /end-authority|data-action="authority"|data-action="person"/);
  assert.doesNotMatch(h.actions.innerHTML, /Add person|Add team/);
  assert.match(h.actions.innerHTML, /2 people/);
  assert.match(text(html), /knows billing · acme\/platform · from an answer · asserted by Marisol Vega/);
  assert.match(text(html), /ended 2020-01-01/);
}

// 3. No record disappears: a team's row and a row for a name with no card go under 'Other'.
{
  const extra = authority.concat([
    {id: 'a4', person_id: '', team_id: 't1', who: 'Payments', is_team: true, role: 'approves', scope_kind: 'repo', scope: '', repo: 'acme/platform', source: 'config', asserted_by: '', accepted: 1, effective_to: ''},
    {id: 'a5', person_id: '', who: 'Gone Person', is_team: false, role: 'decides', scope_kind: 'path', scope: 'legacy/*', repo: '', source: 'config', asserted_by: '', accepted: 0, effective_to: ''},
  ]);
  const h = harness({people, authority: extra});
  const html = h.owners();
  const cards = cardsOf(html);
  assert.equal(cards.length, 3);
  assert.match(cards[2], /<h2>Other<\/h2><h3>Decides<\/h3>/);
  assert.match(text(cards[2]), /^O2 Other Decides Payments \(team\) · must approve everything · acme\/platform · from config End Gone Person · decides legacy\/\* · every repository · from config · \(not yet accepted\) End$/);
  for (const id of ['a1', 'a2', 'a4', 'a5']) assert.match(html, new RegExp(`data-id="${id}"`));
  assert.equal((html.match(/<div class="authority-row/g) || []).length, extra.length, 'every authority row is on a card');
  // A person with nothing recorded says so.
  const h2 = harness({people, authority: []});
  assert.equal((h2.owners().match(/Nothing recorded yet/g) || []).length, 2);
}

// 4. Empty and loading states.
{
  const h = harness({people: [], authority: []});
  const html = h.owners();
  assert.match(html, /<h2>Nobody yet\.<\/h2><p>Add the people who decide, or connect a repository in Settings and Raven finds them\.<\/p><button data-action="person">Add person<\/button>/);
  assert.match(h.actions.innerHTML, /0 people/);
  assert.doesNotMatch(harness({admin: false, people: [], authority: []}).owners(), /data-action="person"/);
  const loading = harness();
  assert.match(loading.owners(), /Loading…/);
  assert.equal(loading.loads.length, 1);
}

// 5. What the spec removed from this page is gone from the source, not hidden.
assert.doesNotMatch(source, /function verifiedLayer|function newOwner|owner-form|'owner'\)|Owners by path pattern|Verified authority outranks what git suggests|Start with the people who know/);
assert.doesNotMatch(source, /function owners\(\)[^]*?workspaceAccountCard\(\)[^]*?function newPerson/);
assert.doesNotMatch(css, /\.owners-page/);
console.log('people-ui-unit: ok');
