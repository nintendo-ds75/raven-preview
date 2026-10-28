const assert = require('node:assert/strict');
(async () => {
  const {default:create} = await import('../hosts/opencode/guard.mjs');
  const guard = await create();
  for (const tool of ['bash','read','write','webfetch','task','skill'])
    await assert.rejects(guard['tool.execute.before']({tool,sessionID:'ses_x',callID:'call'}, {args:{}}));
  const output = {args:{path:'a'}};
  await guard['tool.execute.before']({tool:'raven_read_file',sessionID:'ses_x',callID:'native'},output);
  assert.deepEqual(output.args._raven_context,{session_id:'ses_x',call_id:'native'});
  const permission={status:'allow'};
  await guard['permission.ask']({},permission);
  assert.equal(permission.status,'deny');
  console.log('OpenCode native tool and permission guards passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
