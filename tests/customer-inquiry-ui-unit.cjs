'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
const render = source.slice(source.indexOf('function decisionInquiry('),source.indexOf('async function review('));
const handler = source.slice(source.lastIndexOf("document.addEventListener('submit'"));
const escape = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
(async()=>{
  let submit, finish;
  const calls=[], c={esc:escape,modalVersion:1,document:{addEventListener:(kind,fn)=>submit=fn},
    api:(url,body)=>{calls.push({url,body});return new Promise(resolve=>finish=resolve)}};
  vm.createContext(c);vm.runInContext(render+handler,c);
  const html=vm.runInContext("decisionInquiry({id:'decision',run_id:'task'})",c);
  assert.match(html,/data-task="task"/);assert.match(html,/maxlength="2000"/);assert.match(html,/Read-only clarification/);assert.doesNotMatch(html,/data-action="signoff"/);
  const output={textContent:'',innerHTML:''},button={disabled:false};
  const form={dataset:{decisionInquiry:'decision',task:'task'},isConnected:true,reportValidity:()=>true,
    querySelector:s=>s==='button'?button:s==='textarea'?{value:' What changes? '}:output};
  const event={target:form,preventDefault(){}};
  let pending=submit(event);assert.equal(button.disabled,false);assert.equal(button.ariaDisabled,'true');assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/tasks/task/inquiry');assert.equal(calls[0].body.question,'What changes?');assert.equal(calls[0].body.focus_id,'decision');
  await submit(event);assert.equal(calls.length,1,'double submit must not duplicate a query');
  finish({answer:'<script>bad</script>',evidence:[{title:'<img>',text:'<script>'}],notice:'Reading only'});await pending;
  assert.match(output.innerHTML,/&lt;script&gt;/);assert.doesNotMatch(output.innerHTML,/<script>/);assert.equal(button.disabled,false);assert.equal(button.ariaDisabled,'false');
  output.innerHTML='Original modal';pending=submit(event);c.modalVersion=2;finish({answer:'late reply'});await pending;
  assert.equal(output.innerHTML,'Original modal','late response must not replace another modal');
  console.log('Customer inquiry UI: render, read-only route, duplicate guard, escaped evidence, stale response checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
