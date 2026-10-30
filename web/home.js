'use strict';

// Ask Raven (#ask): the home screen. One question box over everything the
// team has already decided, with the people who hold the context named
// beside each answer, and the coding-agent tasks Raven is working on in
// the background shown as they happen. Nothing here asks anyone to sign.
let askThread = [], askBusy = false, askKey = '';
try { askThread = JSON.parse(sessionStorage.getItem('raven-ask') || '[]').slice(-12); } catch { askThread = []; }
const askSave = () => { try { sessionStorage.setItem('raven-ask', JSON.stringify(askThread.slice(-12))); } catch {} };
const askRepo = () => (state.question_repositories || [])[0]?.id || (state.runs || []).find(r => r.repo)?.repo || '';
const askDate = value => { const d = new Date(value); return Number.isNaN(+d) ? '' : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}); };
const askFirst = name => String(name || '').split(/\s+/)[0];

// A task in a sentence: what the agent ran into, what Raven settled from
// what people already said, and who it asked about the rest.
function taskStory(run) {
  const ds = (state.decisions || []).filter(d => d.run_id === run.id && !d.superseded_by);
  const fromPast = d => ['signed', 'rule'].includes(d.signoff) && d.source_id && d.status !== 'approved';
  const settled = ds.filter(fromPast);
  const answered = ds.filter(d => !fromPast(d) && d.status === 'approved');
  const asked = ds.filter(d => !fromPast(d) && d.status !== 'approved');
  return {ds, settled, answered, asked};
}

function askTaskCard(run) {
  const {ds, settled, answered, asked} = taskStory(run);
  const live = !['completed', 'failed', 'cancelled', 'abandoned'].includes(run.status);
  const who = [...answered.map(d => ({name: d.answered_by || d.owner_name, text: 'answered', done: true})),
    ...asked.filter(d => d.owner_name).map(d => ({name: d.owner_name, text: 'asked', done: false}))];
  const people = [...new Map(who.map(w => [w.name, w])).values()].slice(0, 4);
  const cited = [...new Set(settled.map(d => d.answered_by || d.signed_by).filter(Boolean))];
  return `<a class="ask-task" href="#runs/${encodeURIComponent(run.id)}">
    <div class="ask-task-top"><span class="ask-live ${live ? 'on' : ''}">${live ? 'Live' : 'Done'}</span><span class="ask-agent">${esc(run.agent || 'Coding agent')}${run.requester ? ' · for ' + esc(askFirst(run.requester)) : ''}</span><span class="ask-when">${esc(ago(run.updated_at))}</span></div>
    <strong>${esc(run.title)}</strong>
    <div class="ask-meter" aria-label="${ds.length} decisions">${settled.map(() => '<i class="past"></i>').join('')}${answered.map(() => '<i class="person"></i>').join('')}${asked.map(() => '<i class="open"></i>').join('')}</div>
    <p>${plural(ds.length, 'decision')} · <b>${settled.length}</b> settled from what ${cited.length ? esc(cited.map(askFirst).join(' and ')) + ' already said' : 'the team already said'}${answered.length ? ` · <b>${answered.length}</b> answered` : ''}${asked.length ? ` · <b>${asked.length}</b> with people` : ''}</p>
    ${people.length ? `<div class="ask-task-people">${people.map(p => `<span class="${p.done ? 'done' : ''}">${avatar(p.name)}${esc(askFirst(p.name))} ${p.done ? 'answered' : 'asked'}</span>`).join('')}</div>` : ''}
  </a>`;
}

function askSuggestions() {
  const out = [];
  const decided = (state.decisions || []).filter(d => d.status === 'approved' && d.question && !d.run_id?.startsWith('x')).slice(0, 2);
  for (const d of decided) out.push(d.question.length > 80 ? d.question.slice(0, 78) + '…' : d.question);
  const area = (directory?.authority || []).find(a => a.scope_kind === 'path' && a.role === 'decides');
  if (area) out.push(`Who should I talk to before changing ${area.scope.replace(/\/\*$/, '')}?`);
  const knows = (directory?.authority || []).find(a => a.role === 'knows' && a.note);
  if (knows) out.push(`Who knows the most about ${knows.note.replace(/,.*$/, '').toLowerCase()}?`);
  if (out.length < 3) out.push('What did we decide last month, and who decided it?');
  return [...new Set(out)].slice(0, 4);
}

