'use strict';

// The map (#map, #map/<person id>): who holds context across the company.
// People and the areas they decide or know about, drawn as one graph from
// the authority map, git history and what people said in their intros.
// Selecting a person or an area shows who they are in a card, not a list.
let mapData = null, mapLoading = false, mapKey = '', mapSelected = '', mapHover = '';
const mapPersonId = () => { try { return view === 'map' ? decodeURIComponent(location.hash.slice(1).split('/')[1] || '') : ''; } catch { return ''; } };
const mapTitle = text => text.charAt(0).toUpperCase() + text.slice(1);

// "app/labels/*" -> "Labels"; "pricing" -> "Pricing". The path stays in the card.
function mapAreaName(scope) {
  const parts = String(scope || '').replace(/\/?\*+$/, '').split('/').filter(Boolean);
  const last = parts[parts.length - 1] || scope || 'Everything';
  const name = last.replace(/[_-]+/g, ' ');
  return name.length <= 3 ? name.toUpperCase() : mapTitle(name);
}

async function loadMap() {
  if (mapLoading) return;
  mapLoading = true;
  try {
    const [people, owned, intro] = await Promise.all([api('/api/people'), api('/api/ownership').catch(() => ({ownership: []})),
      api('/api/intro').catch(() => ({people: []}))]);
    const notes = {};
    await Promise.all((intro.people || []).filter(p => p.notes).map(p => api('/api/people/' + encodeURIComponent(p.id) + '/context')
      .then(r => { notes[p.id] = r.context || []; }).catch(() => {})));
    mapData = buildMap(people, owned.ownership || [], intro.people || [], notes);
  } catch (error) {
    mapData = {error: error.message};
  } finally { mapLoading = false; mapKey = ''; render(); }
}

function buildMap(dir, ownership, introPeople, notes) {
  const people = (dir.people || []).filter(p => p.active !== 0);
  const byLogin = {}, byName = {};
  for (const p of people) { byName[p.name.toLowerCase()] = p; if (p.github_login) byLogin[p.github_login.toLowerCase()] = p; }
  const find = name => byName[String(name || '').toLowerCase()] || byLogin[String(name || '').toLowerCase().replace(/^@/, '')];
  const nodes = new Map(), links = [];
  const intro = Object.fromEntries(introPeople.map(p => [p.id, p]));
  for (const p of people) nodes.set('p:' + p.id, {id: 'p:' + p.id, kind: 'person', person: p, label: p.name, intro: intro[p.id], notes: notes[p.id] || []});
  const area = (key, label, detail) => {
    if (!nodes.has(key)) nodes.set(key, {id: key, kind: 'area', label, detail, scopes: new Set()});
    return nodes.get(key);
  };
  for (const a of dir.authority || []) {
    if (a.ended_at || !a.person_id || !nodes.has('p:' + a.person_id)) continue;
    const label = mapAreaName(a.scope);
    const node = area('a:' + label.toLowerCase(), label, a.note);
    node.scopes.add(a.scope);
    if (a.note && !node.detail) node.detail = a.note;
    links.push({source: 'p:' + a.person_id, target: node.id, kind: a.role === 'decides' ? 'decides' : 'knows', note: a.note, scope: a.scope});
  }
  // Git history: who has actually been changing an area lately. A path no
  // one decides becomes its own area only when someone clearly leads it.
  const seen = new Set();
  for (const o of ownership.filter(o => ['blame', 'blame_recent'].includes(o.source) && o.weight >= 0.34 && o.path_prefix)) {
    const person = find(o.engineer);
    if (!person) continue;
    const label = mapAreaName(o.path_prefix);
    const key = 'a:' + label.toLowerCase();
    if (!nodes.has(key) && o.path_prefix.split('/').filter(Boolean).length < 2) continue;
    const node = area(key, label, '');
    node.scopes.add(o.path_prefix);
    const id = person.id + key;
    if (seen.has(id) || links.some(l => l.source === 'p:' + person.id && l.target === key)) continue;
    seen.add(id);
    links.push({source: 'p:' + person.id, target: key, kind: 'git', note: o.evidence, scope: o.path_prefix});
  }
  for (const t of dir.teams || []) {
    for (const member of t.members || []) {
      const n = nodes.get('p:' + member);
      if (n) (n.teams = n.teams || []).push(t.name);
    }
  }
  const list = [...nodes.values()].filter(n => n.kind === 'person' || links.some(l => l.target === n.id));
  layoutMap(list, links);
  return {nodes: list, links, byId: Object.fromEntries(list.map(n => [n.id, n]))};
}

