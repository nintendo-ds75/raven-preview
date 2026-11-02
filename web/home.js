'use strict';

// Ask Raven (#ask): the home screen. One question box over everything the
// team has already decided, with the people who hold the context named
// beside each answer, and the coding-agent tasks Raven is working on in
// the background shown as they happen. Nothing here asks anyone to sign.
let askThread = [], askBusy = false, askKey = '', askStick = true;
try { askThread = JSON.parse(sessionStorage.getItem('raven-ask') || '[]').slice(-12); } catch { askThread = []; }
// A reading cut off by a reload is said to be cut off, with a retry, not left spinning.
for (const e of askThread) if (e.status === 'thinking') Object.assign(e, {status: 'error', error: 'This answer was interrupted when the page reloaded.'});
const askSave = () => { try { sessionStorage.setItem('raven-ask', JSON.stringify(askThread.slice(-12))); } catch {} };
const askRepo = () => (state.question_repositories || [])[0]?.id || (state.runs || []).find(r => r.repo)?.repo || '';
const askDate = value => { const d = new Date(value); return Number.isNaN(+d) ? '' : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}); };
const askFirst = name => String(name || '').split(/\s+/)[0];

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

// Where a decision's context came from, as short labels: the records the
// agent cited (an ADR, a pull request, a postmortem, a Slack thread, a Jira
// issue, a file). The earlier decision it was settled from is shown apart.
function buildSources(d) {
  const out = [];
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

// The judgment log. Each decision is read into one of six plain states:
//   checking  Raven is still reading it: the static pass found a match or a person, and
//             the model pass has not confirmed it, so nobody has been asked or spared yet
//   settled   Raven applied a call a person already made; nobody was asked
//   answered  a person answered (Raven may have guessed first and asked them to confirm)
//   corrected a person said Raven's call was wrong, or did not apply, and gave the answer
//   review    the call this leaned on was replaced since, so it needs another look
//   waiting   Raven routed it to a person and is waiting
const buildById = id => (state.decisions || []).find(x => x.id === id);
const buildVia = {'browser': 'in Raven', 'slack': 'on Slack', 'teams': 'on Teams', 'voice call': 'on a Raven voice call',
  'call (typed)': 'on a Raven call', 'meeting': 'in a meeting', 'github': 'in a GitHub review', 'agent': 'in Claude Code', 'operator': 'in Raven'};
const buildWhen = value => { const d = new Date(value); return Number.isNaN(+d) ? '' : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}); };
const buildHref = d => `#runs/${encodeURIComponent(d.run_id)}/${encodeURIComponent(d.id)}`;
function judge(d) {
  const prior = d.source_id ? buildById(d.source_id) : d.supersedes ? buildById(d.supersedes) : null;
  const by = d.answered_by || d.signed_by || '';
  const priorBy = prior ? (prior.answered_by || prior.signed_by || prior.owner_name || '') : by;
  // When the earlier call was made: the revision Raven applied, else the
  // call's own date (its updated_at moves when it is later replaced).
  const priorWhen = d.source_revision || (prior ? prior.created_at : '');
  let kind;
  // A read still in progress is neither settled nor waiting on anyone:
  // measured on a dry run, a static match shown as "nobody asked" was
  // withdrawn by the model pass a minute later.
  if (d.model_pending && d.status !== 'approved') kind = 'checking';
  else if (d.needs_review) kind = 'review';
  else if (d.status === 'approved' && d.supersedes) kind = 'corrected';
  else if (['signed', 'rule'].includes(d.signoff) && d.source_id && d.status !== 'approved') kind = 'settled';
  else if (d.status === 'approved') kind = 'answered';
  else kind = 'waiting';
  return {kind, prior, by, priorBy, priorWhen, guessed: d.kind === 'prediction', via: buildVia[d.answered_via] || '',
    owner: d.owner_name || '', done: kind !== 'waiting' && kind !== 'review'};
}

// A task in a sentence: what the agent ran into, what Raven settled from
// what people already said, and who it asked about the rest.
function taskStory(run) {
  const ds = (state.decisions || []).filter(d => d.run_id === run.id && !d.superseded_by
    && !['duplicate', 'suggested', 'adopted', 'withdrawn'].includes(d.status));
  const js = new Map(ds.map(d => [d.id, judge(d)]));
  const of = kind => ds.filter(d => js.get(d.id).kind === kind);
  const settled = of('settled');
  const answered = [...of('answered'), ...of('corrected')];
  const asked = [...of('waiting'), ...of('review')];
  const checking = of('checking');
  // People Raven reached (asked or answered) and people it did not need to
  // bother because a call they already made covered the question.
  const reached = [...new Set([...answered.map(d => js.get(d.id).by || js.get(d.id).owner), ...asked.map(d => js.get(d.id).owner)].filter(Boolean))];
  const spared = [...new Set(settled.map(d => js.get(d.id).priorBy).filter(n => n && !reached.includes(n)))]
    .map(name => ({name, when: buildWhen(settled.map(d => js.get(d.id)).find(j => j.priorBy === name)?.priorWhen)}));
  return {ds, js, settled, answered, asked, checking, reached, spared};
}

const buildOpen = new Set(), buildForm = new Set();
const buildCanAct = d => !!state.me?.name && (state.me.name === d.owner_name || state.me.role === 'admin');
const buildClip = (text, n) => { text = String(text || ''); return text.length > n ? text.slice(0, n - 1) + '…' : text; };

