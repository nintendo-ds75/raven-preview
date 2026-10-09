/* Actual local Chromium workflow: explicit task context, never approval. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const readline = require('node:readline');
const {once} = require('node:events');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'raven-work-item-browser-'));
  const child = spawn(process.env.BRIDGE_PYTHON || 'python3',
    [path.join(root, 'tests/work_item_browser_server.py'), path.join(temp, 'context.db')], {cwd:root});
  let stderr = '', browser;
  child.stderr.on('data', chunk => { stderr += chunk; });
  try {
    const lines = readline.createInterface({input:child.stdout});
    const fixture = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Fixture startup timeout: ' + stderr)), 30000);
      lines.on('line', line => { try { const value = JSON.parse(line); clearTimeout(timer); resolve(value); } catch {} });
      child.once('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited ' + code + ': ' + stderr)); });
    });
    const state = await (await fetch(fixture.url + '/api/state')).json();
    const post = async (route, data) => {
      const response = await fetch(fixture.url + route, {method:'POST', headers:{'Content-Type':'application/json',
        'X-Bridge-CSRF':state.csrf_token}, body:JSON.stringify(data)});
      assert.equal(response.status, 200, await response.clone().text());
      return response.json();
    };
    const read = async route => (await fetch(fixture.url + route)).json();
    const tool = async (name, args) => {
      const response = await post('/mcp', {jsonrpc:'2.0', id:1, method:'tools/call', params:{name, arguments:args}});
      assert(!response.error, JSON.stringify(response));
      assert(!response.result.isError, JSON.stringify(response.result));
      return JSON.parse(response.result.content[0].text);
    };
    const decision = () => read('/api/decisions/' + fixture.node_id);
    const decisionState = d => Object.fromEntries(['answer','updated_at','signoff','signatures','signed_by','signed_hash',
      'signed_revision','authorized','sources','context_history'].map(key => [key, d[key]]));
    const initial = decisionState(await decision());
    assert.equal(initial.authorized, false);
    assert.deepEqual(initial.sources, []);
    browser = await chromium.launch({headless:true,
      ...(process.env.BRIDGE_BROWSER ? {executablePath:process.env.BRIDGE_BROWSER} : {})});
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const artifacts = process.env.BRIDGE_TEST_ARTIFACTS || path.join(root, 'test-results');
    fs.mkdirSync(artifacts, {recursive:true});
    const screenshot = name => page.screenshot({path:path.join(artifacts, name), fullPage:true});
    const taskPage = async (taskId = fixture.task_id) => {
      await page.goto(fixture.url + '/#runs/' + taskId);
      await page.locator('#task-tab-overview').waitFor();
      await page.locator('#task-tab-overview').click();
      const context = page.locator('#task-context');
      if (!(await context.evaluate(el => el.open))) await context.locator(':scope > summary').click();
      await page.locator('#task-context .work-item-context').waitFor();
    };
    const openOwner = async (nodeId = fixture.node_id) => {
      await page.evaluate(id => review(id), nodeId);
      await page.locator('#modal[open] .work-item-context').waitFor();
    };
    const noContextControls = async () => {
      assert.equal(await page.locator('#modal .work-item-context input, #modal .work-item-context button, #modal .work-item-context form').count(), 0);
      assert.equal(await page.locator('#source-review-signoff').count(), 0);
    };
    await taskPage();
    assert.match(await page.locator('#task-context .work-item-context').innerText(), /CASE-1 · Unlinked/);
    await screenshot('work-item-unlinked-task.png');
    await openOwner();
    assert.match(await page.locator('#modal .work-item-context').innerText(), /no explicit work-item link/);
    await noContextControls();
    await page.getByRole('button', {name:'Close',exact:true}).last().click();
    assert.deepEqual(decisionState(await decision()), initial);

    // Read-only lookup and exact read preserve the unlinked state.
    const ambiguousLookup = await tool('bridge_lookup_record', {repo:fixture.repo, ref:'CASE-1'});
    assert.equal(ambiguousLookup.status, 'ambiguous');
    const lookupIdentity = {repo:fixture.repo, provider:fixture.source.provider, namespace:fixture.source.namespace,
      object_kind:fixture.source.kind, external_id:fixture.source.external_id};
    const found = await tool('bridge_lookup_record', lookupIdentity);
    assert.equal(found.status, 'matched');
    const record = await tool('bridge_get_record', {repo:fixture.repo, record_id:found.source.record_id});
    assert.equal((await tool('bridge_get_tree', {task_id:fixture.task_id})).work_item_association.status, 'unlinked');
    const s = record.source;
    const args = {task_id:fixture.task_id, repo:fixture.repo, record_id:s.record_id, source_version_id:s.source_version_id,
      provider:s.provider, namespace:s.namespace, object_kind:s.kind, external_id:s.external_id};
    assert.equal((await tool('bridge_link_work_item', args)).changed, true);
    const retry = await post('/api/tasks/' + fixture.task_id + '/work-items', args);
    assert.equal(retry.changed, false);
    const linked = await tool('bridge_lookup_record', lookupIdentity);
    assert.equal(linked.task_anchors.items.filter(a => a.task_id === fixture.task_id).length, 1);
    assert.equal(linked.task_decisions.items.find(d => d.task_id === fixture.task_id).relation, 'task_anchor_association');
    assert.deepEqual(linked.decision_sources.items.filter(d => d.task_id === fixture.task_id), []);
    assert.deepEqual(decisionState(await decision()), initial);

    await taskPage();
    // The mutation occurred through a separate API client. Same-hash
    // navigation can retain the prior render until the normal task poll.
    await page.waitForFunction(() => document.querySelector('#task-context .work-item-context')?.textContent
      .includes('CASE-1 · Linked association'));
    const taskContext = await page.locator('#task-context .work-item-context').innerText();
    for (const text of ['CASE-1 · Linked association', s.record_id, s.source_version_id, 'current observed version',
      'not supporting evidence or approval']) assert(taskContext.includes(text), text);
    await openOwner();
    await noContextControls();
    await page.getByText('Task work-item association history', {exact:true}).click();
    const ownerContext = await page.locator('#modal .work-item-context').innerText();
    for (const text of [s.record_id, s.source_version_id, 'Explicit task association']) assert(ownerContext.includes(text), text);
    await screenshot('work-item-linked-owner-history.png');
    await page.getByRole('button', {name:'Close',exact:true}).last().click();
    await page.locator('#task-tab-history').click();
    const timeline = page.locator('.task-timeline');
    assert.equal(await timeline.getByText('work item linked', {exact:true}).count(), 1);
    assert((await timeline.innerText()).includes('Explicit task association, never supporting evidence or approval.'));
    await screenshot('work-item-linked-task-history.png');

    // A newer source observation never silently moves the old association.
    const updated = await post('/api/records', {...fixture.source_data, body:'A newer work-item observation.'});
    assert.notEqual(updated.source.source_version_id, s.source_version_id);
    const tree = await tool('bridge_get_tree', {task_id:fixture.task_id});
    assert.equal(tree.work_item_association.status, 'linked');
    assert.equal(tree.source_anchors[0].source_version_id, s.source_version_id);
    assert.equal(tree.source_anchors[0].current, false);
    assert.deepEqual(decisionState(await decision()), initial);
    await taskPage();
    await page.waitForFunction(() => document.querySelector('#task-context .work-item-context')?.textContent
      .includes('historical or unavailable version'));
    assert((await page.locator('#task-context .work-item-context').innerText()).includes('historical or unavailable version'));
    assert((await page.locator('#task-context .work-item-context').innerText()).includes(s.source_version_id));
    await screenshot('work-item-historical-task.png');
    await openOwner();
    await noContextControls();
    assert((await page.locator('#modal .work-item-context').innerText()).includes('historical or unavailable version'));
    await page.getByRole('button', {name:'Close',exact:true}).last().click();
    assert.deepEqual(decisionState(await decision()), initial);
    // Existing decision-level work-item pins stay distinctly labeled and
    // expose identities even though their task has no anchor.
    await taskPage(fixture.decision_task);
    await openOwner(fixture.decision_node);
    await noContextControls();
    const ownContext = await page.locator('#modal .work-item-context').innerText();
    assert(ownContext.includes('Decision association'));
    assert(!ownContext.includes('Task association'));
    assert(ownContext.includes(s.record_id));
    assert(ownContext.includes(s.source_version_id));
    await screenshot('work-item-decision-only-owner.png');
    await page.getByRole('button', {name:'Close',exact:true}).last().click();
    assert.equal((await read('/api/decisions/' + fixture.decision_node)).authorized, false);
    // Tree nodes carry both canonical identities even without a duplicate
    // source_anchors field on the node payload.
    await taskPage(fixture.ambiguous_task);
    await page.locator('#task-tab-decisions').click();
    const question = page.locator(`.task-question[data-node-id="${fixture.ambiguous_node}"]`);
    if (!(await question.evaluate(el => el.open))) await question.locator(':scope > summary').click();
    const ambiguousContext = question.locator('.work-item-context');
    assert((await ambiguousContext.innerText()).includes('Ambiguous association'));
    for (const item of fixture.ambiguous_sources) assert((await ambiguousContext.innerText()).includes(item.record_id));
    assert.equal(await ambiguousContext.locator('[data-work-item-origin="task"]').count(), 2);
    await screenshot('work-item-ambiguous-node.png');
    await taskPage(fixture.closed_task);
    const closed = await page.locator('#task-context .work-item-context').innerText();
    assert(closed.includes('Historical Unlinked'));
    assert(closed.includes('start a new task'));
    await screenshot('work-item-historical-unlinked-task.png');
    assert.deepEqual(errors, []);
    const result = {ok:true, browser:'Chromium', checks:['MCP exact lookup/read remains unlinked', 'MCP explicit link',
      'REST idempotent retry', 'task unlinked-to-linked display', 'owner association history', 'one task history event',
      'historical version retained after source update', 'close/reopen never signs', 'no context approval controls',
      'decision answer/revision/edges/signatures unchanged', 'decision-only association origin and identity',
      'ambiguous node displays both task identities', 'closed unlinked task suggests new task', 'no browser errors'],
      screenshots:['work-item-unlinked-task.png','work-item-linked-owner-history.png','work-item-linked-task-history.png',
        'work-item-historical-task.png','work-item-decision-only-owner.png','work-item-ambiguous-node.png','work-item-historical-unlinked-task.png']};
    fs.writeFileSync(path.join(artifacts, 'work-item-browser-result.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null && child.signalCode === null) { child.kill('SIGTERM'); await once(child, 'exit').catch(() => {}); }
    fs.rmSync(temp, {recursive:true, force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
