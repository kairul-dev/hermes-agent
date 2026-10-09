"""Read-only native focused projection through real dispatcher and owning databases."""
import io
import threading
from types import SimpleNamespace

import pytest
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import StdioTransport


def rpc(peer, sid):
    return server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'session.info.get',
                            'params': {'session_id': sid}}, peer)


@pytest.fixture
def owned(tmp_path, monkeypatch):
    records = {}
    peers = {}
    dbs = []
    for sid in ('A', 'B'):
        home = tmp_path / sid
        home.mkdir()
        db = SessionDB(db_path=home / 'state.db')
        dbs.append(db)
        peer = StdioTransport(lambda: io.StringIO(), threading.Lock())
        peers[sid] = peer
        agent = SimpleNamespace(session_id='stored-' + sid, _session_db=db,
                                model='planner-' + sid, provider='openai',
                                reasoning_config={'effort': 'high'}, _cached_system_prompt='SECRET')
        record = dict(transport=peer, agent=agent, session_key=agent.session_id,
                      profile_home=str(home), history=[], running=False)
        records[sid] = record
        monkeypatch.setitem(server._sessions, sid, record)
    yield peers, records
    for db in dbs:
        db.close()


def test_live_projection_exact_owned_tuple_without_writes_or_secret_access(owned, monkeypatch):
    peers, records = owned
    def forbidden(*a, **kw):
        pytest.fail('read getter reached state write, turn setup, or full secret projection')
    monkeypatch.setattr(server, '_make_agent', forbidden)
    monkeypatch.setattr(server, '_persist_live_session_runtime', forbidden)
    monkeypatch.setattr(server, '_probe_credentials', forbidden)
    for sid, record in records.items():
        db = record['agent']._session_db
        before = db._conn.total_changes
        got = rpc(peers[sid], sid)
        assert 'result' in got, got
        result = got['result']
        assert result['session_id'] == sid
        assert result['stored_session_id'] == 'stored-' + sid
        assert result['info'] == dict(model='planner-' + sid, provider='openai',
                                      reasoning_effort='high', reasoning_effort_wire='high',
                                      model_available=True, provider_available=True)
        assert db.get_session('stored-' + sid) is None
        assert db._conn.total_changes == before


def test_lazy_own_create_tuple_and_reasoning_without_materialization(owned, monkeypatch):
    peers, records = owned
    record = records['B']
    db = record['agent']._session_db
    record['agent'] = None
    record['model_override'] = {'model': 'shared-model', 'provider': 'custom:workspace'}
    record['create_reasoning_override'] = {'enabled': False}
    monkeypatch.setattr(server, '_session_default_model', lambda *a: pytest.fail('global model fallback'))
    before = db._conn.total_changes
    result = rpc(peers['B'], 'B')['result']['info']
    assert result['model'] == 'shared-model'
    assert result['provider'] == 'custom:workspace'
    assert result['reasoning_effort'] == 'none'
    assert result['model_available'] and result['provider_available']
    assert db.get_session('stored-B') is None
    assert db._conn.total_changes == before


@pytest.mark.parametrize('row', [True, False])
def test_agentless_persisted_native_row_or_explicit_unavailable_is_read_only(owned, monkeypatch, row):
    peers, records = owned
    record = records['B']
    db = record['agent']._session_db
    if row:
        db.create_session('stored-B', source='desktop', model='stored-model',
                          model_config={'provider': 'custom:workspace', 'reasoning_config': {'effort': 'low'}})
    before = db._conn.total_changes
    record['agent'] = None
    monkeypatch.setattr(server, '_session_default_model', lambda *a: pytest.fail('global fallback'))
    got = rpc(peers['B'], 'B')['result']['info']
    assert got['model'] == ('stored-model' if row else '')
    assert got['provider'] == ('custom:workspace' if row else '')
    assert got['reasoning_effort'] == ('low' if row else '')
    assert got['model_available'] is row and got['provider_available'] is row
    assert db._conn.total_changes == before
    assert not any(k in got for k in ('base_url', 'api_key', 'system_prompt'))


@pytest.mark.parametrize('sid', ['', 'stale', 'stored-A', 123, None, False])
def test_missing_stale_and_non_runtime_ids_reject(owned, sid):
    peers, records = owned
    assert 'error' in rpc(peers['A'], sid)


def test_foreign_or_detached_transport_has_no_authority(owned):
    peers, records = owned
    assert rpc(peers['A'], 'B')['error']['code'] == 4001
    records['A']['transport'] = peers['B']
    assert rpc(peers['A'], 'A')['error']['code'] == 4001