// The one line under each question: who settled it, how, and when.
function buildLine(d, j) {
  const first = askFirst;
  if (j.kind === 'settled') return `<span class="dot past"></span>From ${esc(first(j.priorBy) || 'an earlier')}’s call${j.priorWhen ? ' · ' + esc(buildWhen(j.priorWhen)) : ''} · nobody asked`;
  if (j.kind === 'corrected') return `<span class="dot person"></span>${esc(first(j.by))} corrected this${j.prior ? ` · replaces ${esc(first(j.priorBy))}’s ${esc(buildWhen(j.priorWhen))} call` : ''}`;
  if (j.kind === 'answered') return `<span class="dot person"></span>${j.guessed ? `Raven guessed, ${esc(first(j.by))} confirmed` : `${esc(first(j.by))} answered`}${j.via ? ' ' + esc(j.via) : ''} · ${esc(ago(d.updated_at))}`;
  if (j.kind === 'review') return `<span class="dot open"></span>Needs another look · the call this used was replaced`;
  if (j.kind === 'checking') return `<span class="dot checking"></span>Raven is still reading this${j.priorBy ? ` · an earlier call by ${esc(first(j.priorBy))} may cover it` : ''} · nobody asked yet`;
  return `<span class="dot open"></span>${j.owner ? `Waiting on ${esc(first(j.owner))} · ${esc(ago(d.created_at)).replace(/ ago$/, '')}` : 'Raven is finding who owns this'}`;
}

// What opens when a tile is clicked: the answer, why Raven trusted it, the
// earlier call it came from, where the rest was read, and the one action.
function buildDetail(d, j) {
  const first = askFirst;
  const parts = [];
  if (d.answer && j.kind !== 'review' && j.kind !== 'checking') parts.push(`<p class="build-answer">${esc(d.answer)}</p>`);
  if (j.kind === 'settled') {
    parts.push(`<div class="build-why"><span class="ask-label">Why Raven applied it</span><p>${esc(d.fit_reason || `Raven matched it to a call ${j.priorBy || 'someone'} already made.`)}</p></div>`);
    parts.push(buildPrecedent(j, 'Settled from this call'));
    if (j.priorBy) parts.push(`<p class="build-note">${esc(first(j.priorBy))} was not pinged: the call already covered it. ${buildCanAct(d) ? 'If it doesn’t, say so below.' : `Only ${esc(first(j.priorBy))} (or an admin) can change that.`}</p>`);
  } else if (j.kind === 'corrected') {
    if (d.rationale) parts.push(`<div class="build-why"><span class="ask-label">Why</span><p>${esc(d.rationale)}</p></div>`);
    if (j.prior) parts.push(buildPrecedent(j, 'Replaced this call'));
    parts.push(`<p class="build-note">From now on the next similar question uses ${esc(first(j.by))}’s correction.</p>`);
  } else if (j.kind === 'answered') {
    if (j.guessed) parts.push(`<p class="build-note">Raven had a likely answer from earlier context but was not sure, so it asked ${esc(first(j.by))} rather than apply it.</p>`);
    if (d.rationale) parts.push(`<div class="build-why"><span class="ask-label">Why, in ${esc(first(j.by))}’s words</span><p>${esc(d.rationale)}</p></div>`);
    parts.push(`<p class="build-note">${esc(j.by)} answered${j.via ? ' ' + esc(j.via) : ''} · ${esc(buildWhen(d.updated_at))}.</p>`);
  } else if (j.kind === 'checking') {
    parts.push(`<p class="build-note">Raven's first pass ${j.priorBy ? `found a call ${esc(first(j.priorBy))} made that may cover this` : 'found who may own this'}; the model pass is reading it now. Nobody has been asked, and nothing is settled, until that read finishes.</p>`);
    if (d.context) parts.push(`<div class="build-why"><span class="ask-label">What the agent found</span><p>${esc(buildClip(d.context, 320))}</p></div>`);
  } else if (j.kind === 'review') {
    parts.push(`<div class="build-why"><span class="ask-label">What happened</span><p>${esc(d.review_reason || 'An answer this leaned on was corrected.')}</p></div>`);
    if (d.answer) parts.push(`<p class="build-answer stale">Was: ${esc(d.answer)}</p>`);
    parts.push(`<p class="build-note">Raven is asking ${esc(first(j.owner) || 'the owner')} to look again.</p>`);
  } else {
    parts.push(`<p class="build-note">Raven asked ${esc(j.owner || 'the owner')} ${esc(ago(d.created_at))}. Nothing on record settles this, so a person decides.</p>`);
    if (d.context) parts.push(`<div class="build-why"><span class="ask-label">What the agent found</span><p>${esc(buildClip(d.context, 320))}</p></div>`);
  }
  const sources = buildSources(d);
  if (sources.length) parts.push(`<div class="build-from"><span>Raven also read</span>${sources.map(x => `<span class="build-src ${x.kind}">${esc(x.label)}</span>`).join('')}</div>`);
  const actions = [`<a class="button small text" href="${buildHref(d)}">Open the full record</a>`];
  if (j.done && buildCanAct(d)) actions.unshift(`<button type="button" class="button small" data-build="correct" data-id="${esc(d.id)}">That’s wrong, or doesn’t apply here</button>`);
  if (!j.done && buildCanAct(d)) actions.unshift(`<a class="button small primary" href="${buildHref(d)}">Answer it</a>`);
  parts.push(`<div class="build-actions">${actions.join('')}</div>`);
  if (buildForm.has(d.id)) parts.push(buildCorrectForm(d, j));
  return `<div class="build-detail">${parts.join('')}</div>`;
}

