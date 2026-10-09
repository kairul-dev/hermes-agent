"""Session policy through production RPC dispatch and real isolated SQLite stores."""
import io
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import StdioTransport


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    homes = [tmp_path / 'profile-a', tmp_path / 'profile-b']
    databases = []
    records = {}
    peer = StdioTransport(lambda: io.StringIO(), threading.Lock())
    for name, home in zip(('A', 'B', 'C'), (homes[0], homes[1], homes[0])):
        home.mkdir(exist_ok=True)
        db = SessionDB(db_path=home / 'state.db')
        databases.append(db)
        agent = SimpleNamespace(session_id='stored-' + name, _session_db=db, platform='desktop')
        record = dict(transport=peer, agent=agent, session_key=agent.session_id,
                      profile_home=str(home), source='desktop', history=[], running=False)
        records[name] = record
        monkeypatch.setitem(server._sessions, name, record)
    yield peer, records
    for db in databases:
        db.close()


def rpc(peer, method, **params):
    return server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, peer)


def set_policy(peer, sid, enabled=True, provider='openrouter', model='worker-a', effort='high'):
    return rpc(peer, 'orchestration.set', session_id=sid, enabled=enabled, worker_provider=provider,
               worker_model=model, worker_reasoning_effort=effort)


def test_set_persists_in_owning_db_and_get_restores_profile_a_b_a(sessions):
    peer, records = sessions
    a = set_policy(peer, 'A')
    assert 'result' in a, a
    assert a['result']['worker_model'] == 'worker-a'
    assert rpc(peer, 'orchestration.get', session_id='B')['result']['enabled'] is False
    c = set_policy(peer, 'C', provider='anthropic', model='worker-c', effort='')
    assert c['result']['worker_provider'] == 'anthropic'
    assert rpc(peer, 'orchestration.get', session_id='A')['result'] == a['result']
    # Close/reopen the real stores; reminting a runtime id still reads the durable agent id.
    db = records['A']['agent']._session_db
    db.patch_session_model_config('stored-A', {'lineage-marker': 'keep'})
    path = db.db_path
    db.close()
    reopened = SessionDB(db_path=path)
    records['A']['agent']._session_db = reopened
    try:
        assert reopened.get_session_model_config_value('stored-A', 'lineage-marker') == 'keep'
        assert rpc(peer, 'orchestration.get', session_id='A')['result'] == a['result']
        # Disabling with blank UI fields must remember the previous worker.
        disabled = set_policy(peer, 'A', False, '', '', '')['result']
        assert disabled['enabled'] is False
        assert disabled['worker_provider'] == 'openrouter'
        assert disabled['worker_model'] == 'worker-a'
        assert disabled['worker_reasoning_effort'] == 'high'
        assert rpc(peer, 'orchestration.get', session_id='C')['result'] == c['result']
    finally:
        reopened.close()


@pytest.mark.parametrize('provider,model,effort', [('', 'worker', ''), ('openrouter', '', ''),
                                                     ('openrouter', 'worker', 'nonsense')])
def test_set_invalid_selection_rejects_before_materializing(sessions, provider, model, effort):
    peer, records = sessions
    response = set_policy(peer, 'A', True, provider, model, effort)
    assert response.get('error', {}).get('code') == 4000, response
    assert records['A']['agent']._session_db.get_session('stored-A') is None


@pytest.mark.parametrize('bad', [False, {}, {'version': 1, 'enabled': 'true'},
                                {'version': 2, 'enabled': False}])
def test_get_rejects_malformed_saved_policy(sessions, bad):
    peer, records = sessions
    db = records['A']['agent']._session_db
    db.create_session('stored-A', source='desktop')
    db.patch_session_model_config('stored-A', {'_orchestration': bad})
    response = rpc(peer, 'orchestration.get', session_id='A')
    assert response.get('error', {}).get('code') == 4000, response


def test_set_requires_authority_and_verified_readback(sessions, monkeypatch):
    peer, records = sessions
    stranger = StdioTransport(lambda: io.StringIO(), threading.Lock())
    assert set_policy(stranger, 'A')['error']['code'] == 4001
    for sid in ('', 'stale', 'stored-A'):
        assert set_policy(peer, sid)['error']['code'] == 4001
    db = records['A']['agent']._session_db
    # Fault-injection only at the persistence boundary: a silently dropped UPDATE is not success.
    monkeypatch.setattr(db, 'patch_session_model_config', lambda *args: None)
    assert set_policy(peer, 'A')['error']['code'] == 5033


@pytest.mark.parametrize('override', [None, {'provider': 'openrouter', 'model': 'escape'}])
def test_desktop_off_blocks_actual_delegate_before_any_config_or_provider(sessions, monkeypatch, override):
    from tools import delegate_tool
    peer, records = sessions
    parent = records['B']['agent']
    def forbidden(*args, **kwargs):
        pytest.fail('OFF reached configuration/provider/child construction')
    monkeypatch.setattr(delegate_tool, '_load_config', forbidden)
    monkeypatch.setattr(delegate_tool, '_resolve_delegation_credentials', forbidden)
    monkeypatch.setattr(delegate_tool, '_build_child_agent', forbidden)
    result = json.loads(delegate_tool.delegate_task(goal='must not spawn', parent_agent=parent,
                                                  credentials_cfg=override))
    assert 'orchestration' in result.get('error', '').lower(), result
    assert json.loads(delegate_tool.delegate_task(action='list', parent_agent=parent)).get('error') is None
    assert 'no live subagent' in json.loads(delegate_tool.delegate_task(action='stop', subagent_id='missing',
                                                               parent_agent=parent))['error'].lower()
    assert 'no live subagent' in json.loads(delegate_tool.delegate_task(action='steer', subagent_id='missing',
                                    message='cleanup', parent_agent=parent))['error'].lower()


