"""``orchestration.set`` through REAL native dispatch under strict shared-runtime admission.

Desktop writes the per-session orchestration policy with a native ``orchestration.set`` call. In strict
shared mode the dispatcher authorizes every native method BEFORE its handler runs; ``orchestration.set``
carries a ``session_id`` but was left unclassified, so a valid owner/member was refused with
"this session-scoped native method is not classified" while ``orchestration.get`` worked.
"""
import io
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import StdioTransport

_SET = dict(session_id='A', enabled=True, worker_provider='openrouter', worker_model='worker-a',
            worker_reasoning_effort='high')


class _AuthedTransport(StdioTransport):
    """A transport carrying a server-verified login, as the WebSocket upgrade stamps it."""

    auth_identity = None


def _transport(identity=None):
    transport = _AuthedTransport(lambda: io.StringIO(), threading.Lock())
    transport.auth_identity = identity
    return transport


@pytest.fixture
def shared_session(tmp_path, monkeypatch):
    home = tmp_path / 'launch-home'
    home.mkdir()
    (home / 'config.yaml').write_text('dashboard:\n  shared_runtime:\n    enabled: true\n', encoding='utf-8')
    monkeypatch.setattr(server, '_hermes_home', home)
    db = SessionDB(db_path=home / 'state.db')
    member = _transport()
    agent = SimpleNamespace(session_id='stored-A', _session_db=db, platform='desktop')
    record = dict(transport=member, agent=agent, session_key='stored-A', profile_home=str(home),
                  source='desktop', history=[], running=False)
    monkeypatch.setitem(server._sessions, 'A', record)
    yield home, db, member
    db.close()


def _call(transport, rpc_method='orchestration.set', **params):
    return server.dispatch({'jsonrpc': '2.0', 'id': 7, 'method': rpc_method, 'params': params}, transport)


def test_owner_member_can_set_through_native_dispatch(shared_session):
    _home, db, member = shared_session
    response = _call(member, **_SET)
    assert 'result' in response, response
    assert response['result']['enabled'] is True
    assert db.get_session_model_config_value('stored-A', '_orchestration')['worker_model'] == 'worker-a'
    # The read side is unchanged and sees the write.
    assert _call(member, 'orchestration.get', session_id='A')['result']['worker_model'] == 'worker-a'


@pytest.mark.parametrize('case', ['non_member', 'other_owner', 'unknown_session', 'malformed_policy'])
def test_set_stays_denied_before_the_handler_runs(shared_session, case):
    home, db, _member = shared_session
    caller = _transport({'provider': 'basic', 'user_id': 'mallory'}) if case == 'other_owner' else _transport()
    params = dict(_SET, session_id='missing') if case == 'unknown_session' else dict(_SET)
    if case == 'malformed_policy':
        (home / 'config.yaml').write_text('dashboard: [unterminated', encoding='utf-8')
        caller = _member_of(server._sessions['A'])
    response = _call(caller, **params)
    assert response.get('error', {}).get('code') == 4403, response
    # Denied at admission: no handler ran, so not even the draft row was materialized.
    assert db.get_session('stored-A') is None


def _member_of(record):
    return record['transport']


def test_set_is_not_forwarded_by_the_shared_adapter(shared_session):
    """The adapter allowlist is a separate surface: native admission must not widen it."""
    _home, db, member = shared_session
    # session.shared.rpc is a pool-run handler (dispatch() returns None); admit it inline on the bound transport.
    token = server.bind_transport(member)
    try:
        response = server.handle_request({'jsonrpc': '2.0', 'id': 7, 'method': 'session.shared.rpc', 'params': {
            'runtime_epoch': server.shared_runtime_epoch(), 'method': 'orchestration.set', 'params': dict(_SET)}})
    finally:
        server.reset_transport(token)
    assert response.get('error', {}).get('code') == 4403, response
    assert db.get_session('stored-A') is None
