// Fresh evidence display is independent from human revalidation controls.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const app = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const brief = fs.readFileSync(path.join(__dirname, '../web/brief.js'), 'utf8');
const context = {};
vm.createContext(context);
vm.runInContext(app.slice(app.indexOf('const esc ='), app.indexOf('const plural =')), context);
vm.runInContext(app.slice(app.indexOf('function sourceSnapshots('), app.indexOf('function sourceReplacementField(')), context);
vm.runInContext(brief.slice(brief.indexOf('function sourceReview('), brief.indexOf('function focusCard(')), context);
const pin = {record_id:'exact-record',source_version_id:'exact-version',role:'support'};
const source = {...pin, snapshot:{body:'Complete source body\n<svg onload="throw 1">& END',
  display_ref:'POL-1',title:'Title <script>',author:'Writer',status:'Done',paths:['policy/a.py'],
  provider:'jira',namespace:'source.example',external_id:'POL-1',custom_metadata:'Exact metadata retained'}};
const review = {available:true,has_reliance:true,sources:[source],pins:[pin],decision_pins:[],dependencies:[],notice:'Current source snapshot'};
const citations = [{...pin,ref:'POL-1',sequence:1,namespace:'source.example',url:'javascript:throw 1'}];
const fresh = context.sourceEvidence(citations, review);
for (const text of ['exact-record','exact-version','support','Complete source body','&lt;svg', 'source.example','Exact metadata retained']) assert(fresh.includes(text),text);
assert(!fresh.includes('<svg'));
assert(!fresh.includes('href="javascript:'));
assert(!fresh.includes('name="use_current_sources"'));
assert.equal(context.sourceRevalidationFields({needs_review:0,source_revalidation:review}, 'signoff'),'');
const stale = context.sourceRevalidationFields({needs_review:1,source_revalidation:review}, 'signoff');
assert(stale.includes('Complete source body'));
assert(stale.includes('name="use_current_sources"'));
assert(stale.includes('id="source-pins-signoff"'));
let payload = {use_current_sources:'yes',current_source_pins:JSON.stringify([pin]),current_source_decisions:'[]'};
context.takeSourceRevalidation(payload);
assert.equal(JSON.stringify(payload),JSON.stringify({source_evidence:[pin],source_decision_pins:[]}));
for (const role of ['context','work_item']) {
  const info = {...review,has_reliance:false,sources:[{...source,role}],pins:[{...pin,role}]};
  const rendered = context.sourceReview({source_revalidation:info});
  for (const text of ['exact-record','exact-version',role,'Complete source body','Exact metadata retained']) assert(rendered.includes(text),text);
  assert(!rendered.includes('id="brief-source-confirm"'));
  assert(!rendered.includes('Signing waits'));
  assert(rendered.includes('informational'));
}
assert(context.sourceReview({source_revalidation:review}).includes('id="brief-source-confirm"'));
assert.equal(context.sourceEvidence([]), '');
assert.equal(context.sourceReview({source_revalidation:{has_reliance:false,sources:[],dependencies:[]}}),'');
console.log('Fresh source review rendering checks passed');