function buildPrecedent(j, label) {
  const p = j.prior;
  if (!p) return j.priorBy ? `<div class="build-prior"><span class="ask-label">${esc(label)}</span><div class="build-prior-head">${avatar(j.priorBy)}<b>${esc(j.priorBy)}</b><span>${esc(buildWhen(j.priorWhen))}</span></div></div>` : '';
  const via = buildVia[p.answered_via] || '';
  return `<div class="build-prior"><span class="ask-label">${esc(label)}</span>
    <div class="build-prior-head">${avatar(j.priorBy)}<b>${esc(j.priorBy)}</b><span>decided ${esc(buildWhen(j.priorWhen))}${via ? ' ' + esc(via) : ''}${p.run_title ? ' · ' + esc(buildClip(p.run_title, 48)) : ''}</span><a href="${buildHref(p)}" aria-label="Open that call">↗</a></div>
    <p class="build-prior-q">${esc(p.question)}</p>
    <p class="build-prior-a">“${esc(p.answer || '')}”</p></div>`;
}

function buildCorrectForm(d, j) {
  const hasPrior = !!(d.source_id || d.supersedes) && j.kind !== 'corrected';
  return `<form class="build-correct" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">
    <label>The right answer<textarea name="answer" required maxlength="12000" rows="3">${esc(d.answer || '')}</textarea></label>
    <label>Why the call Raven made doesn’t hold here<textarea name="why" required maxlength="12000" rows="2" placeholder="One or two sentences. This becomes the record under your name."></textarea></label>
    ${hasPrior ? `<div class="build-scope"><label><input type="radio" name="scope" value="everywhere" checked> The earlier call was wrong: replace it, so the next similar question uses this answer</label>
    <label><input type="radio" name="scope" value="here"> It doesn’t apply to this case: keep the earlier call for other cases</label></div>` : '<input type="hidden" name="scope" value="here">'}
    <p class="ask-error" hidden></p>
    <div class="build-actions"><button class="button small primary" type="submit">Record under my name</button><button class="button small text" type="button" data-build="cancel" data-id="${esc(d.id)}">Cancel</button></div>
  </form>`;
}

function buildTile(d, j) {
  const open = buildOpen.has(d.id);
  return `<li class="build-tile ${j.kind} ${open ? 'open' : ''}" data-id="${esc(d.id)}">
    <button type="button" class="build-tile-head" data-build="toggle" data-id="${esc(d.id)}" aria-expanded="${open}">
      <span class="build-q">${esc(d.question)}</span>
      <span class="build-line">${buildLine(d, j)}</span>
    </button>
    ${open ? buildDetail(d, j) : ''}
  </li>`;
}

function buildCard(run) {
  const {ds, js, settled, answered, asked, checking, reached, spared} = taskStory(run);
  const waiting = asked.map(d => js.get(d.id).owner).filter(Boolean);
  const waitingOn = [...new Set(waiting)];
  const pill = asked.length
    ? `<span class="build-pill waiting">Waiting on ${esc(waitingOn.map(askFirst).join(', ') || 'a person')}</span>`
    : checking.length
      ? `<span class="build-pill building">Building · Raven is still reading ${plural(checking.length, 'call')}</span>`
      : `<span class="build-pill building">Building · nothing waiting on anyone</span>`;
  const done = settled.length + answered.length;
  const order = d => ({review: 0, waiting: 1, checking: 2, corrected: 3, answered: 4, settled: 5})[js.get(d.id).kind];
  const rows = [...ds].sort((a, b) => order(a) - order(b) || String(a.created_at).localeCompare(String(b.created_at)));
  return `<article class="build-card">
    <div class="build-top">${pill}<span class="ask-agent">${esc(run.agent || 'Coding agent')}${run.requester ? ' · for ' + esc(run.requester) : ''}</span><span class="ask-when">${esc(ago(run.updated_at))}</span></div>
    <h2><a href="#runs/${encodeURIComponent(run.id)}">${esc(run.title)}</a></h2>
    ${run.goal ? `<p class="build-goal">${esc(buildClip(run.goal, 220))}</p>` : ''}
    <div class="build-counts">
      <div><b>${ds.length}</b><span>judgment calls the agent hit</span></div>
      <div class="past"><b>${settled.length}</b><span>settled by Raven from earlier calls, nobody asked</span></div>
      <div class="person"><b>${answered.length + asked.length}</b><span>reached a person${reached.length ? ': ' + esc(reached.map(askFirst).join(', ')) : ''}</span></div>
    </div>
    <div class="build-bar" role="img" aria-label="${done} of ${ds.length} settled">${settled.map(() => '<i class="past"></i>').join('')}${answered.map(() => '<i class="person"></i>').join('')}${asked.map(() => '<i class="open"></i>').join('')}${checking.map(() => '<i class="checking"></i>').join('')}<em>${done} of ${ds.length} settled${checking.length ? ` · ${checking.length} still being read` : ''}</em></div>
    ${spared.length ? `<p class="build-spared">Not asked: ${spared.map(x => `<span>${avatar(x.name)}${esc(askFirst(x.name))}</span> (${esc(askFirst(x.name))}’s ${x.when ? esc(x.when) + ' ' : ''}call already covered it)`).join(', ')}.</p>` : ''}
    <ul class="build-tiles">${rows.map(d => buildTile(d, js.get(d.id))).join('')}</ul>
  </article>`;
}