function askAnswer(entry) {
  if (entry.status === 'thinking') return '<div class="ask-raven"><span class="ask-orb thinking" aria-hidden="true"></span><p class="ask-muted" role="status">Reading what your team has said…</p></div>';
  if (entry.status === 'error') return `<div class="ask-raven"><span class="ask-orb" aria-hidden="true"></span><p class="ask-error" role="alert">${esc(entry.error)}</p></div>`;
  const r = entry.result || {};
  const evidence = r.evidence || [];
  const byId = Object.fromEntries(evidence.map(e => [e.id, e]));
  const said = e => (e.text.match(/Recorded by: (.+)/) || [])[1] || e.author || '';
  const cites = r.model_used ? (r.citations || []).filter(c => byId[c.id]) : evidence.slice(0, 3).map(e => ({id: e.id, quote: e.text.split('\n')[0]}));
  const seen = new Set();
  const quotes = cites.filter(c => !seen.has(c.quote) && seen.add(c.quote)).slice(0, 3).map((c, i) => {
    const e = byId[c.id];
    return `<blockquote class="ask-quote"><p>“${esc(c.quote)}”</p><footer>${said(e) ? `${avatar(said(e))}<b>${esc(said(e))}</b> · ` : ''}${esc(e.title)}${e.updated_at || e.created_at ? ' · ' + esc(askDate(e.updated_at || e.created_at)) : ''}<span class="ask-ref">[${i + 1}]</span></footer></blockquote>`;
  }).join('');
  const lead = r.model_used ? esc(r.answer)
    : evidence.length ? 'Here is what your team has already said about this.'
    : (r.people || []).length ? `Nobody has written this down yet. ${esc(r.people.slice(0, 2).map(p => p.name).join(' and '))} ${r.people.length > 1 ? 'are' : 'is'} who to ask.` : 'Nothing your team has said covers this yet.';
  const people = (r.people || []).length ? `<div class="ask-people"><span class="ask-label">People with context</span>${r.people.map(p => `<a class="ask-person" href="#map/${encodeURIComponent(p.id || '')}">${avatar(p.name)}<span><b>${esc(p.name)}</b><small>${esc(p.why)}</small></span></a>`).join('')}</div>` : '';
  const open = (r.open || []).length ? `<p class="ask-muted">Still open: ${esc(r.open.join('; '))}</p>` : '';
  return `<div class="ask-raven"><span class="ask-orb" aria-hidden="true"></span><div class="ask-body"><p class="ask-lead">${lead}</p>${quotes}${people}${open}</div></div>`;
}

function askThreadView() {
  return askThread.map(entry => `<div class="ask-turn"><div class="ask-you"><p>${esc(entry.q)}</p></div>${askAnswer(entry)}</div>`).join('');
}

function askAgentUses() {
  const connected = (state.agent_connections || []).length > 0;
  const rows = [
    ['Before it builds', 'Claude Code asks Raven what was already decided about the files it is about to touch, and who owns them today, not just who wrote them.'],
    ['While it builds', 'Questions only a person can answer go to that person in Slack or a short call. Claude keeps working on everything else.'],
    ['When it ships', 'The pull request opens with what each person said and where they said it, so review is about the code.'],
  ];
  return `<section class="ask-uses"><h2>Raven in your coding agent</h2>${rows.map(([h, p], i) => `<div class="ask-use"><span>${i + 1}</span><div><strong>${h}</strong><p>${p}</p></div></div>`).join('')}
    <div class="ask-prompt"><span class="ask-label">Try it in Claude Code</span><code>Let merchants buy labels in bulk. Check with Raven first.</code></div>
    ${connected ? '' : '<a class="button small" href="#connect">Connect Claude Code</a>'}</section>`;
}