// A small force layout, run to rest once. Seeded from a circle so the same
// workspace draws the same map every time.
function layoutMap(nodes, links) {
  const W = 1000, H = 640, byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const areas = nodes.filter(n => n.kind === 'area'), people = nodes.filter(n => n.kind === 'person');
  areas.forEach((n, i) => { const a = (i / Math.max(1, areas.length)) * Math.PI * 2; n.x = W / 2 + Math.cos(a) * 170; n.y = H / 2 + Math.sin(a) * 130; });
  people.forEach((n, i) => { const a = (i / Math.max(1, people.length)) * Math.PI * 2 + 0.4; n.x = W / 2 + Math.cos(a) * 360; n.y = H / 2 + Math.sin(a) * 250; });
  const edges = links.map(l => [byId[l.source], byId[l.target], l.kind === 'git' ? 190 : 150]).filter(([a, b]) => a && b);
  for (let step = 0; step < 400; step++) {
    const heat = 1 - step / 400;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy || 1;
        const f = 30000 / d2 * heat;
        const d = Math.sqrt(d2);
        dx /= d; dy /= d;
        a.x += dx * f; a.y += dy * f; b.x -= dx * f; b.y -= dy * f;
      }
    }
    for (const [a, b, len] of edges) {
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - len) * 0.02 * heat;
      a.x += dx / d * f; a.y += dy / d * f; b.x -= dx / d * f; b.y -= dy / d * f;
    }
    for (const n of nodes) {
      n.x += (W / 2 - n.x) * 0.004; n.y += (H / 2 - n.y) * 0.006;
    }
  }
  // Fill the canvas: scale the settled layout into it, keeping its shape.
  const xs = nodes.map(n => n.x), ys = nodes.map(n => n.y);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const scale = Math.min((W - 160) / Math.max(1, x1 - x0), (H - 130) / Math.max(1, y1 - y0), 2.2);
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  for (const n of nodes) { n.x = W / 2 + (n.x - cx) * scale; n.y = H / 2 - 10 + (n.y - cy) * scale; }
}

function mapNeighbours(id) {
  const out = new Set([id]);
  for (const l of mapData.links) { if (l.source === id) out.add(l.target); if (l.target === id) out.add(l.source); }
  return out;
}

function mapSvg() {
  const focus = mapHover || mapSelected;
  const near = focus ? mapNeighbours(focus) : null;
  const dim = id => near && !near.has(id) ? ' dim' : '';
  const lines = mapData.links.map(l => {
    const a = mapData.byId[l.source], b = mapData.byId[l.target];
    if (!a || !b) return '';
    const on = near && near.has(l.source) && near.has(l.target) && (l.source === focus || l.target === focus);
    return `<line class="map-link ${l.kind}${on ? ' on' : ''}${near && !on ? ' dim' : ''}" x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"/>`;
  }).join('');
  const dots = mapData.nodes.map(n => {
    const x = n.x.toFixed(1), y = n.y.toFixed(1), sel = n.id === mapSelected ? ' selected' : '';
    if (n.kind === 'area') {
      const r = 26 + Math.min(14, mapData.links.filter(l => l.target === n.id).length * 3);
      return `<g class="map-node area${sel}${dim(n.id)}" data-map="${esc(n.id)}" tabindex="0" role="button" aria-label="${esc(n.label)}"><circle cx="${x}" cy="${y}" r="${r}"/><text x="${x}" y="${(+y + 4).toFixed(1)}">${esc(n.label)}</text></g>`;
    }
    const heard = n.notes.length ? `<circle class="map-heard" cx="${(+x + 17).toFixed(1)}" cy="${(+y - 17).toFixed(1)}" r="6"><title>Raven has heard from them</title></circle>` : '';
    return `<g class="map-node person${sel}${dim(n.id)}${n.notes.length ? ' heard' : ''}" data-map="${esc(n.id)}" tabindex="0" role="button" aria-label="${esc(n.label)}"><circle cx="${x}" cy="${y}" r="22"/><text class="initials" x="${x}" y="${(+y + 4).toFixed(1)}">${esc(initials(n.label))}</text><text class="name" x="${x}" y="${(+y + 40).toFixed(1)}">${esc(n.label)}</text>${heard}</g>`;
  }).join('');
  return `<svg class="map-svg" viewBox="0 0 1000 640" role="img" aria-label="People and the areas they hold context on">${lines}${dots}</svg>`;
}

