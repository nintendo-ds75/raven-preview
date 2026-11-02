'use strict';

// Ask Raven (#ask): the home screen. One question box over everything the
// team has already decided, with the people who hold the context named
// beside each answer, and the coding-agent tasks Raven is working on in
// the background shown as they happen. Nothing here asks anyone to sign.
let askThread = [], askBusy = false, askKey = '', askStick = true;
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
    <div class="ask-meter" aria-label="${plural(ds.length, 'decision')}">${settled.map(() => '<i class="past"></i>').join('')}${answered.map(() => '<i class="person"></i>').join('')}${asked.map(() => '<i class="open"></i>').join('')}</div>
    <p>${plural(ds.length, 'decision')} · <b>${settled.length}</b> settled from what ${cited.length ? esc(cited.map(askFirst).join(' and ')) + ' already said' : 'the team already said'}${answered.length ? ` · <b>${answered.length}</b> answered` : ''}${asked.length ? ` · <b>${asked.length}</b> with people` : ''}</p>
    ${people.length ? `<div class="ask-task-people">${people.map(p => `<span class="${p.done ? 'done' : ''}">${avatar(p.name)}${esc(askFirst(p.name))} ${p.done ? 'answered' : 'asked'}</span>`).join('')}</div>` : ''}
  </a>`;
}

// Tasks being built right now: live coding-agent tasks Raven is part of.
const taskLive = run => !['completed', 'failed', 'cancelled', 'abandoned'].includes(run.status);
function askBuilding() {
  return (state.runs || []).filter(r => taskLive(r) && taskStory(r).ds.length)
    .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
}

// Where a decision's context came from, as short labels: the earlier
// decision it was settled from, and the records the agent cited (an ADR,
// a pull request, a postmortem, a Slack thread, a Jira issue, a file).
function buildSources(d) {
  const out = [];
  if (d.source_id) {
    const prior = (state.decisions || []).find(x => x.id === d.source_id);
    const who = prior?.answered_by || d.answered_by || d.signed_by || '';
    out.push({label: `${who ? askFirst(who) + '’s' : 'An earlier'} decision${d.source_revision ? ' · ' + askDate(d.source_revision) : ''}`, href: prior ? `#runs/${encodeURIComponent(prior.run_id)}/${encodeURIComponent(prior.id)}` : '', kind: 'decision'});
  }
  const text = `${d.context || ''}\n${d.evidence || ''}`;
  const seen = new Set();
  const add = (label, kind) => { const k = label.toLowerCase(); if (!seen.has(k) && out.length < 5) { seen.add(k); out.push({label, kind}); } };
  for (const m of text.matchAll(/\bADR\s?0*(\d+)/gi)) add('ADR ' + m[1].padStart(4, '0'), 'doc');
  for (const m of text.matchAll(/\bPR\s?#(\d+)/gi)) add('PR #' + m[1], 'github');
  for (const m of text.matchAll(/\bpostmortem(?:\s+\d{4}-\d{2}-\d{2})?/gi)) add('Postmortem' + m[0].slice(10), 'doc');
  for (const m of text.matchAll(/\bslack\s+(#[\w-]+)/gi)) add(m[1].slice(1), 'slack');
  for (const m of text.matchAll(/\b([A-Z][A-Z0-9]{1,9}-\d+)\b/g)) add(m[1], 'jira');
  // Files, but not an ADR's own file when the ADR is already named.
  for (const m of text.matchAll(/\b((?:app|web|infra|docs|tests)\/[\w./-]+?\.\w+)(?::\d+)?/g)) {
    if (/docs\/adr\//.test(m[1]) && out.some(x => x.label.startsWith('ADR'))) continue;
    add(m[1].split('/').slice(-2).join('/'), 'code');
  }
  return out;
}

// One decision, in three parts: which camp it is in (strong precedent,
// close precedent or novel judgement), its status and who Raven talked
// to, and where the information came from.
function buildRow(d, settled) {
  const camp = settled || d.kind === 'evidence' ? ['precedent', 'Strong precedent', 'Raven settled this from what the team already decided. Nobody was asked.']
    : d.kind === 'prediction' ? ['close', 'Close precedent', 'Raven had a likely answer from earlier context and asked the owner to confirm it.']
    : ['novel', 'Novel judgement', 'Nothing on record covered this. Raven asked the person who owns it to decide from first principles.'];
  const owner = d.owner_name || '';
  const by = d.answered_by || d.signed_by || '';
  let status;
  if (settled || d.kind === 'evidence') status = `Settled from ${esc(askFirst(by) || 'an earlier')}’s decision · nobody asked`;
  else if (d.status === 'approved') status = `Raven asked ${esc(askFirst(owner) || askFirst(by))}${by && owner && by !== owner ? `, ${esc(askFirst(by))} answered` : ' · answered'} ${esc(ago(d.updated_at))}`;
  else status = owner ? `Raven asked ${esc(askFirst(owner))} · waiting ${esc(ago(d.created_at)).replace(/ ago$/, '')}` : 'Raven is finding who owns this';
  const done = settled || d.kind === 'evidence' || d.status === 'approved';
  const sources = buildSources(d);
  const who = done ? by || owner : owner;
  return `<li class="build-row ${camp[0]} ${done ? 'done' : 'waiting'}">
    <div class="build-row-top"><span class="build-camp ${camp[0]}" title="${esc(camp[2])}">${camp[1]}</span><span class="build-status">${who ? avatar(who) : ''}${status}</span></div>
    <a class="build-q" href="#runs/${encodeURIComponent(d.run_id)}/${encodeURIComponent(d.id)}">${esc(d.question)}</a>
    ${done && d.answer ? `<p class="build-a">${esc(d.answer.length > 160 ? d.answer.slice(0, 158) + '…' : d.answer)}</p>` : ''}
    ${sources.length ? `<div class="build-from"><span>From</span>${sources.map(x => x.href ? `<a class="build-src ${x.kind}" href="${esc(x.href)}">${esc(x.label)}</a>` : `<span class="build-src ${x.kind}">${esc(x.label)}</span>`).join('')}</div>` : ''}
  </li>`;
}

function buildCard(run) {
  const {ds, settled, answered, asked} = taskStory(run);
  const novel = ds.filter(d => !settled.includes(d) && d.kind !== 'evidence' && d.kind !== 'prediction').length;
  const precedent = ds.length - novel;
  const talked = [...new Set(ds.filter(d => !settled.includes(d) && d.kind !== 'evidence').map(d => d.owner_name).filter(Boolean))];
  const order = d => (settled.includes(d) || d.kind === 'evidence') ? 2 : d.status === 'approved' ? 1 : 0;
  const rows = [...ds].sort((a, b) => order(a) - order(b) || String(a.created_at).localeCompare(String(b.created_at)));
  return `<article class="build-card">
    <div class="ask-task-top"><span class="ask-live on">Building</span><span class="ask-agent">${esc(run.agent || 'Coding agent')}${run.requester ? ' · picked up by ' + esc(run.requester) : ''}</span><span class="ask-when">${esc(ago(run.updated_at))}</span></div>
    <h2><a href="#runs/${encodeURIComponent(run.id)}">${esc(run.title)}</a></h2>
    ${run.goal ? `<p class="build-goal">${esc(run.goal.length > 260 ? run.goal.slice(0, 258) + '…' : run.goal)}</p>` : ''}
    <div class="build-stats">
      <div><b>${precedent}</b><span>from precedent</span></div>
      <div><b>${novel}</b><span>novel judgement</span></div>
      <div><b>${answered.length + settled.length}</b><span>of ${ds.length} settled</span></div>
      <div><b>${asked.length}</b><span>waiting on people</span></div>
    </div>
    <div class="ask-meter" aria-hidden="true">${settled.map(() => '<i class="past"></i>').join('')}${answered.map(() => '<i class="person"></i>').join('')}${asked.map(() => '<i class="open"></i>').join('')}</div>
    ${talked.length ? `<p class="build-talked">Raven talked to ${talked.map(n => `<span>${avatar(n)}${esc(askFirst(n))}</span>`).join('')}</p>` : ''}
    <ul class="build-rows">${rows.map(d => buildRow(d, settled.includes(d))).join('')}</ul>
  </article>`;
}

function renderTasks() {
  $('.page-head').hidden = true;
  const live = askBuilding();
  const me = (state.me?.name || '').toLowerCase();
  const mine = me ? live.filter(r => String(r.requester || '').toLowerCase() === me) : [];
  const rest = live.filter(r => !mine.includes(r));
  const done = (state.runs || []).filter(r => r.status === 'completed' && taskStory(r).ds.length).slice(0, 5);
  const key = JSON.stringify(['tasks', live.map(r => [r.id, r.updated_at]), done.map(r => r.id), (state.decisions || []).map(d => [d.id, d.status]).length, me]);
  if (key === askKey && $('#app .build')) return;
  askKey = key;
  $('#app').innerHTML = `<div class="build">
    <header class="build-head"><h1>Being built</h1><p>Every judgement call in the tasks agents are building now: settled from precedent or a novel call for a person, who Raven talked to, and where each answer came from.</p></header>
    ${mine.length ? `<h2 class="ask-label">Yours</h2>${mine.map(buildCard).join('')}` : ''}
    ${rest.length ? `${mine.length ? '<h2 class="ask-label">Across the team</h2>' : ''}${rest.map(buildCard).join('')}` : ''}
    ${live.length ? '' : '<p class="ask-muted">Nothing is being built right now. When Claude Code, Codex or Cursor starts a task, it shows up here.</p>'}
    ${done.length ? `<h2 class="ask-label">Recently finished</h2><div class="build-done">${done.map(r => `<a href="#runs/${encodeURIComponent(r.id)}"><span>${esc(r.title)}</span><small>${plural(taskStory(r).ds.length, 'decision')} · ${esc(askDate(r.updated_at))}</small></a>`).join('')}</div>` : ''}
    ${askAgentUses()}
  </div>`;
}
window.addEventListener('hashchange', () => { if (location.hash.startsWith('#tasks')) askKey = ''; });

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

// People named in an answer become chips that open their page.
function askMentions(html, people) {
  const known = [...new Map(people.filter(p => p.name && p.id).map(p => [p.name, p])).values()].sort((a, b) => b.name.length - a.name.length);
  for (const p of known) {
    const name = esc(p.name).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    html = html.replace(new RegExp(`(^|[^\\w>])(${name})(?![\\w<])`, 'g'), (m, pre) => `${pre}<a class="ask-mention" href="#people/${encodeURIComponent(p.id)}">${avatar(p.name)}${esc(p.name)}</a>`);
  }
  return html;
}
// The answer as written for scanning: a lead sentence, then short points.
function askProse(text, people) {
  const lines = String(text || '').split('\n').map(l => l.trim()).filter(Boolean);
  const points = lines.filter(l => /^[-•*]\s+/.test(l)).map(l => l.replace(/^[-•*]\s+/, ''));
  const lead = lines.filter(l => !/^[-•*]\s+/.test(l)).join(' ');
  return `${lead ? `<p class="ask-lead">${askMentions(esc(lead), people)}</p>` : ''}${points.length ? `<ul class="ask-points">${points.map(p => `<li>${askMentions(esc(p), people)}</li>`).join('')}</ul>` : ''}`;
}

// A record's line in an answer: who, how they said it, when. The
// question a decision answered is left out: it is close to what was just
// asked, and repeated on every quote it made the answer twice as long.
const askSaid = e => e.author || (e.text.match(/Recorded by: ([^(\n]+)/) || [])[1]?.trim() || '';
const askHow = e => e.kind === 'prior_decision' ? (String(e.ref || '').startsWith('call:') ? 'decided on a call' : 'decided')
  : e.kind === 'person_note' ? ({'intro chat': 'said in a voice intro', 'from a meeting': 'said in a meeting', 'via an agent': 'told an agent', 'from a call': 'said on a call'}[e.via] || 'told Raven')
  : e.kind === 'profile' ? 'on Raven' : 'wrote';
// Lines that only say who signed or recorded a decision repeat its byline.
const askBylineOnly = quote => /^(Signed by|Recorded by):/i.test(String(quote).trim());

function askSources(r) {
  const evidence = r.evidence || [];
  const byId = Object.fromEntries(evidence.map(e => [e.id, e]));
  const cites = r.model_used ? (r.citations || []).filter(c => byId[c.id])
    : evidence.filter(e => e.kind !== 'profile').slice(0, 3).map(e => ({id: e.id, quote: e.text.split('\n')[0]}));
  const groups = new Map();
  for (const c of cites) {
    if (askBylineOnly(c.quote)) { if (!groups.has(c.id)) groups.set(c.id, []); continue; }
    const list = groups.get(c.id) || [];
    if (!list.includes(c.quote)) list.push(c.quote);
    groups.set(c.id, list);
  }
  const rows = [...groups.entries()].slice(0, 3).map(([id, quotes]) => {
    const e = byId[id], who = askSaid(e);
    const open = e.ref ? `data-source="${esc(e.ref)}" data-quote="${esc(quotes[0] || '')}"` : e.person_id ? `data-href="#people/${esc(e.person_id)}"` : e.url ? `data-href="${esc(e.url)}"` : '';
    const where = e.kind === 'person_note' ? (e.source_title || '') : e.kind === 'source' ? e.title : '';
    const when = askDate(e.updated_at || e.created_at || e.version);
    return `<div class="ask-src ask-quote" ${open} tabindex="0" role="button" aria-label="Open the source" title="${esc(e.title || '')}">
      <div class="ask-src-head">${who ? `${avatar(who)}<b>${esc(who)}</b> ` : ''}<span>${esc(askHow(e))}${where ? ' · ' + esc(where.length > 60 ? where.slice(0, 58) + '…' : where) : ''}${when ? ' · ' + esc(when) : ''}</span><span class="ask-ref" aria-hidden="true">↗</span></div>
      ${quotes.slice(0, 2).map(q => `<p>“${esc(q)}”</p>`).join('')}</div>`;
  }).join('');
  const refs = (r.workspace_refs || []).map(x => `<div class="ask-src ask-quote" ${x.task_id ? `data-href="#runs/${esc(x.task_id)}"` : ''} tabindex="0" role="button">
      <div class="ask-src-head"><span>${esc(x.title)}</span>${x.task_id ? '<span class="ask-ref" aria-hidden="true">↗</span>' : ''}</div><p>“${esc(x.quote)}”</p></div>`).join('');
  return rows + refs ? `<div class="ask-sources">${rows}${refs}</div>` : '';
}

function askAnswer(entry) {
  if (entry.status === 'thinking') return `<div class="ask-raven"><span class="ask-orb thinking" aria-hidden="true"></span><p class="ask-muted" role="status">${entry.slow ? 'Still reading. Longer questions can take a minute…' : 'Reading what your team has said…'}</p></div>`;
  if (entry.status === 'error') return `<div class="ask-raven"><span class="ask-orb" aria-hidden="true"></span><div class="ask-body"><p class="ask-error" role="alert">${esc(entry.error)}</p><button type="button" class="button small text" data-ask-retry="${esc(entry.q)}">Ask again</button></div></div>`;
  const r = entry.result || {};
  const evidence = r.evidence || [];
  const sourced = new Set(evidence.map(askSaid).filter(Boolean).map(n => n.toLowerCase()));
  const mentionable = [...(r.people || []), ...evidence.filter(e => e.person_id).map(e => ({name: askSaid(e), id: e.person_id}))];
  const profile = evidence.find(e => e.kind === 'profile');
  let lead;
  if (r.model_used || r.grounding === 'workspace') lead = askProse(r.answer, mentionable);
  else if (profile) { const [first, ...rest] = profile.text.split('\n'); lead = askProse([first, ...rest.slice(0, 3).map(l => '- ' + l)].join('\n'), mentionable); }
  else lead = `<p class="ask-lead">${evidence.length ? 'Here is what your team has already said about this.'
    : (r.people || []).length ? `Nobody has written this down yet. ${askMentions(esc(r.people.slice(0, 2).map(p => p.name).join(' and ')), mentionable)} ${r.people.length > 1 ? 'are' : 'is'} who to ask.` : 'Nobody has written this down yet.'}</p>`;
  const note = r.grounding === 'workspace' ? '<p class="ask-note">Nobody has recorded an answer to this, so this is Raven’s read from what it knows about the team.</p>' : '';
  // People the sources already name are not listed again.
  const others = (r.people || []).filter(p => !sourced.has(String(p.name).toLowerCase()));
  const people = others.length ? `<div class="ask-who"><span>Also ask</span>${others.slice(0, 4).map(p => `<a class="ask-person-chip" href="#people/${encodeURIComponent(p.id || '')}" title="${esc(p.why)}">${avatar(p.name)}${esc(p.name)}</a>`).join('')}</div>` : '';
  const open = (r.open || []).length ? `<p class="ask-muted ask-open">Still open: ${esc(r.open.join('; '))}</p>` : '';
  return `<div class="ask-raven"><span class="ask-orb" aria-hidden="true"></span><div class="ask-body">${lead}${note}${askSources(r)}${people}${open}</div></div>`;
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

// Raven noticed: what the background pass found when it read what people
// said next to the code. Each finding quotes who said it and offers one
// next step: route the question to the right person, or hand it to Claude.
let noticed = null, noticedAt = 0, noticedBusy = '';
function loadNoticed(force) {
  if (!force && Date.now() - noticedAt < 8000) return;
  noticedAt = Date.now();
  api('/api/muse').then(r => { const before = JSON.stringify(noticed); noticed = r; if (JSON.stringify(r) !== before) { askKey = ''; if (view === 'ask') render(); } })
    .catch(() => { noticed = noticed || {insights: []}; });
}
function noticedCard(x) {
  const cites = x.evidence.slice(0, 2).map(e => `<p class="noticed-cite">“${esc(e.quote)}” <small>${e.person_id ? `<a href="#people/${esc(e.person_id)}">${esc(e.who)}</a>` : esc(e.who)}, ${esc(e.kind === 'decided' ? 'answering an agent' : (e.where === 'intro chat' ? 'voice intro' : e.where))} · ${esc(askDate(e.at))}</small></p>`).join('');
  const who = x.person ? askFirst(x.person.name) : '';
  return `<article class="noticed-card" data-kind="${esc(x.kind)}"><span class="noticed-kind">${esc(x.kind_label)}</span><h3>${esc(x.title)}</h3><p>${esc(x.body)}</p>${cites}
    ${x.paths.length ? `<p class="noticed-paths">Raven read ${x.paths.slice(0, 3).map(p => `<code>${esc(p)}</code>`).join(' ')}</p>` : ''}
    <div class="noticed-actions">${x.task_id ? `<a class="button small" href="#runs/${encodeURIComponent(x.task_id)}">Asked${who ? ' · see ' + esc(who) + "'s answer" : ''}</a>`
      : `<button type="button" class="button small primary" data-noticed="ask" data-id="${esc(x.id)}" ${noticedBusy ? 'disabled' : ''}>${who ? 'Ask ' + esc(who) : 'Ask the right person'}</button>`}
      ${x.agent_prompt ? `<button type="button" class="button small" data-noticed="claude" data-id="${esc(x.id)}">Hand to Claude</button>` : ''}
      <button type="button" class="button small text" data-noticed="dismiss" data-id="${esc(x.id)}">Dismiss</button></div></article>`;
}
function noticedView() {
  const items = (noticed?.insights || []).slice(0, 3);
  const last = noticed?.last_pass;
  const looking = noticedBusy === 'run' || noticed?.running;
  return `<section class="noticed"><div class="noticed-head"><span class="ask-orb small ${looking ? 'thinking' : ''}" aria-hidden="true"></span><h2 class="ask-label">Raven noticed</h2>
    <button type="button" class="button small text" data-noticed="run" ${looking ? 'disabled' : ''}>${looking ? 'Looking…' : 'Look again'}</button></div>
    ${last && !looking ? `<p class="noticed-last">Read ${plural(last.said, 'thing')} people said, ${plural(last.decided, 'decision')} and ${plural(last.files, 'file')} · ${esc(ago(last.at))}</p>` : ''}
    ${items.length ? items.map(noticedCard).join('') : `<p class="ask-muted">${looking ? 'Reading what people said next to the code…' : 'When people tell Raven something the product doesn\'t do yet, it shows up here.'}</p>`}</section>`;
}

function renderHome() {
  loadNoticed();
  if (directory === null && typeof loadDirectory === 'function') loadDirectory();
  const building = askBuilding();
  const key = JSON.stringify([noticed, noticedBusy, askThread, askBusy, building.map(r => [r.id, r.updated_at]), !!directory]);
  $('.page-head').hidden = true;
  if (key === askKey && $('#app .ask')) return;
  const typed = $('#ask-input')?.value || '';
  const hadFocus = document.activeElement?.id === 'ask-input';
  const oldThread = $('#ask-thread');
  const keepTop = oldThread && !askStick ? oldThread.scrollTop : null;
  askKey = key;
  const empty = !askThread.length;
  const name = state.workspace?.name || state.workspace_name || '';
  $('#app').innerHTML = `<div class="ask ${empty ? 'is-empty' : ''}">
    <div class="ask-main">
      <div class="ask-thread" id="ask-thread" aria-live="polite">
        ${empty ? `<div class="ask-hero"><span class="ask-orb big" aria-hidden="true"></span><h1>Ask Raven</h1><p>Raven keeps track of what people across ${name ? esc(name) : 'your company'} have decided and who knows what. Your coding agents ask it before they guess, and so can you.</p></div>` : ''}
        ${askWaiting()}
        ${askThreadView()}
      </div>
      <div class="ask-dock">
        <form class="ask-composer" id="ask-form"><label class="sr-only" for="ask-input">Ask Raven</label><input id="ask-input" autocomplete="off" maxlength="2000" placeholder="Ask who knows something, or why it was decided…" ${askBusy ? 'disabled' : ''}><button class="button primary" type="submit" ${askBusy ? 'disabled' : ''}>Ask</button></form>
        <div class="ask-chips" ${empty ? '' : 'hidden'}>${askSuggestions().map(s => `<button type="button" class="ask-chip" data-ask="${esc(s)}">${esc(s)}</button>`).join('')}</div>
      </div>
    </div>
    <aside class="ask-side">
      ${noticedView()}
      ${building.length ? `<a class="ask-building" href="#tasks"><span class="ask-live on">Live</span><span>${plural(building.length, 'task')} being built</span><span aria-hidden="true">→</span></a>` : ''}
    </aside>
  </div>`;
  const input = $('#ask-input');
  if (input) { input.value = typed; if (hadFocus || (!askBusy && !empty)) input.focus({preventScroll: true}); }
  const thread = $('#ask-thread');
  if (thread) {
    // Pinned to the newest message unless the reader scrolled up to read.
    if (keepTop !== null) thread.scrollTop = keepTop;
    else thread.scrollTop = thread.scrollHeight;
    thread.addEventListener('scroll', () => { askStick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 60; }, {passive: true});
  }
}

// The reading runs on the server as a job; the page polls for it. One
// long request was what showed "Failed to fetch": the claude CLI can take
// minutes, and a browser or proxy drops a request held open that long.
const askPause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function askRaven(question) {
  question = question.trim();
  if (!question || askBusy) return;
  const entry = {q: question, status: 'thinking'};
  askThread.push(entry); askBusy = true; askStick = true; askKey = ''; render();
  const started = Date.now();
  try {
    const job = await api('/api/ask', {question, repository: askRepo(), background: true});
    let misses = 0;
    for (;;) {
      if (job.status === 'done' || job.status === 'error') break;
      await askPause(Date.now() - started < 10000 ? 900 : 2000);
      try { Object.assign(job, await api('/api/ask/' + encodeURIComponent(job.job))); misses = 0; }
      catch (error) {
        // A dropped poll is retried; the reading is still going on the server.
        if (/gone/i.test(error.message) || ++misses > 5) throw error;
      }
      if (!entry.slow && Date.now() - started > 15000) { entry.slow = true; askKey = ''; render(); }
    }
    if (job.status === 'error') throw new Error(job.error || 'Raven could not finish reading this.');
    entry.result = job.result;
    entry.status = 'done';
  } catch (error) {
    entry.status = 'error';
    entry.error = /failed to fetch|networkerror|load failed/i.test(error.message)
      ? 'Raven lost the connection to the server. Check that it is still running, then ask again.' : error.message;
  } finally {
    delete entry.slow;
    askBusy = false; askStick = true; askSave(); askKey = ''; render();
  }
}
document.addEventListener('click', event => {
  const retry = event.target.closest('[data-ask-retry]');
  if (!retry || view !== 'ask') return;
  askThread = askThread.filter(e => !(e.status === 'error' && e.q === retry.dataset.askRetry));
  askRaven(retry.dataset.askRetry);
});

document.addEventListener('submit', event => {
  if (event.target.id !== 'ask-form') return;
  event.preventDefault();
  const input = $('#ask-input');
  const q = input.value;
  input.value = '';
  askRaven(q);
});
document.addEventListener('click', event => {
  const quote = event.target.closest('.ask-quote[data-href]');
  if (quote) { const href = quote.dataset.href; if (href.startsWith('#')) location.hash = href; else window.open(href, '_blank', 'noopener'); }
});
document.addEventListener('keydown', event => {
  const quote = event.target.closest?.('.ask-quote');
  if (quote && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); quote.click(); }
});
document.addEventListener('click', event => {
  const chip = event.target.closest('[data-ask]');
  if (!chip || view !== 'ask') return;
  event.preventDefault();
  askRaven(chip.dataset.ask);
});
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-noticed]');
  if (!button || view !== 'ask') return;
  event.preventDefault();
  const action = button.dataset.noticed, id = button.dataset.id;
  const item = (noticed?.insights || []).find(x => x.id === id);
  if (action === 'claude') {
    const text = `${item.agent_prompt}\n\n(Raven noticed ${id}; bridge_raven_noticed has the quotes.)`;
    try { await navigator.clipboard.writeText(text); notify('Copied for Claude Code. Paste it, or ask Claude "what did Raven notice?"'); }
    catch { notify(item.agent_prompt); }
    return;
  }
  noticedBusy = action; askKey = ''; render();
  try {
    if (action === 'run') {
      const result = await api('/api/muse/run', {});
      notify(result.added.length ? `Raven noticed ${plural(result.added.length, 'thing')} worth a look.` : 'Nothing new since the last look.');
    } else {
      const result = await api(`/api/muse/${encodeURIComponent(id)}/${action}`, {});
      if (action === 'ask' && result.task_id) notify(`Raven asked ${result.node?.owner || 'the right person'}.`);
    }
  } catch (error) { notify(error.message); }
  finally { noticedBusy = ''; loadNoticed(true); askKey = ''; render(); }
});
window.addEventListener('hashchange', () => { askKey = ''; });
