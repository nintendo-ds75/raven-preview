'use strict';
const assert = require('node:assert/strict');
const {observations} = require('../evals/audit/viewer-data.js');
const current = {node_id:'d1', question:'Which format?', answer:'Waiting', authorized:false};
const response = value => ({data:{message:{result:{content:[{type:'text',text:JSON.stringify(value)}]}}}});
const events = [
  response({nodes:[current]}),
  response({bundle:{payload:{decisions:[{...current,authorized:true,answer:'OLD'}]}}}),
  response({follows:[{node_id:'d1',question:'Which format?',verdict:'follows'}]}),
  response({nodes:[{...current,authorized:true,answer:'CURRENT',sources:[{ref:'POL-1'}],
    context_history:[{...current,authorized:false,answer:'OLD'}]}]}),
];
assert.equal(observations(events,0).size,0);
assert.equal(observations(events,1).get('d1').authorized,false);
assert.equal(observations(events,2).get('d1').answer,'Waiting');
assert.equal(observations(events,3).get('d1').answer,'Waiting');
assert.equal(observations(events,4).get('d1').answer,'CURRENT');
assert.equal(observations(events,4).get('d1').sources[0].ref,'POL-1');
console.log('6 audit-viewer state assertions passed');