// What someone said, and Raven's note on it when the note adds anything.
function mapQuote(x) {
  const same = (a, b) => String(a || '').replace(/\W+/g, ' ').trim().toLowerCase() === String(b || '').replace(/\W+/g, ' ').trim().toLowerCase();
  return `<blockquote class="map-quote"><p>${esc(x.quote ? '“' + x.quote + '”' : x.text)}</p>${x.quote && !same(x.quote, x.text) ? `<small>${esc(x.text)}</small>` : ''}</blockquote>`;
}

function mapPersonCard(n) {
  const p = n.person;
  const mine = mapData.links.filter(l => l.source === n.id);
  const group = kind => mine.filter(l => l.kind === kind).map(l => mapData.byId[l.target]).filter(Boolean);
  const chips = (list, cls) => list.map(a => `<button type="button" class="map-chip ${cls}" data-map="${esc(a.id)}">${esc(a.label)}</button>`).join('');
  const decides = group('decides'), knows = group('knows'), git = group('git');
  const quotes = n.notes.filter(x => x.quote || x.text).slice(0, 3).map(mapQuote).join('');
  const heard = n.notes.length ? `<div class="map-section"><span class="ask-label">In their words</span>${quotes}<small class="ask-muted">From their intro with Raven${n.intro?.updated_at ? ', ' + esc(ago(n.intro.updated_at)) : ''}.</small></div>`
    : `<div class="map-section"><span class="ask-label">Not heard from yet</span><p class="ask-muted">A few minutes talking with Raven fills in what isn't written down anywhere.</p></div>`;
  const canTalk = n.intro?.can_take !== false;
  return `<div class="map-card"><div class="map-card-head">${avatar(p.name)}<div><h2>${esc(p.name)}</h2><small>${esc((n.teams || [p.team]).filter(Boolean).join(' · ') || 'No team yet')}</small></div></div>
    ${decides.length ? `<div class="map-section"><span class="ask-label">Decides</span><div>${chips(decides, 'decides')}</div></div>` : ''}
    ${knows.length ? `<div class="map-section"><span class="ask-label">Knows about</span><div>${chips(knows, 'knows')}</div></div>` : ''}
    ${git.length ? `<div class="map-section"><span class="ask-label">Has written the code in</span><div>${chips(git, 'git')}</div></div>` : ''}
    ${heard}
    <div class="map-actions">${canTalk ? `<a class="button ${n.notes.length ? '' : 'primary'} small" href="#intro/${encodeURIComponent(p.id)}">${n.notes.length ? 'Talk again' : 'Start a voice intro'}</a>` : ''}<button type="button" class="button small" data-map-ask="${esc('What does ' + p.name + ' decide, and what have they said?')}">Ask about ${esc(p.name.split(' ')[0])}</button></div></div>`;
}

function mapAreaCard(n) {
  const into = mapData.links.filter(l => l.target === n.id);
  const row = (l, verb) => { const who = mapData.byId[l.source]; return who ? `<button type="button" class="map-who" data-map="${esc(who.id)}">${avatar(who.label)}<span><b>${esc(who.label)}</b><small>${verb}${l.note && l.kind !== 'git' ? ' · ' + esc(l.note) : ''}</small></span></button>` : ''; };
  const order = {decides: 0, knows: 1, git: 2};
  const rows = into.sort((a, b) => order[a.kind] - order[b.kind]).map(l => row(l, l.kind === 'decides' ? 'Decides' : l.kind === 'knows' ? 'Knows about it' : 'Has written much of the code')).join('');
  return `<div class="map-card"><div class="map-card-head"><span class="map-area-dot" aria-hidden="true"></span><div><h2>${esc(n.label)}</h2><small>${[...n.scopes].map(s => `<code>${esc(s)}</code>`).join(' ')}</small></div></div>
    ${n.detail ? `<p class="map-detail">${esc(n.detail)}</p>` : ''}<div class="map-section"><span class="ask-label">Who to talk to</span>${rows}</div>
    <div class="map-actions"><button type="button" class="button small" data-map-ask="${esc('What has the team decided about ' + n.label.toLowerCase() + ', and who decided it?')}">Ask what was decided</button></div></div>`;
}

