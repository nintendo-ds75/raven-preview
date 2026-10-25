"""Isolated JSON-lines bridge to supported coding harnesses, never a model loop.

Only normalized text, tool calls, checkpoints and usage leave this process.
Provider reasoning and raw authentication errors are not persisted or displayed.
"""
import asyncio
from collections import defaultdict, deque
import json
import os
import sys


def emit(value):
    print(json.dumps(value), flush=True)


async def claude(config):
    from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient, PermissionResultDeny,
                                 ResultMessage, AssistantMessage, SystemMessage, TextBlock,
                                 create_sdk_mcp_server, tool, HookMatcher)
    pending = {}
    delivered = False
    observed_calls = defaultdict(deque)

    async def before_tool(payload, tool_use_id, context):
        name = payload.get('tool_name', '')
        if not name.startswith('mcp__raven__'):
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                    'permissionDecisionReason': 'Use Raven task tools only.'}}
        ident = tool_use_id or payload.get('tool_use_id')
        if not ident:
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                    'permissionDecisionReason': 'A stable tool call ID is required.'}}
        key = (name.removeprefix('mcp__raven__'), json.dumps(payload.get('tool_input', {}), sort_keys=True))
        observed_calls[key].append(ident)
        return {}

    async def deny(name, args, context):
        return PermissionResultDeny(message='Use the task-scoped Raven tools; native tools are unavailable.')

    def wrap(spec):
        async def execute(args):
            key = (spec['name'], json.dumps(args, sort_keys=True))
            if not observed_calls[key]:
                return {'content': [{'type': 'text', 'text': 'Missing stable tool identity; operation refused'}], 'isError': True}
            ident = observed_calls[key].popleft()
            future = asyncio.get_running_loop().create_future()
            pending[ident] = future
            emit({'kind': 'tool', 'call': {'id': ident, 'name': spec['name'], 'input': args}})
            try:
                result = await future
                return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': 'error' in result}
            finally:
                pending.pop(ident, None)
        return tool(spec['name'], spec['description'], spec['input_schema'])(execute)

    specs = config['tools']
    options = ClaudeAgentOptions(
        tools=[], allowed_tools=['mcp__raven__' + s['name'] for s in specs],
        mcp_servers={'raven': create_sdk_mcp_server(name='raven', version='1.0.0', tools=[wrap(s) for s in specs])},
        strict_mcp_config=True, can_use_tool=deny, permission_mode='dontAsk',
        hooks={'PreToolUse': [HookMatcher(hooks=[before_tool])]},
        model=config['model'], system_prompt=config['system'], cwd=config['cwd'],
        setting_sources=[], plugins=[], agents={}, skills=[],
        max_turns=config['max_turns'], max_budget_usd=config['budget_usd'],
        resume=config['session_id'] or None)

    async def commands():
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                await client.interrupt()
                return
            value = json.loads(line)
            if value.get('kind') == 'interrupt':
                await client.interrupt()
            elif value.get('kind') == 'tool_result':
                future = pending.get(value['id'])
                if future and not future.done():
                    future.set_result(value['result'])

    async with ClaudeSDKClient(options=options) as client:
        listener = asyncio.create_task(commands())
        try:
            await client.query(config['prompt'])
            async for message in client.receive_response():
                if isinstance(message, SystemMessage) and message.subtype == 'init':
                    emit({'kind': 'session', 'id': message.data['session_id']})
                elif isinstance(message, AssistantMessage):
                    if not delivered:
                        emit({'kind': 'delivery', 'accepted': True})
                        delivered = True
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            emit({'kind': 'text', 'text': block.text})
                elif isinstance(message, ResultMessage):
                    if not delivered and message.num_turns > 0:
                        emit({'kind': 'delivery', 'accepted': True})
                        delivered = True
                    emit({'kind': 'session', 'id': message.session_id})
                    emit({'kind': 'result', 'status': message.subtype, 'cost_usd': message.total_cost_usd,
                          'usage': message.usage, 'turns': message.num_turns, 'is_error': message.is_error})
        finally:
            listener.cancel()
            # stdin's blocked reader is a daemon in the process; main uses
            # os._exit only after SDK disconnect completes below.