def test_pending_mirror_and_session_reasoning_precedence_match_native_projection(owned):
    peers, records = owned
    record = records['B']
    record['_metadata_mirror'] = {'model': 'mirrored', 'provider': 'custom:mirror', 'system_prompt': 'SECRET'}
    record['create_reasoning_override'] = {'effort': 'low'}
    info = rpc(peers['B'], 'B')['result']['info']
    assert (info['model'], info['provider'], info['reasoning_effort']) == ('mirrored', 'custom:mirror', 'low')
    record['pending_model_switch'] = {'display_model': 'shared-model', 'display_provider': 'custom:workspace'}
    info = rpc(peers['B'], 'B')['result']['info']
    assert (info['model'], info['provider']) == ('shared-model', 'custom:workspace')
    assert info['reasoning_effort'] == 'low'


@pytest.mark.parametrize('lazy', [True, False])
def test_bare_custom_recovers_name_from_own_profile_without_provider_setup(owned, monkeypatch, lazy):
    peers, records = owned
    record = records['B']
    from pathlib import Path
    (Path(record['profile_home']) / 'config.yaml').write_text(
        'custom_providers:\n  - name: workspace\n    base_url: https://offline.invalid/v1\n    model: shared-model\n', encoding='utf-8')
    agent = record['agent']
    agent.model, agent.provider, agent.base_url = 'shared-model', 'custom', 'https://offline.invalid/v1'
    if lazy:
        record['agent'] = None
        record['model_override'] = {'model': agent.model, 'provider': agent.provider, 'base_url': agent.base_url}
    from hermes_cli import runtime_provider
    monkeypatch.setattr(runtime_provider, 'resolve_runtime_provider', lambda *a, **kw: pytest.fail('credential setup'))
    got = rpc(peers['B'], 'B')['result']['info']
    assert got['model'] == 'shared-model'
    assert got['provider'] == 'custom:workspace'
    assert 'base_url' not in got


@pytest.mark.parametrize('kind', ['live', 'lazy', 'row'])
def test_bare_custom_with_overlapping_models_cannot_infer_first_catalog_provider(owned, kind):
    peers, records = owned
    from pathlib import Path
    record = records['B']
    (Path(record['profile_home']) / 'config.yaml').write_text(
        'custom_providers:\n  - name: first\n    base_url: https://first.invalid/v1\n    model: shared-model\n'
        '  - name: second\n    base_url: https://second.invalid/v1\n    model: shared-model\n', encoding='utf-8')
    agent = record['agent']
    agent.model, agent.provider = 'shared-model', 'custom'
    if kind == 'lazy':
        record['agent'] = None
        record['model_override'] = {'model': agent.model, 'provider': 'custom'}
    if kind == 'row':
        agent._session_db.create_session('stored-B', source='desktop', model='shared-model', model_config={'provider': 'custom'})
        record['agent'] = None
    result = rpc(peers['B'], 'B')['result']['info']
    assert result['model'] == 'shared-model'
    assert result['provider'] == ''
    assert result['provider_available'] is False


@pytest.mark.parametrize('change', ['remove', 'reuse', 'durable-rotation'])
def test_inflight_projection_rechecks_exact_record_and_durable_identity(owned, monkeypatch, change):
    peers, records = owned
    original = server._session_model_info
    def project(agent, session, **kwargs):
        result = original(agent, session, **kwargs)
        if change == 'remove':
            server._sessions.pop('B')
        elif change == 'reuse':
            server._sessions['B'] = dict(session)
        else:
            session['session_key'] = 'rotated-stored-B'
        return result
    monkeypatch.setattr(server, '_session_model_info', project)
    response = rpc(peers['B'], 'B')
    assert response.get('error', {}).get('code') == 4001, response


@pytest.mark.parametrize('foreign', [True, False])
def test_getter_never_hydrates_profile_secrets_before_or_after_authorization(owned, monkeypatch, foreign):
    from hermes_cli import env_loader
    peers, records = owned
    calls = []
    monkeypatch.setattr(env_loader, 'hydrate_profile_secret_sources', lambda *a, **kw: calls.append(a))
    response = rpc(peers['A'] if foreign else peers['B'], 'B')
    assert response.get('error', {}).get('code') == 4001 if foreign else 'result' in response
    assert calls == [], 'read-only getter hydrated external profile secrets'


def test_getter_custom_endpoint_projection_uses_only_own_raw_config(owned, monkeypatch):
    from pathlib import Path
    from hermes_cli import env_loader, config
    peers, records = owned
    record = records['B']
    (Path(record['profile_home']) / 'config.yaml').write_text(
        'custom_providers:\n  - name: workspace\n    base_url: https://offline.invalid/v1\n    api_key: ${SECRET}\n', encoding='utf-8')
    record['agent'].provider, record['agent'].base_url = 'custom', 'https://offline.invalid/v1'
    calls = []
    monkeypatch.setattr(env_loader, 'hydrate_profile_secret_sources', lambda *a, **kw: calls.append('external'))
    monkeypatch.setattr(config, 'load_config', lambda *a, **kw: calls.append('expanded-config') or {})
    before = set(Path(record['profile_home']).rglob('*'))
    result = rpc(peers['B'], 'B')
    assert result.get('result', {}).get('info', {}).get('provider') == 'custom:workspace', result
    assert calls == [], 'getter reached secret/expanded config machinery'
    assert set(Path(record['profile_home']).rglob('*')) == before


