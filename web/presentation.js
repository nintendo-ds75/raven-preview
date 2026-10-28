// A request key that also works over plain http on a LAN address, where the
// browser offers no crypto.randomUUID (not a secure context).
function ravenUuid() {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === 'function') c.getRandomValues(bytes);
  else for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40; bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

'use strict';

// Presentation only. Never shorten the stored request or derive facts from it.
function taskDisplayLabel(task, focus = null, fallback = 'Task overview') {
  const title = task.title || '';
  if (title.trim() && title.length <= 120 && !/[\r\n]/.test(title)) return title;
  const facts = [focus?.approval_scope?.facts, task.facts];
  const workItem = facts.map(value => value?.work_item).find(value =>
    typeof value === 'string' && value.trim() && value.length <= 80 && !/[\r\n]/.test(value));
  return fallback + (workItem ? ' · ' + workItem : '');
}

// Each value comes unchanged from approval_scope.snapshot. Preserve nested and
// unusual values, including falsy values, and escape every key and string.
function scopeValue(value, field = '') {
  if (['followup_required', 'reusable'].includes(field) && (value === 0 || value === 1)) return value ? 'Yes' : 'No';
  if (value === null) return '<span class="scope-empty">null</span>';
  if (typeof value === 'string') return value === '' ? '<span class="scope-empty">Empty text</span>' : esc(value);
  if (Array.isArray(value)) return value.length
    ? `<ol class="scope-list">${value.map(item => `<li>${scopeValue(item)}</li>`).join('')}</ol>`
    : '<span class="scope-empty">Empty list</span>';
  if (typeof value === 'object') return Object.keys(value).length
    ? `<dl class="scope-values">${Object.entries(value).map(([key, item]) => `<dt>${esc(key)}</dt><dd>${scopeValue(item)}</dd>`).join('')}</dl>`
    : '<span class="scope-empty">No recorded values</span>';
  return esc(value);
}

function approvalScope(d) {
  const scope = d.approval_scope;
  const unsafeNumber = value => typeof value === 'number'
    ? !Number.isFinite(value) || Object.is(value, -0) || (Number.isInteger(value) && !Number.isSafeInteger(value))
    : value !== null && typeof value === 'object' && Object.values(value).some(unsafeNumber);
  // Older payloads retain the entire original representation, without parsing.
  // JSON numbers beyond JavaScript's exact integer range must also use the
  // server's literal text; re-encoding them would display a different scope.
  if (!scope || typeof scope !== 'object' || Array.isArray(scope) || unsafeNumber(scope)) {
    return `<pre class="context-box approval-scope">${esc(d.approval_scope_text || '')}</pre>`;
  }
  const labels = d.approval_scope_labels || {};
  const rows = Object.entries(scope).filter(([key, value]) => {
    // These identifiers remain in the exact record below. The question is
    // already the review heading, but only suppress it when it is identical.
    if (['id', 'run_id', 'scope_key'].includes(key) || (key === 'question' && value === d.question)) return false;
    if (['facts', 'applicability'].includes(key)) return true;
    return value !== '' && !(Array.isArray(value) && value.length === 0);
  });
  return `<section class="decision-scope" aria-label="Scope of this decision"><h3>Scope of this decision</h3>
    <dl class="scope-fields">${rows.map(([key, value]) => `<dt>${esc(labels[key] || key)}</dt><dd data-scope-field="${esc(key)}">${scopeValue(value, key)}</dd>`).join('')}</dl>
    <details class="scope-exact" data-keep="scope-${esc(scope.id)}"><summary>Exact recorded scope</summary><p>All recorded fields, including identifiers and empty values.</p><pre class="approval-scope scope-json">${esc(JSON.stringify(scope, null, 2))}</pre><details data-keep="scope-text-${esc(scope.id)}"><summary>Original scope text</summary><pre class="approval-scope scope-text">${esc(d.approval_scope_text || '')}</pre></details></details>
  </section>`;
}

// Quotes are attributed only to evidence supplied with this response. Never
// turn model-supplied IDs or source URLs into navigable links.
function inquiryCitations(result, key = 'inquiry-citations') {
  const evidence = Array.isArray(result?.evidence) ? result.evidence : [];
  const citations = (Array.isArray(result?.citations) ? result.citations : []).flatMap(citation => {
    if (!citation || typeof citation.id !== 'string' || !citation.id ||
        typeof citation.quote !== 'string' || !citation.quote.trim()) return [];
    const matches = evidence.filter(item => item && item.id === citation.id);
    if (matches.length !== 1) return [];
    const source = matches[0];
    return [{source, quote:citation.quote}];
  });
  if (!citations.length) return '';
  const groups = new Map();
  for (const {source, quote} of citations) {
    if (!groups.has(source.id)) groups.set(source.id, {source, quotes:[]});
    groups.get(source.id).quotes.push(quote);
  }
  return `<details class="inquiry-citations" id="${esc(key)}" data-keep="${esc(key)}"><summary>Exact quotes · ${citations.length}</summary>${Array.from(groups.values(), ({source, quotes}) => `<figure><figcaption>${esc(source.title || source.id)}<small>Source: ${esc(source.id)}${source.version ? ` · Version: ${esc(source.version)}` : ''}</small></figcaption>${quotes.map(quote => `<blockquote>${esc(quote)}</blockquote>`).join('')}</figure>`).join('')}</details>`;
}