async def codex(config):
    # No operator bypass: pinned native flags do not establish a tools-only
    # boundary. Keep research transport separate from production dispatch.
    raise RuntimeError('codex_native_tool_boundary_unverified')


async def _codex_transport(config):
    """Pinned app-server protocol. Dynamic tools require experimentalApi.

    Research transport only; production codex() refuses to run it. Native
    built-ins may remain enabled despite these flags and bypass parent tool
    dispatch. Read-only is not protection against reads or network disclosure.
    Every permission request we receive is denied, but that is not sufficient.
    """
    binary = os.environ['RAVEN_CODEX_BIN']
    flags = {'shell_tool': False, 'unified_exec': False, 'apps': False, 'plugins': False,
             'multi_agent': False, 'hooks': False, 'browser_use': False, 'computer_use': False,
             'code_mode': False, 'code_mode_host': False, 'image_generation': False, 'memories': False,
             'view_image': False, 'skill_search': False, 'skill_mcp_dependency_install': False}
    args = [binary, 'app-server']
    for name, enabled in flags.items():
        args += ['-c', 'features.' + name + '=' + str(enabled).lower()]
    args += ['-c', 'web_search="disabled"', '-c', 'forced_login_method="api"']
    process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    requests, tool_requests = {}, {}
    allowed_tools = {spec['name'] for spec in config['tools']}
    completed = False
    interrupted = False
    delivered = False
    turn_id, thread_id = '', config['session_id']
    done = asyncio.Event()
    sequence = 0

    async def send(value):
        process.stdin.write((json.dumps(value) + '\n').encode())
        await process.stdin.drain()

    async def request(method, params):
        nonlocal sequence
        sequence += 1
        ident = sequence
        future = asyncio.get_running_loop().create_future()
        requests[ident] = future
        await send({'id': ident, 'method': method, 'params': params})
        try:
            return await asyncio.wait_for(future, timeout=60)
        finally:
            requests.pop(ident, None)

    async def read():
        nonlocal turn_id, completed, delivered
        try:
            while line := await process.stdout.readline():
                event = json.loads(line)
                method, params = event.get('method'), event.get('params') or {}
                if method is None and event.get('id') in requests:
                    future = requests.pop(event['id'])
                    if not future.done():
                        if 'error' in event:
                            future.set_exception(RuntimeError('app_server_request_failed'))
                        else:
                            future.set_result(event.get('result', {}))
                elif method == 'item/tool/call':
                    ident = params.get('callId')
                    valid = (params.get('threadId') == thread_id and turn_id
                             and params.get('turnId') == turn_id and not completed
                             and isinstance(ident, str) and ident and ident not in tool_requests
                             and params.get('tool') in allowed_tools
                             and isinstance(params.get('arguments'), dict))
                    if not valid:
                        await send({'id': event['id'], 'result': {'success': False, 'contentItems': [
                            {'type': 'inputText', 'text': 'Invalid, duplicate or out-of-scope task tool call'}]}})
                        continue
                    tool_requests[ident] = event['id']
                    emit({'kind': 'tool', 'call': {'id': ident, 'name': params['tool'], 'input': params['arguments']}})
                elif method == 'turn/started' and params.get('threadId') == thread_id:
                    # The notification can precede turn/start's response. Only
                    # the selected thread may establish this turn's identity.
                    candidate = params.get('turn', {}).get('id')
                    if candidate and not turn_id:
                        turn_id = candidate
                        if not delivered:
                            emit({'kind': 'delivery', 'accepted': True})
                            delivered = True
                elif method == 'item/completed' and params.get('threadId') == thread_id and params.get('turnId') == turn_id and not completed:
                    if params.get('item', {}).get('type') == 'agentMessage':
                        emit({'kind': 'text', 'text': params['item'].get('text', '')})
                elif method == 'thread/tokenUsage/updated' and params.get('threadId') == thread_id:
                    emit({'kind': 'usage', 'usage': params.get('tokenUsage')})
                elif method == 'turn/completed' and params.get('threadId') == thread_id and params.get('turn', {}).get('id') == turn_id and not completed:
                    status = params['turn'].get('status', 'unknown')
                    completed = True
                    emit({'kind': 'result', 'status': status,
                          'is_error': status != 'completed', 'turn_id': turn_id})
                    done.set()
                elif method and 'id' in event:
                    # Unrecognized server requests never gain implicit approval.
                    if method.endswith('/requestApproval'):
                        result = {'decision': 'decline'}
                    elif method.endswith('/requestPermissions'):
                        result = {'permissions': {}, 'scope': 'turn'}
                    else:
                        await send({'id': event['id'], 'error': {'code': -32601, 'message': 'Unavailable in Raven host'}})
                        continue
                    await send({'id': event['id'], 'result': result})
        finally:
            # EOF or a malformed frame cannot leave initialize/start waiting
            # for a full timeout, nor masquerade as a successful completion.
            for future in list(requests.values()):
                if not future.done():
                    future.set_exception(RuntimeError('app_server_disconnected'))
            done.set()

    async def commands():
        nonlocal interrupted
        while line := await asyncio.to_thread(sys.stdin.readline):
            value = json.loads(line)
            if value.get('kind') == 'tool_result' and value['id'] in tool_requests:
                await send({'id': tool_requests.pop(value['id']), 'result': {
                    'success': 'error' not in value['result'], 'contentItems': [
                        {'type': 'inputText', 'text': json.dumps(value['result'])}]}})
            elif value.get('kind') == 'interrupt':
                interrupted = True
                if turn_id:
                    await request('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})
        interrupted = True
        if turn_id and not completed:
            await request('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})

    reader = asyncio.create_task(read())
    listener = asyncio.create_task(commands())
    try:
        await request('initialize', {'clientInfo': {'name': 'raven', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
        await send({'method': 'initialized'})
        common = {'model': config['model'], 'modelProvider': 'openai', 'cwd': config['cwd'],
                  'sandbox': 'read-only', 'approvalPolicy': 'never', 'developerInstructions': config['system']}
        if thread_id:
            result = await request('thread/resume', {**common, 'threadId': thread_id})
        else:
            result = await request('thread/start', {**common, 'allowProviderModelFallback': False,
                'dynamicTools': [{'type': 'function', 'name': s['name'], 'description': s['description'],
                                  'inputSchema': s['input_schema']} for s in config['tools']]})
        if result.get('model') != config['model'] or result.get('modelProvider') != 'openai':
            raise RuntimeError('app_server_model_identity_changed')
        selected_thread = result['thread']['id']
        if thread_id and selected_thread != thread_id:
            raise RuntimeError('app_server_resume_identity_changed')
        thread_id = selected_thread
        emit({'kind': 'session', 'id': thread_id})
        if interrupted:
            emit({'kind': 'result', 'status': 'interrupted', 'is_error': True})
            return
        result = await request('turn/start', {'threadId': thread_id,
                'input': [{'type': 'text', 'text': config['prompt'], 'text_elements': []}]})
        selected_turn = result['turn']['id']
        if turn_id and selected_turn != turn_id:
            raise RuntimeError('app_server_turn_identity_changed')
        turn_id = selected_turn
        if not delivered:
            emit({'kind': 'delivery', 'accepted': True})
            delivered = True
        if interrupted and not completed:
            await request('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})
        await done.wait()
        if reader.done():
            await reader
        if not completed:
            raise RuntimeError('app_server_missing_final_receipt')
    finally:
        reader.cancel()
        if listener:
            listener.cancel()
        await asyncio.gather(reader, *([listener] if listener else []), return_exceptions=True)
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


def main():
    config = json.loads(sys.stdin.readline())
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(claude(config) if config['provider'] == 'claude' else codex(config))
    except Exception as error:
        # Raw provider errors can echo prompts and credentials.
        emit({'kind': 'error', 'code': type(error).__name__})
    finally:
        loop.close()
        # A blocking stdin reader must not keep an otherwise finished host alive.
        os._exit(0)


if __name__ == '__main__':
    main()