def test_r4_getter_uses_native_agent_id_during_rotation_before_host_key_sync(owned):
    peers, records = owned
    record = records['B']; agent = record['agent']; db = agent._session_db
    db.create_session('stored-B', source='desktop', model='planner-B')
    db.publish_compression_child(parent_session_id='stored-B', child_session_id='child-B', source='desktop',
        messages=[{'role':'user','content':'handoff'}], require_compression_lease=False)
    agent.session_id = 'child-B'
    assert record['session_key'] == 'stored-B'
    got = rpc(peers['B'], 'B')['result']
    orchestration = server.dispatch({'jsonrpc':'2.0','id':2,'method':'orchestration.get',
        'params': {'session_id':'B'}}, peers['B'])['result']
    assert got['stored_session_id'] == orchestration['stored_session_id'] == 'child-B'


def test_r4_native_identity_change_during_projection_refuses_ack(owned, monkeypatch):
    peers, records = owned
    original = server._session_model_info
    def project(agent, session, **kw):
        result = original(agent, session, **kw)
        session['agent'].session_id = 'new-child'
        return result
    monkeypatch.setattr(server, '_session_model_info', project)
    response = rpc(peers['B'], 'B')
    assert response.get('error', {}).get('code') == 4001, response


@pytest.mark.parametrize('kind', ['live', 'uncertain', 'endpoint', 'pending', 'mirror', 'lazy', 'lazy-known', 'row'])
def test_r5_native_event_nested_projection_equals_getter_exact_six_fields(owned, monkeypatch, kind):
    from pathlib import Path
    from hermes_cli import banner
    peers, records = owned
    record = records['B']; agent = record['agent']; db = agent._session_db
    agent.model, agent.provider = 'shared-model', 'custom:second'
    (Path(record['profile_home']) / 'config.yaml').write_text(
        'custom_providers:\n  - name: first\n    base_url: https://first.invalid/v1\n    model: shared-model\n'
        '  - name: second\n    base_url: https://second.invalid/v1\n    model: shared-model\n', encoding='utf-8')
    if kind in ('uncertain', 'endpoint', 'row'):
        agent.provider = 'custom'
    if kind == 'endpoint':
        agent.base_url = 'https://second.invalid/v1'
    if kind == 'pending':
        record['pending_model_switch'] = {'display_model':'shared-model', 'display_provider':'custom'}
    if kind == 'mirror':
        record['_metadata_mirror'] = {'model':'shared-model', 'provider':'custom', 'system_prompt':'SECRET'}
    if kind in ('lazy', 'lazy-known'):
        record['agent'] = None
        record['model_override'] = {'model':'shared-model', 'provider':'custom:second' if kind == 'lazy-known' else 'custom'}
    if kind == 'row':
        db.create_session('stored-B', source='desktop', model='shared-model', model_config={'provider':'custom'})
        record['agent'] = None
    monkeypatch.setattr(server, '_display_cfg', lambda: {})
    monkeypatch.setattr(server, '_display_session_cwd', lambda *a: record['profile_home'])
    monkeypatch.setattr(server, '_project_info_for_cwd', lambda *a: {})
    monkeypatch.setattr(server.git_probe, 'branch', lambda *a: '')
    monkeypatch.setattr(server, '_session_live_title', lambda *a: '')
    monkeypatch.setattr(server, '_session_usage_snapshot', lambda *a: {})
    monkeypatch.setattr(server, '_load_approval_mode', lambda: 'manual')
    monkeypatch.setattr(server, '_probe_credentials', lambda *a: '')
    monkeypatch.setattr(banner, 'get_update_result', lambda *a, **kw: None)
    monkeypatch.setattr(banner, 'get_available_skills', lambda: {})
    expected = rpc(peers['B'], 'B')['result']['info']
    events = []
    monkeypatch.setattr(server, '_emit', lambda event, sid, payload: events.append((event, sid, payload)))
    server._emit_session_info_for_session('B', record)
    assert len(events) == 1, events
    event, sid, payload = events[0]
    assert event == 'session.info' and sid == 'B'
    assert payload.get('planner_model_info') == expected, payload
    assert set(payload['planner_model_info']) == {'model','provider','reasoning_effort','reasoning_effort_wire',
                                                'model_available','provider_available'}
    available = kind in ('live', 'endpoint', 'lazy-known')
    assert expected['provider'] == ('custom:second' if available else '')
    assert expected['provider_available'] is available