// The top of the page: where the team's attention is going right now, in
// people, and what Raven kept off their plates.
function buildAttention(stories) {
  const waiting = new Map();
  for (const s of stories) for (const d of s.asked) {
    const who = s.js.get(d.id).owner; if (!who) continue;
    const w = waiting.get(who) || {name: who, count: 0, since: d.created_at};
    w.count += 1; if (String(d.created_at) < String(w.since)) w.since = d.created_at;
    waiting.set(who, w);
  }
  const people = [...waiting.values()].sort((a, b) => String(a.since).localeCompare(String(b.since)));
  const came = stories.reduce((n, s) => n + s.ds.length, 0);
  const settled = stories.reduce((n, s) => n + s.settled.length, 0);
  const answered = stories.reduce((n, s) => n + s.answered.length, 0);
  const spared = [...new Set(stories.flatMap(s => s.spared.map(x => x.name)))];
  const unrouted = stories.reduce((n, s) => n + s.asked.filter(d => !s.js.get(d.id).owner).length, 0);
  return `<div class="build-attn">
    <div class="build-attn-people">
      <span class="ask-label">Where the team’s attention is going</span>
      <p class="build-attn-lead">${people.length ? `<b>${plural(people.reduce((n, p) => n + p.count, 0), 'call')}</b> waiting on <b>${plural(people.length, 'person', 'people')}</b>, out of ${came} the agents hit.` : `<b>Nobody is being asked anything.</b> ${came} calls came in, all settled.`}</p>
      ${people.length ? `<ul class="build-attn-list">${people.map(p => `<li>${avatar(p.name)}<b>${esc(askFirst(p.name))}</b><span>${p.count === 1 ? 'one call' : plural(p.count, 'call')} · waiting ${esc(ago(p.since)).replace(/ ago$/, '')}</span></li>`).join('')}${unrouted ? `<li><span class="avatar empty" aria-hidden="true"></span><b>Unassigned</b><span>${plural(unrouted, 'call')} · Raven is finding who owns it</span></li>` : ''}</ul>` : ''}
    </div>
    <div class="build-attn-kept">
      <span class="ask-label">Kept off people’s plates</span>
      <p class="build-attn-lead"><b>${settled}</b> settled by Raven from earlier calls${spared.length ? ` · ${esc(spared.map(askFirst).join(' and '))} ${spared.length === 1 ? 'was' : 'were'} not asked` : ''}</p>
      <p class="build-attn-sub"><b>${answered}</b> answered by the person who owns them · <b>${came - settled - answered}</b> still open</p>
    </div>
  </div>`;
}

function renderTasks() {
  $('.page-head').hidden = true;
  const live = askBuilding();
  const me = (state.me?.name || '').toLowerCase();
  const mine = me ? live.filter(r => String(r.requester || '').toLowerCase() === me) : [];
  const rest = live.filter(r => !mine.includes(r));
  const replaced = run => (state.decisions || []).filter(d => d.run_id === run.id && d.superseded_by).length;
  const done = (state.runs || []).filter(r => r.status === 'completed' && (taskStory(r).ds.length || replaced(r))).slice(0, 5);
  const key = JSON.stringify(['tasks', live.map(r => [r.id, r.updated_at]), done.map(r => r.id), (state.decisions || []).map(d => [d.id, d.status, d.updated_at]), me, [...buildOpen], [...buildForm]]);
  if (key === askKey && $('#app .build')) return;
  askKey = key;
  const all = live.map(taskStory);
  $('#app').innerHTML = `<div class="build">
    <header class="build-head"><h1>Being built</h1><p>Every judgment call a coding agent hit while building, and what Raven did with it: settled from a call someone already made, or sent to the one person who has to decide. Click a call to see where its answer came from.</p></header>
    ${live.length ? buildAttention(all) : ''}
    ${mine.length ? `<h2 class="ask-label">Yours</h2>${mine.map(buildCard).join('')}` : ''}
    ${rest.length ? `${mine.length ? '<h2 class="ask-label">Across the team</h2>' : ''}${rest.map(buildCard).join('')}` : ''}
    ${live.length ? '' : '<p class="ask-muted">Nothing is being built right now. When Claude Code, Codex or Cursor starts a task, it shows up here.</p>'}
    ${done.length ? `<h2 class="ask-label">Recently finished</h2><div class="build-done">${done.map(r => `<a href="#runs/${encodeURIComponent(r.id)}"><span>${esc(r.title)}</span><small>${plural(taskStory(r).ds.length + replaced(r), 'decision')}${replaced(r) ? ` · ${replaced(r) === 1 ? 'one' : replaced(r)} since replaced by ${esc([...new Set((state.decisions || []).filter(d => d.run_id === r.id && d.superseded_by).map(d => askFirst(buildById(d.superseded_by)?.answered_by || '')).filter(Boolean))].join(', ') || 'a newer call')}` : ''} · ${esc(askDate(r.updated_at))}</small></a>`).join('')}</div>` : ''}
    ${live.length ? '' : askAgentUses()}
  </div>`;
}
window.addEventListener('hashchange', () => { if (location.hash.startsWith('#tasks')) askKey = ''; });