@pytest.mark.parametrize('platform', ['desktop', 'cli', 'telegram'])
def test_persisted_off_is_enforced_on_every_surface(sessions, monkeypatch, platform):
    from tools import delegate_tool
    peer, records = sessions
    assert 'result' in set_policy(peer, 'A', False, '', '', '')
    parent = records['A']['agent']
    parent.platform = platform
    monkeypatch.setattr(delegate_tool, '_load_config', lambda: pytest.fail('OFF reached global config'))
    result = json.loads(delegate_tool.delegate_task(goal='blocked', parent_agent=parent))
    assert 'orchestration' in result.get('error', '').lower(), result


@pytest.mark.parametrize('failure', ['read-error', 'malformed'])
def test_desktop_storage_failure_is_fail_closed(sessions, monkeypatch, failure):
    from tools import delegate_tool
    peer, records = sessions
    parent = records['A']['agent']
    if failure == 'read-error':
        def unavailable(*args):
            raise sqlite3.OperationalError('forced offline DB')
        monkeypatch.setattr(parent._session_db, 'get_session', unavailable)
    else:
        parent._session_db.create_session(parent.session_id, source='desktop')
        parent._session_db.patch_session_model_config(parent.session_id, {'_orchestration': False})
    monkeypatch.setattr(delegate_tool, '_load_config', lambda: pytest.fail('invalid policy reached global config'))
    result = json.loads(delegate_tool.delegate_task(goal='blocked', parent_agent=parent))
    assert 'orchestration' in result.get('error', '').lower(), result