// An objective-scope decision (Review & take point on Today) stores the
// proposed scope as JSON in its context; the signature binds to that text.
// A person reads a card instead: the outcome, the question, what the move
// will and won't do, its bound and the evidence. Mirrors bridge/objective_text.py:
// nothing is invented, a malformed context gives null and the plain context shows.
const OBJECTIVE_OPTIONS = ['Yes, take point', 'Not now', 'Change the scope'];
const OBJECTIVE_CASES = [['positive', 'Will'], ['negative', 'Won’t'], ['boundary', 'Edge'], ['non_implication', 'Does not imply']];
function objectiveScope(d) {
  if (!d || (d.category !== 'objective_scope' && !String(d.client_ref || '').startsWith('objective-acceptance:'))) return null;
  const lines = String(d.context || '').split('\n');
  while (lines.length && /^(Paths|Options): /.test(lines[lines.length - 1])) lines.pop();
  const text = lines.join('\n').trim();
  if (!text.startsWith('{')) return null;
  let gov;
  try { gov = JSON.parse(text); } catch { return null; }
  if (!gov || typeof gov !== 'object' || Array.isArray(gov)) return null;
  const str = value => typeof value === 'string' ? value.trim() : '';
  const items = value => (Array.isArray(value) ? value.filter(v => typeof v === 'string') : typeof value === 'string' ? value.split('\n') : [])
    .map(line => line.replace(/\s+/g, ' ').trim().replace(/^(?:[-*•–—]\s+)/, '').trim()).filter(Boolean);
  const cases = gov.cases && typeof gov.cases === 'object' && !Array.isArray(gov.cases) ? gov.cases : {};
  const proposal = String(d.client_ref || '').startsWith('objective-acceptance:') ? d.client_ref.slice('objective-acceptance:'.length).trim() : '';
  const ident = str(gov.objective_id) || str(gov.proposal_id) || proposal;
  const what = str(gov.title) || str(gov.outcome);
  return {title: str(gov.title), outcome: str(gov.outcome),
    question: what ? `Should Raven take point on ${what}?` : 'Should Raven take point on this?', options: [...OBJECTIVE_OPTIONS],
    cases: Object.fromEntries(OBJECTIVE_CASES.map(([key]) => [key, items(key === 'non_implication' && !(key in cases) ? cases.non_implications : cases[key])])),
    guardrails: str(gov.guardrails), stop_criteria: str(gov.stop_criteria), success_criteria: str(gov.success_criteria),
    why_now: str(gov.why_now), judgment_kind: str(gov.judgment_kind),
    evidence: (Array.isArray(gov.citations) ? gov.citations : []).slice(0, 12).filter(c => c && typeof c === 'object' && str(c.quote))
      .map(c => ({quote: str(c.quote), title: str(c.title) || str(c.source_title)})),
    link_to_today: ident ? `#today/${encodeURIComponent(ident)}` : ''};
}

// The card for a scope objectiveScope() or the brief's server-made `objective` gave.
// Headings and lists, so a screen reader walks it; quotes fold when long.
function objectiveScopeCard(card, key = 'objective-scope') {
  if (!card || typeof card !== 'object') return '';
  const cases = OBJECTIVE_CASES.filter(([k]) => (card.cases?.[k] || []).length);
  const evidence = Array.isArray(card.evidence) ? card.evidence : [];
  const long = evidence.reduce((n, e) => n + String(e.quote || '').length, 0) > 240;
  return `<section class="objective-scope" aria-label="Proposed scope">
    ${card.outcome ? `<p class="objective-scope-outcome"><strong>Outcome:</strong> ${esc(card.outcome)}</p>` : ''}
    <p class="objective-scope-question"><strong>${esc(card.question || 'Should Raven take point on this?')}</strong> <span class="muted">${(card.options || OBJECTIVE_OPTIONS).map(esc).join(' · ')}</span></p>
    ${cases.map(([k, label]) => `<h4>${label}</h4><ul>${card.cases[k].map(item => `<li>${esc(item)}</li>`).join('')}</ul>`).join('')}
    ${card.guardrails ? `<p><strong>Within:</strong> ${esc(card.guardrails)}</p>` : ''}
    ${card.stop_criteria ? `<p><strong>Stop when:</strong> ${esc(card.stop_criteria)}</p>` : ''}
    ${evidence.length ? `<details class="objective-scope-evidence" id="${esc(key)}-evidence" ${long ? '' : 'open'}><summary>Evidence (${evidence.length})</summary><ul>${evidence.map(e => `<li><blockquote>${esc(e.quote)}</blockquote>${e.title ? `<cite>${esc(e.title)}</cite>` : ''}</li>`).join('')}</ul></details>` : ''}
    ${card.link_to_today ? `<a class="objective-scope-link" href="${esc(card.link_to_today)}">Open in Today</a>` : ''}
  </section>`;
}