document.addEventListener('click', event => {
  const button = event.target.closest('[data-build]');
  if (!button || view !== 'tasks') return;
  const {build, id} = button.dataset;
  if (build === 'toggle') { if (buildOpen.has(id)) { buildOpen.delete(id); buildForm.delete(id); } else buildOpen.add(id); }
  else if (build === 'correct') buildForm.add(id);
  else if (build === 'cancel') buildForm.delete(id);
  askKey = ''; render();
  if (build === 'correct') $(`.build-correct[data-id="${CSS.escape(id)}"] textarea[name=why]`)?.focus();
});
document.addEventListener('submit', async event => {
  const form = event.target.closest('.build-correct');
  if (!form) return;
  event.preventDefault();
  const d = buildById(form.dataset.id);
  const data = Object.fromEntries(new FormData(form).entries());
  const error = form.querySelector('.ask-error');
  const button = form.querySelector('button[type=submit]');
  button.disabled = true; button.textContent = 'Recording…';
  try {
    const result = await api(`/api/decisions/${encodeURIComponent(form.dataset.id)}/correct`, {
      answer: data.answer, why: data.why, scope: data.scope || 'here', expected_updated_at: form.dataset.updated,
      ...(d && state.me?.role === 'admin' && state.me.name !== d.owner_name ? {override: true} : {})});
    buildForm.delete(form.dataset.id);
    notify(result.notice || 'Recorded.');
    askKey = '';
    await refresh();
  } catch (err) {
    error.textContent = err.message; error.hidden = false;
    button.disabled = false; button.textContent = 'Record under my name';
  }
});

// Suggested first questions, short enough to read at a glance: two things
// the team decided (asked the way a person would), who owns an area, and
// one question no record answers, which Raven still answers.
function askSuggestions() {
  const out = [];
  const short = q => { let s = String(q).split(/[:—]|\s-\s/)[0].trim().replace(/[?.]*$/, ''); if (s.length > 64) s = s.slice(0, 62).replace(/\s+\S*$/, '') + '…'; return s.endsWith('…') ? s : s + '?'; };
  const decided = (state.decisions || []).filter(d => d.status === 'approved' && d.question && !d.run_id?.startsWith('x')).slice(0, 2);
  for (const d of decided) out.push(short(d.question));
  const area = (directory?.authority || []).find(a => a.scope_kind === 'path' && a.role === 'decides');
  if (area) out.push(`Who should I talk to before changing ${area.scope.replace(/\/?\*+$/, '')}?`);
  out.push('What should I work on next?');
  if (out.length < 4) out.push('What did we decide last month, and who decided it?');
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
  // A fenced code block (asked for code) is shown as code, after the words.
  const code = [];
  text = String(text || '').replace(/```[\w+-]*\n?([\s\S]*?)(?:```|$)/g, (m, body) => { if (body.trim()) code.push(body.replace(/\s+$/, '')); return '\n'; });
  const blocks = code.map(c => `<pre class="ask-code"><code>${esc(c)}</code></pre>`).join('');
  const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
  const point = l => /^([-•*]|\d+[.)])\s+/.test(l);
  const points = lines.filter(point).map(l => l.replace(/^([-•*]|\d+[.)])\s+/, ''));
  const lead = lines.filter(l => !point(l)).join(' ');
  return `${lead ? `<p class="ask-lead">${askMentions(esc(lead), people)}</p>` : ''}${points.length ? `<ul class="ask-points">${points.map(p => `<li>${askMentions(esc(p), people)}</li>`).join('')}</ul>` : ''}${blocks}`;
}