function mapOverview() {
  const people = mapData.nodes.filter(n => n.kind === 'person');
  const heard = people.filter(n => n.notes.length).length;
  return `<div class="map-card"><h2>Who holds context</h2><p class="map-detail">Each person is linked to the areas they decide, know about or wrote the code for. Raven routes a question along these lines, so it reaches whoever owns the call today, not whoever last touched the file.</p>
    <div class="map-legend"><span><i class="decides"></i>Decides</span><span><i class="knows"></i>Knows about</span><span><i class="git"></i>Wrote the code</span><span><i class="heard"></i>Talked with Raven</span></div>
    <p class="ask-muted">${heard} of ${plural(people.length, 'person', 'people')} have talked with Raven. Select anyone to see what they hold.</p></div>`;
}

function renderMap() {
  const wanted = mapPersonId();
  if (wanted) mapSelected = 'p:' + wanted;
  if (!mapData && !mapLoading) loadMap();
  $('.page-head').hidden = true;
  const key = JSON.stringify([!!mapData, mapData?.error, mapSelected, mapHover]);
  if (key === mapKey && $('#app .map')) return;
  mapKey = key;
  if (!mapData) { $('#app').innerHTML = '<div class="map"><p class="ask-muted" role="status">Drawing the map…</p></div>'; return; }
  if (mapData.error) { $('#app').innerHTML = `<div class="map"><p class="ask-error" role="alert">${esc(mapData.error)}</p></div>`; return; }
  const sel = mapData.byId[mapSelected];
  const card = !sel ? mapOverview() : sel.kind === 'person' ? mapPersonCard(sel) : mapAreaCard(sel);
  const add = state.me?.role === 'admin' || !state.auth?.enabled ? '<a class="button small" href="#owners">Edit people</a>' : '';
  $('#app').innerHTML = `<div class="map"><div class="map-head"><h1>Map</h1><div class="map-head-actions">${sel ? '<button type="button" class="button small text" data-map="">Show everyone</button>' : ''}${add}</div></div>
    <div class="map-body"><div class="map-canvas">${mapData.nodes.length ? mapSvg() : '<p class="ask-muted">Add people under Settings, or connect a repository, and the map fills in.</p>'}</div><aside class="map-side">${card}</aside></div></div>`;
}

document.addEventListener('click', event => {
  if (view !== 'map') return;
  const ask = event.target.closest('[data-map-ask]');
  if (ask) { event.preventDefault(); location.hash = '#ask'; setTimeout(() => typeof askRaven === 'function' && askRaven(ask.dataset.mapAsk), 0); return; }
  const node = event.target.closest('[data-map]');
  if (!node) return;
  event.preventDefault();
  mapSelected = node.dataset.map === mapSelected ? '' : node.dataset.map;
  mapHover = '';
  const person = mapSelected.startsWith('p:') ? mapSelected.slice(2) : '';
  history.replaceState(null, '', location.pathname + location.search + '#map' + (person ? '/' + encodeURIComponent(person) : ''));
  renderMap();
});
document.addEventListener('keydown', event => {
  if (view !== 'map' || !['Enter', ' '].includes(event.key)) return;
  const node = event.target.closest?.('g[data-map]');
  if (node) { event.preventDefault(); node.dispatchEvent(new MouseEvent('click', {bubbles: true})); }
});
document.addEventListener('mouseover', event => {
  if (view !== 'map') return;
  const node = event.target.closest?.('g[data-map]');
  const next = node ? node.dataset.map : '';
  if (next === mapHover) return;
  mapHover = next;
  const svg = $('#app .map-canvas');
  if (svg && mapData && !mapData.error) svg.innerHTML = mapSvg();
});
window.addEventListener('hashchange', () => { if (!location.hash.startsWith('#map')) { mapData = null; mapSelected = ''; } mapKey = ''; });
