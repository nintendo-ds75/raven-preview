'use strict';
// Actual shipped task/brief renderers and expert-modal submit handler; no provider.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const path = require('node:path');
const read = name => fs.readFileSync(path.join(__dirname, '../web', name), 'utf8');
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const quote = 'Exact first line.\n  Second line <script>"&\' remains verbatim.';
const evidence = [{id:'real-source<&',title:'Actual source <img onerror="bad">',version:'version"<&',text:quote,url:'javascript:alert(1)'}];
const result = {answer:'Supported answer',evidence,citations:[{id:evidence[0].id,quote,title:'Fabricated title',url:'javascript:evil()'}]};
const grouped = {...result,evidence:[...evidence,{id:'second-source',title:'Second source',version:'v2'}],citations:[
  {id:evidence[0].id,quote}, {id:'second-source',quote:'Another source quote'},
  ...['Second quote','Third quote','Fourth quote','Fifth quote'].map(quote=>({id:evidence[0].id,quote}))
]};
const fixtures = [result,
  {...result,citations:[{id:'unknown-source',quote:'UNKNOWN QUOTE'}]},
  {...result,citations:[]}, {...result,citations:null},
  {...result,evidence:[]}, {...result,evidence:[...evidence,...evidence]},
  {...result,citations:[null,{id:evidence[0].id,quote:null},{id:evidence[0].id,quote:' '}]}, grouped
];
(async () => {
  const c = {esc,prose:esc,view:'runs',MODEL_WAIT_MS:45000,setTimeout:()=>1,clearTimeout(){},location:{hash:'#runs/task'},data:{focus:{node_id:'decision'}},inquiryView:()=>({question:'',answer:null})};
  vm.createContext(c);vm.runInContext(read('presentation.js')+read('task.js'),c);
  const brief = read('brief.js');
  const start = brief.indexOf('function inquiryCard()');
  vm.runInContext(brief.slice(start,brief.indexOf('\nfunction ',start+1)),c);
  const app = read('app.js');let submit;
  c.document={addEventListener:(_,fn)=>submit=fn};c.modalVersion=1;
  vm.runInContext(app.slice(app.lastIndexOf("document.addEventListener('submit'")),c);
  for (let index=0;index<fixtures.length;index++) {
    const response=fixtures[index];c.response=response;
    c.inquiryView=()=>({question:'',answer:response});c.api=async()=>response;
    const output={innerHTML:'',textContent:''},button={};
    const form={dataset:{decisionInquiry:'decision',task:'task'},isConnected:true,reportValidity:()=>true,
      querySelector:s=>s==='button'?button:s==='textarea'?{value:'Question'}:output};
    await submit({target:form,preventDefault(){}});
    const outputs=[vm.runInContext("taskView('task').inquiryResult=response; taskInquiry('task')",c),vm.runInContext('inquiryCard()',c),output.innerHTML];
    for (const html of outputs) {
      if (index===0) {
        assert.match(html,/<summary>Exact quotes · 1<\/summary>/);
        assert(html.includes(`<blockquote>${esc(quote)}</blockquote>`),'exact quote must preserve characters and line breaks');
        assert(html.includes(`Source: ${esc(evidence[0].id)}`));
        assert(html.includes(`Version: ${esc(evidence[0].version)}`));
        assert(html.includes(esc(evidence[0].title)));
        assert.doesNotMatch(html,/<details[^>]*\bopen(?:\s|=|>)/,'citation starts collapsed');
      } else if(response===grouped) {
        assert.match(html,/<summary>Exact quotes · 6<\/summary>/);
        const figures=[...html.matchAll(/<figure>(.*?)<\/figure>/gs)].map(match=>match[1]);
        assert.equal(figures.length,2,'one figure per unique evidence source');
        for(let i=0;i<grouped.evidence.length;i++) {
          const source=grouped.evidence[i],figure=figures[i];
          assert.equal(figure.split(esc(source.title)).length-1,1,'source title shown once');
          assert.equal(figure.split('Source: '+esc(source.id)).length-1,1,'source ID shown once');
          assert.equal(figure.split('Version: '+esc(source.version)).length-1,1,'source version shown once');
          const quotes=[...figure.matchAll(/<blockquote>(.*?)<\/blockquote>/gs)].map(match=>match[1]);
          assert.deepEqual(quotes,grouped.citations.filter(citation=>citation.id===source.id).map(citation=>esc(citation.quote)),'preserve within-source quote order');
        }
        assert.doesNotMatch(html,/<details[^>]*\bopen(?:\s|=|>)/,'grouped citations start collapsed');
      } else assert.doesNotMatch(html,/Exact quotes|<blockquote>/,'unknown, ambiguous or empty citations withheld');
      assert.doesNotMatch(html,/Fabricated title|UNKNOWN QUOTE|javascript:|href=|<script>|<img|onerror="bad"/);
    }
  }
  assert.match(vm.runInContext("taskInquiry('task')",c),/Ask Raven about this task/);
  assert.doesNotMatch(vm.runInContext('inquiryCard()',c),/does not answer or sign off|Clarification is not approval/,'the task page has no line explaining the question box');
  console.log('Citation UI: task, expert modal and brief render exact escaped quotes with evidence identity; unknown, ambiguous and empty targets withheld; no links; collapsed by default.');
})().catch(error=>{console.error(error);process.exitCode=1});