// A record's line in an answer: who, how they said it, when. The
// question a decision answered is left out: it is close to what was just
// asked, and repeated on every quote it made the answer twice as long.
const askSaid = e => e.author || (e.text.match(/Recorded by: ([^(\n]+)/) || [])[1]?.trim() || '';
const askHow = e => e.kind === 'prior_decision' ? (String(e.ref || '').startsWith('call:') ? 'decided on a call' : 'decided')
  : e.kind === 'person_note' ? ({'intro chat': 'said in a voice intro', 'from a meeting': 'said in a meeting', 'via an agent': 'told an agent', 'from a call': 'said on a call'}[e.via] || 'told Raven')
  : e.kind === 'profile' ? 'profile on Raven' : 'wrote';
// Lines that only say who signed or recorded a decision repeat its byline.
const askBylineOnly = quote => /^(Signed by|Recorded by):/i.test(String(quote).trim());
const askClip = (text, n) => { text = String(text || ''); return text.length > n ? text.slice(0, n - 1).replace(/\s+\S*$/, '') + '…' : text; };

// Where the answer came from: one line per record, newest wording first,
// each opening the record itself (the call, the meeting, the pull request).
function askSources(r, heading = 'Where this comes from') {
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
  const shown = [...groups.entries()].slice(0, 3);
  const rows = shown.map(([id, quotes]) => {
    const e = byId[id], who = askSaid(e);
    const open = e.ref ? `data-source="${esc(e.ref)}" data-quote="${esc(quotes[0] || '')}"` : e.person_id ? `data-href="#people/${esc(e.person_id)}"` : e.url ? `data-href="${esc(e.url)}"` : '';
    const where = e.kind === 'person_note' ? (e.source_title || '') : e.kind === 'source' ? e.title : '';
    const when = askDate(e.updated_at || e.created_at || e.version);
    let quote = quotes[0] || (e.kind === 'prior_decision' ? e.text.split('\n')[0] : '');
    if (quote && quote.trim() === String(e.title || '').trim()) quote = '';
    return `<div class="ask-src ask-quote" ${open} ${open ? 'tabindex="0" role="button" aria-label="Open the source"' : ''} title="${esc(e.title || '')}">
      <div class="ask-src-head">${who ? `${avatar(who)}<b>${esc(who)}</b>` : ''}<span>${esc(askHow(e))}${where ? ' · ' + esc(askClip(where, 56)) : ''}${when ? ' · ' + esc(when) : ''}</span>${open ? '<span class="ask-ref" aria-hidden="true">Open ↗</span>' : ''}</div>
      ${quote ? `<p>“${esc(askClip(quote, 220))}”</p>` : ''}</div>`;
  }).join('');
  // What Raven knows about the team: a recent decision (who made it), a
  // task being built, something Raven noticed.
  const refs = (r.workspace_refs || []).slice(0, Math.max(0, 3 - shown.length)).map(x => {
    const decided = x.kind === 'decided' && x.who;
    const open = decided ? `data-source="decision:${esc(x.id.slice(8))}" data-quote=""` : x.task_id ? `data-href="#runs/${esc(x.task_id)}"` : '';
    const head = decided ? `${avatar(x.who)}<b>${esc(x.who)}</b><span>decided${x.at ? ' · ' + esc(askDate(x.at)) : ''}</span>`
      : `<span>${x.kind === 'task' ? 'Being built · ' : ''}${esc(askClip(x.title, 60))}</span>`;
    return `<div class="ask-src ask-quote" ${open} ${open ? 'tabindex="0" role="button" aria-label="Open the source"' : ''}>
      <div class="ask-src-head">${head}${open ? '<span class="ask-ref" aria-hidden="true">Open ↗</span>' : ''}</div><p>“${esc(askClip(x.quote.replace(/^.*? decided, asked ".*?": /, ''), 200))}”</p></div>`;
  }).join('');
  return rows + refs ? `<div class="ask-sources"><span class="ask-sub">${esc(heading)}</span>${rows}${refs}</div>` : '';
}

// What the answer rests on, said in one line above it: the team's own
// records, Raven's read of the workspace, or general knowledge.
function askBasis(r) {
  if (r.retry) return ['warn', 'Raven could not finish this answer'];
  if (r.grounding === 'general') return ['general', 'General knowledge · nobody on the team has recorded this'];
  if (r.grounding === 'mixed') return ['general', 'General knowledge plus what your team has said'];
  if (r.grounding === 'workspace') return ['read', 'Raven’s read of the team · nobody has written this down'];
  if (r.grounding === 'records') return ['team', 'From what your team decided and said'];
  if (r.outcome === 'conflict') return ['warn', 'Your team’s records disagree'];
  if (r.model_used) return ['team', 'From what your team decided and said'];
  return ['team', 'What your team has said'];
}

function askAnswer(entry) {
  if (entry.status === 'thinking') return `<div class="ask-raven"><span class="ask-orb thinking" aria-hidden="true"></span><div class="ask-body ask-wait" role="status">
    <p><span id="ask-wait-text">${entry.slow ? 'Still working: checking each point against who said it…' : 'Reading what your team decided and said…'}</span> <span class="ask-secs" id="ask-wait-secs"></span></p>
    <p class="ask-muted">Usually 10 to 30 seconds. The answer cites the people and records it comes from.</p></div></div>`;
  if (entry.status === 'error') return `<div class="ask-raven"><span class="ask-orb" aria-hidden="true"></span><div class="ask-body"><p class="ask-error" role="alert">${esc(entry.error)}</p><button type="button" class="button small" data-ask-retry="${esc(entry.q)}">Ask again</button></div></div>`;
  const r = entry.result || {};
  const evidence = r.evidence || [];
  // Only people whose words are shown as sources; everyone else stays a "talk to".
  const byId = Object.fromEntries(evidence.map(e => [e.id, e]));
  const shownIds = r.model_used ? (r.citations || []).map(c => c.id) : evidence.filter(e => e.kind !== 'profile').slice(0, 3).map(e => e.id);
  const sourced = new Set(shownIds.map(id => byId[id] && askSaid(byId[id])).filter(Boolean).map(n => n.toLowerCase()));
  const mentionable = [...(r.people || []), ...evidence.filter(e => e.person_id).map(e => ({name: askSaid(e), id: e.person_id}))];
  const profile = evidence.find(e => e.kind === 'profile');
  let lead;
  if (r.model_used || r.grounding || r.retry) lead = askProse(r.answer, mentionable);
  else if (profile) { const [first, ...rest] = profile.text.split('\n'); lead = askProse([first, ...rest.slice(0, 3).map(l => '- ' + l)].join('\n'), mentionable); }
  else lead = `<p class="ask-lead">${evidence.length ? 'Here is what your team has already said about this.'
    : (r.people || []).length ? `Nobody has written this down yet. ${askMentions(esc(r.people.slice(0, 2).map(p => p.name).join(' and ')), mentionable)} ${r.people.length > 1 ? 'are' : 'is'} who to ask.` : 'Nobody has written this down yet.'}</p>`;
  const [tone, basis] = askBasis(r);
  // People the sources already name are not listed again.
  for (const x of r.workspace_refs || []) if (x.who) sourced.add(x.who.toLowerCase());
  const me = String(state.me?.name || '').toLowerCase();
  const others = (r.people || []).filter(p => !sourced.has(String(p.name).toLowerCase()) && String(p.name).toLowerCase() !== me);
  const people = others.length ? `<div class="ask-who"><span class="ask-sub">Talk to</span>${others.slice(0, 3).map(p => `<a class="ask-person-chip" href="#people/${encodeURIComponent(p.id || '')}" title="${esc(p.why)}">${avatar(p.name)}${esc(p.name)}${/^(Decides|Knows about) /.test(p.why || '') ? `<small>${esc(askClip(p.why.replace(/^(Decides|Knows about) /, (m, v) => v.toLowerCase() + ' ').replace(/:.*$/, ''), 30))}</small>` : ''}</a>`).join('')}</div>` : '';
  const open = r.model_used && !r.grounding && (r.open || []).length ? `<p class="ask-muted ask-open">Not on record yet: ${esc(askClip(r.open[0], 160))}</p>` : '';
  const retry = r.retry ? `<button type="button" class="button small" data-ask-retry="${esc(entry.q)}">Ask again</button>` : '';
  return `<div class="ask-raven"><span class="ask-orb" aria-hidden="true"></span><div class="ask-body"><span class="ask-basis ${tone}">${esc(basis)}</span>${lead}${askSources(r, ['general', 'mixed'].includes(r.grounding) ? 'Related from your team' : 'Where this comes from')}${people}${open}${retry}</div></div>`;
}

function askThreadView() {
  return askThread.map((entry, i) => `<div class="ask-turn"><div class="ask-you"><p>${esc(entry.q)}</p></div>${askAnswer(entry, i)}</div>`).join('');
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

// The same items For you shows as "Needs you": calls this person owns or
// must sign, from /api/attention. Nothing shows until that has loaded.
function askWaiting() {
  if (!state.auth?.enabled || typeof attention === 'undefined' || !attention?.needs_you?.length) return '';
  const mine = attention.needs_you;
  return `<a class="ask-waiting" href="#you"><span class="ask-orb small" aria-hidden="true"></span><span><b>Raven needs you for ${plural(mine.length, 'thing')}.</b><small>${esc(mine[0].question)}</small></span><span aria-hidden="true">→</span></a>`;
}

// Raven noticed: what the background pass found when it read what people
// said next to the code. A short list of titles beside the chat; open one
// for who said what and one next step: ask the right person, or hand it
// to Claude.
let noticed = null, noticedAt = 0, noticedBusy = '';
const noticedOpen = new Set();
function loadNoticed(force) {
  if (!force && Date.now() - noticedAt < 8000) return;
  noticedAt = Date.now();
  api('/api/muse').then(r => { const before = JSON.stringify(noticed); noticed = r; if (JSON.stringify(r) !== before) { askKey = ''; if (view === 'ask') render(); } })
    .catch(() => { noticed = noticed || {insights: []}; });
}
function noticedCard(x) {
  const cites = x.evidence.slice(0, 2).map(e => `<p class="noticed-cite">“${esc(askClip(e.quote, 150))}” <small>${e.person_id ? `<a href="#people/${esc(e.person_id)}">${esc(e.who)}</a>` : esc(e.who)}, ${esc(e.kind === 'decided' ? 'answering an agent' : (e.where === 'intro chat' ? 'voice intro' : e.where))} · ${esc(askDate(e.at))}</small></p>`).join('');
  const who = x.person ? askFirst(x.person.name) : '';
  return `<details class="noticed-card" data-kind="${esc(x.kind)}" data-noticed-id="${esc(x.id)}" ${noticedOpen.has(x.id) ? 'open' : ''}>
    <summary><span class="noticed-kind">${esc(x.kind_label)}</span><h3>${esc(x.title)}</h3></summary>
    <div class="noticed-more"><p>${esc(x.body)}</p>${cites}
    ${x.paths.length ? `<p class="noticed-paths">Raven read ${x.paths.slice(0, 3).map(p => `<code>${esc(p)}</code>`).join(' ')}</p>` : ''}
    <div class="noticed-actions">${x.task_id ? `<a class="button small" href="#runs/${encodeURIComponent(x.task_id)}">Asked${who ? ' · see ' + esc(who) + '’s answer' : ''}</a>`
      : `<button type="button" class="button small primary" data-noticed="ask" data-id="${esc(x.id)}" ${noticedBusy ? 'disabled' : ''}>${who ? 'Ask ' + esc(who) : 'Ask the right person'}</button>`}
      ${x.agent_prompt ? `<button type="button" class="button small" data-noticed="claude" data-id="${esc(x.id)}">Hand to Claude</button>` : ''}
      <button type="button" class="button small text" data-noticed="dismiss" data-id="${esc(x.id)}">Dismiss</button></div></div></details>`;
}
function noticedView() {
  const items = (noticed?.insights || []).slice(0, 4);
  const last = noticed?.last_pass;
  const looking = noticedBusy === 'run' || noticed?.running;
  return `<section class="noticed"><div class="noticed-head"><span class="ask-orb small ${looking ? 'thinking' : ''}" aria-hidden="true"></span><h2 class="ask-label">Raven noticed</h2>
    <button type="button" class="button small text" data-noticed="run" ${looking ? 'disabled' : ''}>${looking ? 'Looking…' : 'Look again'}</button></div>
    <p class="noticed-last">${looking ? 'Reading what people said next to the code…' : last ? `Things worth a look, from ${plural(last.said, 'note')}, ${plural(last.decided, 'decision')} and ${plural(last.files, 'file')} · ${esc(ago(last.at))}` : 'Things worth a look, from what people said next to the code.'}</p>
    ${items.length ? items.map(noticedCard).join('') : `<p class="ask-muted">${looking ? '' : 'When people tell Raven something the product doesn’t do yet, it shows up here.'}</p>`}</section>`;
}
document.addEventListener('toggle', event => {
  const card = event.target.closest?.('details[data-noticed-id]');
  if (!card) return;
  if (card.open) noticedOpen.add(card.dataset.noticedId); else noticedOpen.delete(card.dataset.noticedId);
}, true);

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
        ${empty ? `<div class="ask-hero"><span class="ask-orb big" aria-hidden="true"></span><h1>Ask Raven</h1><p>Ask anything about the work. Raven answers from what ${name ? esc(name) : 'your team'} has decided and said, shows who said it, and tells you who to talk to.</p></div>` : ''}
        ${askWaiting()}
        ${askThreadView()}
      </div>
      <div class="ask-dock">
        <form class="ask-composer" id="ask-form"><label class="sr-only" for="ask-input">Ask Raven</label><input id="ask-input" autocomplete="off" maxlength="2000" placeholder="${askBusy ? 'Raven is answering…' : empty ? 'Ask why, who knows, or what to do next' : 'Ask a follow-up'}" ${askBusy ? 'disabled' : ''}><button class="button primary" type="submit" ${askBusy ? 'disabled' : ''}>Ask</button></form>
        ${empty ? `<div class="ask-chips"><span class="ask-sub">Try</span>${askSuggestions().map(s => `<button type="button" class="ask-chip" data-ask="${esc(s)}">${esc(s)}</button>`).join('')}</div>` : ''}
      </div>
    </div>
    <aside class="ask-side">
      ${noticedView()}
      ${building.length ? `<a class="ask-building" href="#tasks"><span class="ask-live on">Live</span><span>${plural(building.length, 'task')} being built</span><span aria-hidden="true">→</span></a>` : ''}
    </aside>
  </div>`;
  const input = $('#ask-input');
  if (input) { input.value = typed; if (hadFocus || (!askBusy && !empty)) input.focus({preventScroll: true}); }
  askTick();
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
let askStarted = 0, askTimer = null;
// The wait, counted in seconds where the answer will appear.
function askTick() {
  const secs = $('#ask-wait-secs');
  if (secs && askStarted) secs.textContent = Math.max(1, Math.round((Date.now() - askStarted) / 1000)) + 's';
}
async function askRaven(question) {
  question = question.trim();
  if (!question || askBusy) return;
  const entry = {q: question, status: 'thinking'};
  askThread.push(entry); askBusy = true; askStick = true; askKey = ''; askStarted = Date.now(); render();
  clearInterval(askTimer); askTimer = setInterval(askTick, 1000);
  const started = askStarted;
  try {
    const job = await api('/api/ask', {question, repository: askRepo(), background: true});
    let misses = 0;
    for (;;) {
      if (job.status === 'done' || job.status === 'error') break;
      // A reading that never comes back is reported, not waited on forever.
      if (Date.now() - started > 240000) throw new Error('Raven took too long to answer this one. Ask again; a shorter question is usually faster.');
      await askPause(Date.now() - started < 10000 ? 900 : 2000);
      try { Object.assign(job, await api('/api/ask/' + encodeURIComponent(job.job))); misses = 0; }
      catch (error) {
        // A dropped poll is retried; the reading is still going on the server.
        if (/gone/i.test(error.message) || ++misses > 5) throw error;
      }
      if (!entry.slow && Date.now() - started > 15000) { entry.slow = true; askKey = ''; render(); }
    }
    if (job.status === 'error') throw new Error(job.error || 'Raven could not finish reading this. Ask again.');
    entry.result = job.result;
    entry.status = 'done';
  } catch (error) {
    entry.status = 'error';
    entry.error = /failed to fetch|networkerror|load failed/i.test(error.message)
      ? 'Raven lost the connection to the server. Check that it is still running, then ask again.' : error.message;
  } finally {
    delete entry.slow;
    clearInterval(askTimer); askTimer = null; askStarted = 0;
    askBusy = false; askStick = true; askSave(); askKey = ''; render();
  }
}
document.addEventListener('click', event => {
  const retry = event.target.closest('[data-ask-retry]');
  if (!retry || view !== 'ask') return;
  const q = retry.dataset.askRetry;
  askThread = askThread.filter(e => !(e.q === q && (e.status === 'error' || e.result?.retry)));
  askRaven(q);
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
