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