def test_on_routes_real_children_for_a_and_c_without_global_pin_leak(sessions, monkeypatch):
    import copy
    import httpx
    import run_agent
    from hermes_cli import runtime_provider
    from tools import delegate_tool

    peer, records = sessions
    assert 'result' in set_policy(peer, 'A', True, 'openrouter', 'gpt-4.1-mini', 'high')
    assert 'result' in set_policy(peer, 'C', True, 'openai', 'gpt-4.1', '')
    cfg = dict(provider='wrong-global', model='wrong-model', reasoning_effort='low',
               base_url='https://global.invalid', api_key='global-test-key', api_mode='anthropic_messages',
               request_overrides={'global-leak': True}, fallback_providers=[{'provider': 'wrong', 'model': 'wrong'}],
               max_iterations=19, max_concurrent_children=3, max_spawn_depth=1)
    before = copy.deepcopy(cfg)
    monkeypatch.setattr(delegate_tool, '_load_config', lambda: cfg)
    resolutions = []
    def resolve(requested, target_model):
        resolutions.append((requested, target_model))
        return dict(provider=requested, base_url='https://' + requested + '.invalid/v1',
                    api_key='offline-test-key', api_mode='chat_completions', model=target_model,
                    request_overrides={'extra_body': {'worker-tag': requested}})
    monkeypatch.setattr(runtime_provider, 'resolve_runtime_provider', resolve)
    requests = []
    def send(client, request, **kwargs):
        requests.append(request)
        # No paid inference: the provider HTTP boundary alone is replaced.
        body = json.loads(request.content) if request.content else {}
        if body.get('stream'):
            events = [{'id': 'offline-completion', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': body['model'], 'choices': [{'index': 0,
                       'delta': {'role': 'assistant', 'content': 'Offline worker completed.'}, 'finish_reason': None}]},
                      {'id': 'offline-completion', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': body['model'], 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}]
            text = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'
            return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'}, text=text)
        return httpx.Response(200, request=request, json={
            'id': 'offline-completion', 'object': 'chat.completion', 'created': 1,
            'model': 'offline', 'choices': [{'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': 'Offline worker completed.'}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
    monkeypatch.setattr(httpx.Client, 'send', send)
    built = []
    original = run_agent.AIAgent.__init__
    def observed(agent, *args, **kwargs):
        assert kwargs["model"] == ("gpt-4.1-mini", "gpt-4.1")[len(built)], kwargs
        original(agent, *args, **kwargs)
        built.append((agent, kwargs))
    monkeypatch.setattr(run_agent.AIAgent, '__init__', observed)
    for sid in ('A', 'C'):
        parent = records[sid]['agent']
        parent.model, parent.provider = 'planner-model', 'planner-provider'
        parent.base_url, parent.api_key, parent.api_mode = 'https://planner.invalid/v1', 'planner-test-key', 'chat_completions'
        parent.reasoning_config = {'enabled': True, 'effort': 'medium'}
        parent.request_overrides = {'parent-leak': True}
        parent.enabled_toolsets, parent.disabled_toolsets = ['file'], []
        parent._active_children, parent._active_children_lock = [], threading.Lock()
        parent._delegate_depth, parent._print_fn = 0, lambda *a, **k: None
        with server._session_profile_runtime_scope(records[sid]):
            result = json.loads(delegate_tool.delegate_task(goal='Return a brief completion', parent_agent=parent,
                credentials_cfg={'provider': 'escape', 'model': 'escape', 'base_url': 'https://escape.invalid'}))
        assert result.get('results', [{}])[0].get('status') == 'completed', result
    assert resolutions == [('openrouter', 'gpt-4.1-mini'), ('openai', 'gpt-4.1')]
    assert len(built) == 2
    for (child, kwargs), sid, provider, model, effort in zip(built, ('A', 'C'), ('openrouter', 'openai'),
                                             ('gpt-4.1-mini', 'gpt-4.1'), ('high', 'medium')):
        assert child.provider == provider
        assert child.model == model
        assert kwargs['reasoning_config'] == {'enabled': True, 'effort': effort}
        assert kwargs['max_iterations'] == 19
        with SessionDB(db_path=records[sid]['agent']._session_db.db_path) as store:
            inherited = store.get_session_model_config_value(child.session_id, '_orchestration')
        assert inherited is not None and inherited['worker_model'] == model
        assert inherited['worker_reasoning_effort'] == ('high' if sid == 'A' else '')
        assert not kwargs['fallback_model']
        assert kwargs['request_overrides'] == {'extra_body': {'worker-tag': provider}}
        assert str(child._session_db.db_path) == str(records[sid]['agent']._session_db.db_path)
    assert cfg == before
    assert rpc(peer, 'orchestration.get', session_id='B')['result']['enabled'] is False
    assert len(requests) >= 2


def test_profile_scopes_isolate_colliding_durable_ids_and_leave_planner_context_unchanged(sessions):
    peer, records = sessions
    for sid in ('A', 'B'):
        records[sid]['agent'].session_id = 'same-native-key'
        records[sid]['session_key'] = 'same-native-key'
        records[sid]['agent'].model = 'unchanged-planner'
        records[sid]['agent'].system_prompt = 'byte-stable'
        records[sid]['agent'].tools = [{'function': {'name': 'read_file'}}]
        records[sid]['history'] = [{'role': 'user', 'content': 'do not rewrite'}]
    a = set_policy(peer, 'A', True, 'openai', 'worker-a', '')['result']
    assert rpc(peer, 'orchestration.get', session_id='B')['result']['enabled'] is False
    b = set_policy(peer, 'B', True, 'openrouter', 'worker-b', 'low')['result']
    assert rpc(peer, 'orchestration.get', session_id='A')['result'] == a
    assert rpc(peer, 'orchestration.get', session_id='B')['result'] == b
    assert a['stored_session_id'] == b['stored_session_id']
    for sid in ('A', 'B'):
        agent = records[sid]['agent']
        assert agent.model == 'unchanged-planner'
        assert agent.system_prompt == 'byte-stable'
        assert agent.tools == [{'function': {'name': 'read_file'}}]
        assert records[sid]['history'] == [{'role': 'user', 'content': 'do not rewrite'}]


def test_off_keeps_real_live_control_actions_and_regular_file_tool(sessions, monkeypatch, tmp_path):
    from tools import delegate_tool
    from tools.delegate_tool_registry import _register_subagent, _unregister_subagent
    from model_tools import handle_function_call

    _, records = sessions
    parent = records['B']['agent']
    updates = []
    child = SimpleNamespace(steer=lambda text: updates.append(text) or True,
                            hard_interrupt=lambda *args, **kwargs: updates.append('stopped') or True)
    _register_subagent(dict(subagent_id='off-cleanup', agent=child, owner_agent_session_id=parent.session_id,
                            goal='cleanup child', model='child-model', status='running'))
    try:
        assert json.loads(delegate_tool.delegate_task(action='list', parent_agent=parent))['subagents'][0]['subagent_id'] == 'off-cleanup'
        assert json.loads(delegate_tool.delegate_task(action='steer', subagent_id='off-cleanup', message='finish safely',
                                                     parent_agent=parent))['status'] == 'queued'
        assert json.loads(delegate_tool.delegate_task(action='stop', subagent_id='off-cleanup',
                                                     parent_agent=parent))['status'] == 'interrupt_requested'
        assert updates == ['finish safely', 'stopped']
        monkeypatch.setenv('HERMES_RUNTIME_DIR', str(tmp_path / 'runtime'))
        path = tmp_path / 'normal-tool.txt'
        path.write_text('regular tools remain available', encoding='utf-8')
        assert 'regular tools remain available' in handle_function_call('read_file', {'path': str(path)}, task_id=parent.session_id)
    finally:
        _unregister_subagent('off-cleanup', agent=child)


def test_get_never_falls_back_when_live_record_has_no_durable_identity(sessions):
    peer, records = sessions
    records['A']['agent'].session_id = ''
    records['A']['session_key'] = ''
    assert rpc(peer, 'orchestration.get', session_id='A').get('error', {}).get('code') == 4001


@pytest.mark.parametrize('params', [None, {}, {'session_id': None}, {'session_id': 7},
                                       {'session_id': 'A', 'profile': 'foreign'}])
def test_get_rejects_missing_or_malformed_wire_scope(sessions, params):
    peer, _ = sessions
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'orchestration.get'}
    if params is not None:
        request['params'] = params
    assert server.dispatch(request, peer)['error']['code'] == 4000


@pytest.mark.parametrize('enabled', ['true', 1, None])
def test_set_does_not_coerce_enabled(sessions, enabled):
    peer, _ = sessions
    assert set_policy(peer, 'A', enabled)['error']['code'] == 4000


def test_get_storage_error_and_malformed_outer_json_fail_loudly(sessions, monkeypatch):
    peer, records = sessions
    db = records['A']['agent']._session_db
    db.create_session('stored-A', source='desktop')
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET model_config = ? WHERE id = ?",
                                              ('{broken-json', 'stored-A')))
    assert rpc(peer, 'orchestration.get', session_id='A')['error']['code'] == 4000
    def unavailable(*args):
        raise sqlite3.OperationalError('forced offline DB')
    monkeypatch.setattr(db, 'get_session', unavailable)
    assert rpc(peer, 'orchestration.get', session_id='A')['error']['code'] == 5033


def test_lazy_no_agent_session_uses_native_profile_store(sessions):
    peer, records = sessions
    db = records['A']['agent']._session_db
    records['A']['agent'] = None
    assert rpc(peer, 'orchestration.get', session_id='A')['result']['enabled'] is False
    response = set_policy(peer, 'A')['result']
    assert db.get_session_model_config_value('stored-A', '_orchestration')['enabled'] is True
    assert response['stored_session_id'] == 'stored-A'


def test_set_omitted_effort_inherits_and_does_not_error(sessions):
    peer, _ = sessions
    response = rpc(peer, 'orchestration.set', session_id='A', enabled=True,
                   worker_provider='openai', worker_model='worker')
    assert response.get('result', {}).get('worker_reasoning_effort') == '', response


def test_get_live_lazy_session_defaults_off_without_creating_row(sessions):
    peer, records = sessions
    result = rpc(peer, 'orchestration.get', session_id='A')
    assert result.get('result') == dict(version=1, supported=True, scope='session', session_id='A',
                                      stored_session_id='stored-A', enabled=False, worker_provider='',
                                      worker_model='', worker_reasoning_effort='')
    assert records['A']['agent']._session_db.get_session('stored-A') is None
    for sid in ('', 'stale', 'stored-A'):
        assert rpc(peer, 'orchestration.get', session_id=sid)['error']['code'] == 4001
    stranger = StdioTransport(lambda: io.StringIO(), threading.Lock())
    assert rpc(stranger, 'orchestration.get', session_id='A')['error']['code'] == 4001

@pytest.mark.parametrize('entrypoint', ['public', 'constructor'])
def test_review_s1_off_rejects_at_shared_boundary(sessions, monkeypatch, entrypoint):
    from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleService, SubagentLifecycleError
    from tools import delegate_tool as dt
    peer, records = sessions
    assert 'result' in set_policy(peer, 'A', False, '', '', '')
    parent = records['A']['agent']
    monkeypatch.setattr(dt, '_load_config', lambda: pytest.fail('OFF reached config setup'))
    if entrypoint == 'public':
        with pytest.raises(SubagentLifecycleError, match='orchestration'):
            SubagentLifecycleService(lambda: parent).launch(SubagentLaunchRequest(goal='blocked'))
    else:
        with pytest.raises(ValueError, match='orchestration'):
            dt._build_child_agent(0, 'blocked', None, [], 'escape', 1, 1, parent)


def _review_offline_parent(parent):
    parent.model, parent.provider = 'planner-model', 'planner-provider'
    parent.base_url, parent.api_key, parent.api_mode = 'https://planner.invalid/v1', 'offline-key', 'chat_completions'
    parent.reasoning_config = {'enabled': True, 'effort': 'medium'}
    parent.request_overrides = {'parent-leak': True}
    parent.enabled_toolsets, parent.disabled_toolsets = ['file'], []
    parent._active_children, parent._active_children_lock = [], threading.Lock()
    parent._delegate_depth, parent._print_fn = 0, lambda *a, **k: None


def _review_offline_provider(monkeypatch):
    import httpx
    from hermes_cli import runtime_provider
    resolutions = []
    def resolve(requested, target_model):
        resolutions.append((requested, target_model))
        return dict(provider=requested, model=target_model, base_url='https://worker.invalid/v1',
                    api_key='offline-key', api_mode='chat_completions', request_overrides={})
    def send(client, request, **kwargs):
        body = json.loads(request.content) if request.content else {}
        response = dict(id='offline', object='chat.completion', created=1, model=body.get('model', 'offline'),
                        choices=[dict(index=0, finish_reason='stop', message=dict(role='assistant', content='done'))],
                        usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        if body.get('stream'):
            event = dict(id='offline', object='chat.completion.chunk', created=1, model=body['model'],
                         choices=[dict(index=0, finish_reason='stop', delta=dict(role='assistant', content='done'))])
            return httpx.Response(200, request=request, headers={'content-type':'text/event-stream'},
                                  text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
        return httpx.Response(200, request=request, json=response)
    monkeypatch.setattr(runtime_provider, 'resolve_runtime_provider', resolve)
    monkeypatch.setattr(httpx.Client, 'send', send)
    return resolutions


@pytest.mark.parametrize('entrypoint', ['public', 'constructor'])
def test_review_s1_on_freezes_selected_route(sessions, monkeypatch, entrypoint):
    from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleService, SubagentState
    from tools import delegate_tool as dt
    peer, records = sessions
    assert 'result' in set_policy(peer, 'A', True, 'openai', 'gpt-4.1-mini', 'high')
    parent = records['A']['agent']
    _review_offline_parent(parent)
    resolutions = _review_offline_provider(monkeypatch)
    monkeypatch.setattr(dt, '_load_config', lambda: dict(provider='escape', model='escape', reasoning_effort='low',
        base_url='https://escape.invalid', api_key='escape-key', request_overrides={'leak': True}, max_spawn_depth=1))
    with server._session_profile_runtime_scope(records['A']):
        if entrypoint == 'public':
            service = SubagentLifecycleService(lambda: parent)
            handle = service.launch(SubagentLaunchRequest(goal='offline completion', model='escape'))
            child = service._record(handle).agent
            terminal = service.wait(handle, timeout_seconds=20)
            assert terminal.state == SubagentState.SUCCEEDED
        else:
            child = dt._build_child_agent(0, 'offline completion', None, ['file'], 'escape', 2, 1, parent,
                override_provider='escape', override_base_url='https://escape.invalid', override_api_key='escape-key',
                override_api_mode='anthropic_messages', override_request_overrides={'leak': True},
                routing_cfg={'provider': 'escape', 'model': 'escape'})
            result = dt._run_single_child(0, 'offline completion', child, parent)
            assert result['status'] == 'completed'
    assert (child.provider, child.model) == ('openai', 'gpt-4.1-mini')
    assert child.reasoning_config == {'enabled': True, 'effort': 'high'}
    assert child.base_url == 'https://worker.invalid/v1'
    assert child.request_overrides == {}
    assert resolutions == [('openai', 'gpt-4.1-mini')]
    with SessionDB(db_path=parent._session_db.db_path) as db:
        assert db.get_session_model_config_value(child.session_id, '_orchestration')['worker_model'] == 'gpt-4.1-mini'

@pytest.mark.parametrize('composer', [None, {'model': 'profile-default', 'provider': 'openai'}])
def test_review_s2_runtime_switch_merges_latest_off_policy(sessions, monkeypatch, composer):
    peer, records = sessions
    record = records['A']
    db = record['agent']._session_db
    assert 'result' in set_policy(peer, 'A')
    db.patch_session_model_config('stored-A', dict(provider='old', base_url='https://old.invalid', api_mode='old',
        reasoning_config={'effort': 'old'}, service_tier='fast', composer_override_profile={'old': True},
        follow_profile_config=True, room_plumbing=True, unrelated={'keep': True}))
    read_ready, resume_write = threading.Event(), threading.Event()
    original = server._runtime_model_config
    def interleave(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        read_ready.set()
        assert resume_write.wait(10), 'OFF did not finish'
        return snapshot
    monkeypatch.setattr(server, '_runtime_model_config', interleave)
    errors = []
    def runtime_switch():
        try:
            with SessionDB(db_path=db.db_path) as runtime_db:
                agent = SimpleNamespace(_session_db=runtime_db, model='new-planner', provider='', base_url='',
                                        api_mode='', reasoning_config={}, service_tier=None)
                runtime_record = {**record, 'agent': agent, 'composer_override_profile': composer,
                                  'create_service_tier_override': ''}
                server._persist_live_session_runtime(runtime_record)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=runtime_switch)
    thread.start()
    try:
        assert read_ready.wait(10), 'runtime did not reach snapshot'
        disabled = set_policy(peer, 'A', False, '', '', '')
        assert disabled['result']['enabled'] is False
        db.patch_session_model_config('stored-A', {'new-concurrent-key': 'preserve'})
    finally:
        resume_write.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    with SessionDB(db_path=db.db_path) as reopened:
        row = reopened.get_session('stored-A')
        config = json.loads(row['model_config'])
        assert config['_orchestration']['enabled'] is False
        assert row['model'] == config['model'] == 'new-planner'
        assert config['new-concurrent-key'] == 'preserve'
        assert config['reasoning_config'] == {} and config['service_tier'] == 'normal'
        assert all(k not in config for k in ('provider', 'base_url', 'api_mode'))
        assert config['follow_profile_config'] and config['room_plumbing']
        assert config['unrelated'] == {'keep': True}
        assert config.get('composer_override_profile') == composer
    assert rpc(peer, 'orchestration.get', session_id='A')['result']['enabled'] is False


@pytest.mark.parametrize('enabled', [True, False])
def test_review_l1_publication_uses_current_policy_in_transaction(sessions, monkeypatch, enabled):
    import copy
    from tools import delegate_tool as dt
    peer, records = sessions
    record = records['A']
    db = record['agent']._session_db
    assert 'result' in set_policy(peer, 'A', True, 'openai', 'gpt-4.1-mini', 'high')
    stale = {'model': 'planner', '_orchestration': db.get_session_model_config_value('stored-A', '_orchestration')}
    record['agent']._session_init_model_config = stale
    before = copy.deepcopy(stale)
    ready, proceed = threading.Event(), threading.Event()
    errors = []
    def publish():
        try:
            with SessionDB(db_path=db.db_path) as publishing_db:
                original = publishing_db._execute_transcript_write
                def interleave(fn, messages):
                    ready.set()
                    assert proceed.wait(10)
                    return original(fn, messages)
                publishing_db._execute_transcript_write = interleave
                publishing_db.publish_compression_child(parent_session_id='stored-A', child_session_id='continuation',
                    source='desktop', model='planner', model_config=record['agent']._session_init_model_config,
                    messages=[{'role':'user','content':'handoff'}], require_compression_lease=False)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert ready.wait(10)
        changed = set_policy(peer, 'A', enabled, 'openrouter', 'gpt-4.1', 'medium')['result']
    finally:
        proceed.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    assert stale == before
    with SessionDB(db_path=db.db_path) as reopened:
        parent = records['A']['agent']
        parent._session_db = reopened
        parent.session_id = record['session_key'] = 'continuation'
        restored = rpc(peer, 'orchestration.get', session_id='A')['result']
        assert {k:restored[k] for k in before['_orchestration']} == {k:changed[k] for k in before['_orchestration']}
        _review_offline_parent(parent)
        if enabled:
            resolutions = _review_offline_provider(monkeypatch)
            with server._session_profile_runtime_scope(record):
                child = dt._build_child_agent(0, 'offline', None, ['file'], 'escape', 2, 1, parent)
                assert dt._run_single_child(0, 'offline', child, parent)['status'] == 'completed'
            assert (child.provider, child.model) == ('openrouter', 'gpt-4.1')
            assert resolutions == [('openrouter', 'gpt-4.1')]
        else:
            monkeypatch.setattr(dt, '_load_config', lambda: pytest.fail('compressed OFF reached config'))
            with pytest.raises(ValueError, match='orchestration'):
                dt._build_child_agent(0, 'blocked', None, [], None, 1, 1, parent)
        # Future RPCs on the continuation are independent of previous/init policy.
        assert 'result' in set_policy(peer, 'A', False, 'openai', 'next-worker', '')
        assert reopened.get_session_model_config_value('stored-A', '_orchestration')['worker_model'] == 'gpt-4.1'
        assert stale == before


@pytest.mark.parametrize('damaged', ['policy', 'outer-json'])
def test_review_l1_damaged_durable_policy_rolls_back_publication(sessions, damaged):
    peer, records = sessions
    db = records['A']['agent']._session_db
    assert 'result' in set_policy(peer, 'A')
    if damaged == 'policy':
        db.patch_session_model_config('stored-A', {'_orchestration': False})
    else:
        db._execute_write(lambda conn: conn.execute('UPDATE sessions SET model_config = ? WHERE id = ?',
                                                   ('{broken', 'stored-A')))
    with pytest.raises(ValueError, match='invalid'):
        db.publish_compression_child(parent_session_id='stored-A', child_session_id='damaged-child', source='desktop',
            model_config={'_orchestration': {'stale': True}}, messages=[{'role':'user','content':'handoff'}],
            require_compression_lease=False)
    assert db.get_session('damaged-child') is None
    assert db.get_session('stored-A')['ended_at'] is None


@pytest.mark.parametrize('tier', ['', 'priority'])
def test_review_l4_lazy_choice_preserves_native_planner_projection(sessions, monkeypatch, tier):
    peer, records = sessions
    homes = {}
    expected = {}
    for sid in ('A', 'B'):
        homes[sid] = records[sid]['agent']._session_db.db_path
        record = records[sid]
        record.update(agent=None, session_key='same-lazy-id',
            model_override={'model':'planner-'+sid, 'provider':'openai', 'base_url':'https://api.openai.com/v1',
                            'api_mode':'chat_completions'},
            create_reasoning_override={'enabled':True,'effort':'medium'},
            create_service_tier_override=tier,
            composer_override_profile={'model':'profile-'+sid, 'provider':'openai'})
        with server._session_profile_runtime_scope(record):
            expected[sid] = server._workdir_row_model_config(record)
    monkeypatch.setattr(server, '_get_db', lambda: pytest.fail('foreign lazy session used launch DB'))
    for sid in ('A', 'B'):
        assert 'result' in set_policy(peer, sid, True, 'openrouter', 'worker-'+sid, '')
    for sid in ('A', 'B'):
        with SessionDB(db_path=homes[sid]) as reopened:
            row = reopened.get_session('same-lazy-id')
            model, projection = expected[sid]
            assert row['model'] == model
            config = json.loads(row['model_config'])
            assert {k:config[k] for k in projection} == projection
            assert config['_orchestration']['worker_model'] == 'worker-'+sid
            with server._session_profile_runtime_scope(records[sid]):
                restored = server._stored_session_runtime_overrides(row)
            assert restored['model_override'] == records[sid]['model_override']
            assert restored['reasoning_config_override'] == records[sid]['create_reasoning_override']
            assert restored['service_tier_override'] == tier
            assert row['source'] == 'desktop'
            assert row['profile_name'] is not None
    # Flags remain projection-owned too (native resume deliberately follows the
    # profile for room plumbing, rather than restoring an explicit route).
    records['C'].update(agent=None, room_plumbing=True, follow_profile_config=True,
                         model_override={'model':'room-planner','provider':'openai'})
    assert 'result' in set_policy(peer, 'C')
    with SessionDB(db_path=homes['A']) as reopened:
        flags = json.loads(reopened.get_session('stored-C')['model_config'])
        assert flags['room_plumbing'] and flags['follow_profile_config']
        assert flags['model'] == 'room-planner'


def test_r2_native_rotation_postcommit_off_rpc_cannot_ack_closed_parent(sessions, monkeypatch):
    from agent import conversation_compression as cc
    peer, records = sessions
    record = records['A']; agent = record['agent']; db = agent._session_db
    assert 'result' in set_policy(peer, 'A')
    agent.model = 'planner'; agent._session_init_model_config = {'model': 'planner'}
    agent._flush_messages_to_session_db = lambda *a, **kw: None
    monkeypatch.setattr(cc, '_carry_session_state_to_child', lambda *a: None)
    monkeypatch.setattr(cc, '_rebind_session_context', lambda *a: None)
    original = db.publish_compression_child
    observed = []
    def publish(**kwargs):
        original(**kwargs)
        assert agent.session_id == 'stored-A'
        observed.append(set_policy(peer, 'A', False, '', '', ''))
    monkeypatch.setattr(db, 'publish_compression_child', publish)
    cc._publish_rotated_compaction(agent, [], [{'role': 'user', 'content': 'handoff'}],
        new_system_prompt='', lease=SimpleNamespace(holder=None, ttl=300, watermark=None),
        old_session_id='stored-A', compressed_user_turn_outcome='already_present')
    assert 'error' in observed[0], 'OFF was falsely acknowledged on the superseded compression parent'
    assert observed[0]['error']['code'] == 4001, 'Superseded session must return the retry/identity category, not storage unavailable'
    assert 'session changed' in observed[0]['error']['message'].lower()
    assert db.get_session_model_config_value('stored-A', '_orchestration')['enabled'] is True
    assert db.get_session_model_config_value(agent.session_id, '_orchestration')['enabled'] is True
    assert 'result' in set_policy(peer, 'A', False, '', '', '')
    assert db.get_session_model_config_value(agent.session_id, '_orchestration')['enabled'] is False


@pytest.mark.parametrize('when', ['transaction-entry', 'after-sql'])
def test_r2_durable_identity_change_fences_rpc_write_and_ack(sessions, monkeypatch, when):
    peer, records = sessions
    record = records['A']; agent = record['agent']; db = agent._session_db
    assert 'result' in set_policy(peer, 'A')
    before = db.get_session_model_config_value('stored-A', '_orchestration')
    original = db._execute_write
    def interleave(fn, *a, **kw):
        def wrapped(conn):
            if when == 'transaction-entry':
                agent.session_id = 'new-durable-id'
            result = fn(conn)
            if when == 'after-sql':
                agent.session_id = 'new-durable-id'
            return result
        return original(wrapped, *a, **kw)
    monkeypatch.setattr(db, '_execute_write', interleave)
    response = set_policy(peer, 'A', False, '', '', '')
    assert 'error' in response, 'RPC ACK used a stale durable agent identity'
    assert response['error']['code'] == 4001, 'Durable identity changes must use the session-changed retry category'
    if when == 'transaction-entry':
        assert db.get_session_model_config_value('stored-A', '_orchestration') == before


@pytest.mark.parametrize('surface', ['gateway', 'acp'])
def test_r3_native_whole_row_metadata_writers_preserve_acknowledged_off(sessions, monkeypatch, surface):
    peer, records = sessions
    agent = records['A']['agent']; db = agent._session_db
    assert 'result' in set_policy(peer, 'A')
    agent.model = 'new-planner'; agent.provider = 'openai'
    original = db.update_session_meta
    observed = []
    def interleave(sid, raw, model=None):
        # The real surface has captured metadata, but has not entered its write transaction.
        observed.append(set_policy(peer, 'A', False, '', '', ''))
        return original(sid, raw, model)
    monkeypatch.setattr(db, 'update_session_meta', interleave)
    if surface == 'gateway':
        from gateway.run_turn import GatewayTurnMixin
        runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))
        GatewayTurnMixin._sync_session_model_from_agent(runner, 'stored-A', agent)
    else:
        from acp_adapter.session import SessionManager, SessionState
        manager = SessionManager(agent_factory=lambda **kw: agent, db=db)
        monkeypatch.setattr(manager, '_schedule_git_metadata', lambda *a: None)
        state = SessionState(session_id='stored-A', agent=agent, model='new-planner', cwd='workspace', history=[])
        manager._persist(state)
    assert len(observed) == 1 and 'result' in observed[0], observed
    config = json.loads(db.get_session('stored-A')['model_config'])
    assert config['_orchestration']['enabled'] is False, 'native metadata writer undid acknowledged OFF'
    assert db.get_session('stored-A')['model'] == 'new-planner'
    if surface == 'gateway':
        assert config['gateway_runtime']['provider'] == 'openai'
    else:
        assert config['cwd'] == 'workspace' and config['provider'] == 'openai'


@pytest.mark.parametrize('writer', ['whole-row', 'runtime-patch', 'create-upsert'])
def test_r3_non_policy_writer_cannot_introduce_policy_into_legacy_absence(sessions, writer):
    from agent.session_orchestration import default_policy, policy_for_spawn
    peer, records = sessions
    agent = records['A']['agent']; agent.platform = 'cli'; db = agent._session_db
    db.create_session('stored-A', source='cli')
    incoming = {'model': 'planner', '_orchestration': default_policy()}
    if writer == 'whole-row':
        db.update_session_meta('stored-A', json.dumps(incoming), 'planner')
    elif writer == 'runtime-patch':
        db.patch_session_runtime_config('stored-A', incoming, 'planner')
    else:
        db.create_session('stored-A', source='cli', model_config=incoming)
    assert db.get_session_model_config_value('stored-A', '_orchestration') is None
    assert policy_for_spawn(agent) is None, 'legacy no-policy behavior changed'


@pytest.mark.parametrize('raw', ['{broken', '[]', '{"_orchestration": false}'])
@pytest.mark.parametrize('writer', ['whole-row', 'runtime-patch', 'model-switch'])
def test_r3_metadata_write_never_repairs_corrupt_policy_to_unrestricted_spawn(sessions, raw, writer):
    from agent.session_orchestration import policy_for_spawn
    peer, records = sessions
    agent = records['A']['agent']; agent.platform = 'cli'; db = agent._session_db
    db.create_session('stored-A', source='cli')
    db._execute_write(lambda conn: conn.execute('UPDATE sessions SET model_config=? WHERE id=?', (raw, 'stored-A')))
    try:
        if writer == 'whole-row':
            db.update_session_meta('stored-A', '{"cwd":"new"}', 'new-planner')
        elif writer == 'runtime-patch':
            db.patch_session_runtime_config('stored-A', {'base_url': None}, 'new-planner')
        else:
            db.update_session_model('stored-A', 'new-planner', 'openai')
    except ValueError:
        pass
    with pytest.raises(ValueError, match='unavailable or invalid'):
        policy_for_spawn(agent)


def test_r3_whole_row_replaces_metadata_but_preserves_current_malformed_policy(sessions):
    peer, records = sessions
    db = records['A']['agent']._session_db
    db.create_session('stored-A', source='cli', model='old', model_config={'_orchestration': False, 'old-key': 1})
    db.update_session_meta('stored-A', '{"new-key": null, "_orchestration": {"stale": true}}', model=None)
    row = db.get_session('stored-A')
    assert row['model'] == 'old'
    assert json.loads(row['model_config']) == {'new-key': None, '_orchestration': False}


@pytest.mark.parametrize('saved', ['off', 'absent'])
def test_r3_native_in_place_compaction_metadata_cannot_resurrect_or_introduce_policy(sessions, saved):
    from agent.session_orchestration import default_policy
    peer, records = sessions
    db = records['A']['agent']._session_db
    if saved == 'off':
        assert 'result' in set_policy(peer, 'A', False, '', '', '')
    else:
        db.create_session('stored-A', source='cli')
    stale = {**default_policy(), 'enabled': True, 'worker_provider': 'openai', 'worker_model': 'worker'}
    db.append_message('stored-A', 'user', 'old')
    db.archive_and_compact('stored-A', [{'role':'user','content':'handoff'}],
                           model_config_patch={'_orchestration': stale, 'compression-key': 'keep', 'old-key': None})
    config = json.loads(db.get_session('stored-A')['model_config'])
    assert config['compression-key'] == 'keep' and 'old-key' not in config
    if saved == 'off':
        assert config['_orchestration']['enabled'] is False
    else:
        assert '_orchestration' not in config


def test_r2_sqlite_policy_fence_does_not_invert_registry_lock_order(sessions, monkeypatch):
    peer, records = sessions
    db = records['A']['agent']._session_db
    assert 'result' in set_policy(peer, 'A')
    real_lock = server._sessions_lock
    writing = [False]
    class RegistryLock:
        def __enter__(self):
            assert not writing[0], 'registry lock acquired inside SQLite transaction: lock-order inversion'
            return real_lock.__enter__()
        def __exit__(self, *args):
            return real_lock.__exit__(*args)
    monkeypatch.setattr(server, '_sessions_lock', RegistryLock())
    original = db._execute_write
    def write(fn, *args, **kwargs):
        def guarded(conn):
            writing[0] = True
            try:
                return fn(conn)
            finally:
                writing[0] = False
        return original(guarded, *args, **kwargs)
    monkeypatch.setattr(db, '_execute_write', write)
    response = set_policy(peer, 'A', False, '', '', '')
    assert 'result' in response, response
    assert response['result']['enabled'] is False