function askWaiting() {
  const mine = (state.decisions || []).filter(d => typeof assignedToMe === 'function' && assignedToMe(d) && d.status === 'pending');
  if (!mine.length || !state.auth?.enabled) return '';
  const d = mine[0];
  return `<a class="ask-waiting" href="#runs/${encodeURIComponent(d.run_id)}/${encodeURIComponent(d.id)}"><span class="ask-orb small" aria-hidden="true"></span><span><b>Raven has ${plural(mine.length, 'question')} only you can answer.</b><small>${esc(d.question)}</small></span><span aria-hidden="true">→</span></a>`;
}

function renderHome() {
  if (directory === null && typeof loadDirectory === 'function') loadDirectory();
  const live = run => !['completed', 'failed', 'cancelled', 'abandoned'].includes(run.status);
  const runs = (state.runs || []).filter(r => taskStory(r).ds.length > 1 || (live(r) && taskStory(r).ds.length)).slice(0, 3);
  const key = JSON.stringify([askThread, askBusy, runs.map(r => [r.id, r.updated_at]), (state.decisions || []).length, !!directory, (state.agent_connections || []).length]);
  $('.page-head').hidden = true;
  if (key === askKey && $('#app .ask')) return;
  const typed = $('#ask-input')?.value || '';
  const hadFocus = document.activeElement?.id === 'ask-input';
  askKey = key;
  $('.page-head').hidden = true;
  const empty = !askThread.length;
  const name = state.workspace?.name || state.workspace_name || '';
  $('#app').innerHTML = `<div class="ask ${empty ? 'is-empty' : ''}">
    <div class="ask-main">
      ${empty ? `<div class="ask-hero"><span class="ask-orb big" aria-hidden="true"></span><h1>Ask Raven</h1><p>Raven keeps track of what people across ${name ? esc(name) : 'your company'} have decided and who knows what. Your coding agents ask it before they guess, and so can you.</p></div>` : ''}
      ${askWaiting()}
      <div class="ask-thread" id="ask-thread" aria-live="polite">${askThreadView()}</div>
      <form class="ask-composer" id="ask-form"><label class="sr-only" for="ask-input">Ask Raven</label><input id="ask-input" autocomplete="off" maxlength="2000" placeholder="Ask who knows something, or why it was decided…" ${askBusy ? 'disabled' : ''}><button class="button primary" type="submit" ${askBusy ? 'disabled' : ''}>Ask</button></form>
      <div class="ask-chips" ${empty ? '' : 'hidden'}>${askSuggestions().map(s => `<button type="button" class="ask-chip" data-ask="${esc(s)}">${esc(s)}</button>`).join('')}</div>
    </div>
    <aside class="ask-side">
      <section><h2>Working in the background</h2>${runs.length ? runs.map(askTaskCard).join('') : '<p class="ask-muted">When Claude Code, Codex or Cursor starts a task, it shows up here: what Raven settled from past context, and who it asked.</p>'}</section>
      ${askAgentUses()}
    </aside>
  </div>`;
  const input = $('#ask-input');
  if (input) { input.value = typed; if (hadFocus || (!askBusy && !empty)) input.focus({preventScroll: true}); }
  const thread = $('#ask-thread');
  if (thread && thread.lastElementChild) thread.lastElementChild.scrollIntoView({block: 'nearest'});
}

async function askRaven(question) {
  question = question.trim();
  if (!question || askBusy) return;
  const entry = {q: question, status: 'thinking'};
  askThread.push(entry); askBusy = true; askKey = ''; render();
  try {
    entry.result = await api('/api/ask', {question, repository: askRepo()});
    entry.status = 'done';
  } catch (error) {
    entry.status = 'error'; entry.error = error.message;
  } finally {
    askBusy = false; askSave(); askKey = ''; render();
  }
}

document.addEventListener('submit', event => {
  if (event.target.id !== 'ask-form') return;
  event.preventDefault();
  const input = $('#ask-input');
  const q = input.value;
  input.value = '';
  askRaven(q);
});
document.addEventListener('click', event => {
  const chip = event.target.closest('[data-ask]');
  if (!chip || view !== 'ask') return;
  event.preventDefault();
  askRaven(chip.dataset.ask);
});
window.addEventListener('hashchange', () => { askKey = ''; });
