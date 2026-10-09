/* New synthetic fixture for the 2026-10-09 implementation. No production data. */
'use strict';
const node = (id, extra = {}) => ({node_id:id, question:'Synthetic question ' + id,
  kind:'policy', status:'pending', owner:'Synthetic Reviewer', required_signers:['Synthetic Reviewer'],
  signatures:[], historical_signatures:[], children:[], blocking:true, authorized:false,
  answer:'', context:'Synthetic context', repo:'synthetic/minimal-ui', path:'src/example.py', depth:0,
  ...extra});
const tree = (id = 'synthetic-run-a', extra = {}) => ({task_id:id, title:'Synthetic run overview',
  goal:'Find the synthetic retention answer without changing deployment.', repo:'synthetic/minimal-ui',
  requester:'Synthetic Requester', agent:'Synthetic agent', status:'needs_judgment',
  observed_at:'2026-10-09T00:00:00Z', facts:{customer:'Synthetic Cedar', zero:0, false:false, null:null},
  nodes:[node('pending-a')], notes:[], scope_clarifications:[],
  counts:{blocking:1, total:1}, next:'Wait for Synthetic Reviewer to answer.', ...extra});
const trace = extra => ({events:[], notifications:[], ...extra});
const state = extra => ({auth:{enabled:false}, me:{name:'Local operator', role:'admin'},
  graph:{repos:['synthetic/minimal-ui','synthetic/secondary']}, settings:{}, decisions:[], runs:[],
  owners:[], events:[], executions:[], counts:{needs_you:0}, workspace:{name:'Synthetic workspace'},
  ...extra});
// Explicit API-response double for design review, not a backend authority fixture.
// The fresh human answer has a current-run event; the inherited answer does not.
const design = (earlierDecisionId = 'synthetic-earlier-decision') => ({
  tree:tree('synthetic-design-review',{title:'Preview retention policy',
    goal:'Confirm retention and export behavior for the Cedar preview rollout, then collect the missing scope and contact before implementation.',
    requester:'Synthetic Release Lead', facts:{customer:'Cedar',environment:'preview'},
    nodes:[
      node('document-finding',{question:'How long can preview diagnostic records be kept?',
        source:'record',answer:'The documented preview limit is 21 days after creation.',
        owner:'Synthetic Policy Reviewer',required_signers:['Synthetic Policy Reviewer'],blocking:false,
        sources:[{record_id:'synthetic-policy-record',source_version_id:'synthetic-policy-version-3',
          ref:'Preview retention policy · revision 3',role:'support',current:true,url:'https://policy.example/preview-retention'}],
        owner_evidence:'Recorded policy ownership for preview diagnostic data.'}),
      node('earlier-finding',{question:'May support retain a copy after a customer export?',
        source:'memory',source_id:earlierDecisionId,source_revision:'synthetic-earlier-revision-2',
        answer:'An earlier answer allowed seven days for support copies. This rollout still needs fresh sign-off.',
        answered_by:'Synthetic Minimal Reviewer',owner:'Synthetic Export Reviewer',
        required_signers:['Synthetic Export Reviewer'],signoff:'required',
        owner_evidence:'Recorded referral from the policy reviewer to the export reviewer.',
        related:[{kind:'derived',source_decision_id:earlierDecisionId,source_version_id:'synthetic-earlier-version-2',
          decision_id:'earlier-finding',decision_version_id:'synthetic-current-version-1'}]}),
      node('fresh-answer',{question:'Should preview exports omit internal diagnostic labels?',
        source:'human',answer:'Yes. Omit internal diagnostic labels from customer-facing preview exports.',
        owner:'Synthetic Export Reviewer',answered_by:'Synthetic Export Reviewer',required_signers:['Synthetic Export Reviewer'],
        signatures:['Synthetic Export Reviewer'],signed_by:'Synthetic Export Reviewer',signoff:'signed',authorized:true,blocking:false,
        owner_evidence:'Recorded owner for the preview export format.'}),
      node('contact-gap',{question:'Who approves retention exceptions for partner sandboxes?',
        owner:'',required_signers:[],answer:'',owner_evidence:'No verified exception owner was recorded.'}),
    ],
    scope_clarifications:[{request_key:'synthetic-scope-request',question:'Which partner sandbox is included?',
      scope_clarifications:[{missing_keys:['partner','sandbox'],prior_contact:'Synthetic Earlier Reviewer'}]}],
    counts:{blocking:3,total:4},notes:[],next:'Confirm partner and sandbox scope, identify the exception owner, and collect fresh export sign-off.'}),
  trace:trace({events:[
    {id:1,kind:'owner_changed',decision_id:'earlier-finding',at:'2026-10-09T00:01:00Z',
      detail:{by:'Synthetic Policy Reviewer',to:'Synthetic Export Reviewer',referral:true,why:'Owns export-copy retention decisions.'}},
    {id:2,kind:'owner_approved',decision_id:'fresh-answer',at:'2026-10-09T00:02:00Z',
      detail:{by:'Synthetic Export Reviewer',answer:'Yes. Omit internal diagnostic labels from customer-facing preview exports.',
        fixture_notice:'Explicit synthetic current-run response event; not an authority mechanism probe.'}},
  ]}),
});
module.exports = {node, tree, trace, state, design};
