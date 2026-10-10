"""``session.start_chat`` (the handoff card's Retry) through REAL native dispatch under strict shared-runtime admission.

The card's Retry sends ``session.start_chat {session_id, tool_call_id, args}``. In strict shared mode the dispatcher
authorizes every native method BEFORE its handler runs; ``session.start_chat`` carries a ``session_id`` but was left
unclassified, so a valid owner/member was refused with "this session-scoped native method is not classified" and the
card's Retry could never run. Same class as ``orchestration.set`` (test_shared_runtime_orchestration_admission.py).
"""
import io
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import StdioTransport, current_transport


class _AuthedTransport(StdioTransport):
    """A transport carrying a server-verified login, as the WebSocket upgrade stamps it."""

    auth_identity = None


def _transport(identity=None):
    transport = _AuthedTransport(lambda: io.StringIO(), threading.Lock())
    transport.auth_identity = identity
    return transport


@pytest.fixture
def shared_session(tmp_path, monkeypatch):
    """A live strict-mode session 'A' (stored as 'stored-A') whose transcript holds one saved start_chat call."""
    home = tmp_path / 'launch-home'
    home.mkdir()
    (home / 'config.yaml').write_text('dashboard:\n  shared_runtime:\n    enabled: true\n', encoding='utf-8')
    monkeypatch.setattr(server, '_hermes_home', home)
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('stored-A', 'desktop')
    db.append_message('stored-A', 'tool', content='{"status": "rejected", "retryable": true}',
                      tool_name='start_chat', tool_call_id='call-1')
    member = _transport()
    agent = SimpleNamespace(session_id='stored-A', _session_db=db, platform='desktop')
    record = dict(transport=member, agent=agent, session_key='stored-A', profile_home=str(home),
                  source='desktop', history=[], running=False)
    monkeypatch.setitem(server._sessions, 'A', record)
    yield home, db, member
    db.close()


def _retry(transport, **overrides):
    params = dict(session_id='A', tool_call_id='call-1', args={})
    params.update(overrides)
    # session.start_chat is a pool-run handler (dispatch() returns None and the worker writes the reply); the pool's
    # worker runs exactly handle_request on the bound transport, so run that inline to read the response.
    token = server.bind_transport(transport)
    try:
        return server.handle_request({'jsonrpc': '2.0', 'id': 7, 'method': 'session.start_chat', 'params': params})
    finally:
        server.reset_transport(token)


def test_owner_member_retry_is_admitted_and_reaches_the_handler(shared_session):
    _home, db, member = shared_session
    response = _retry(member, args={'message': '   '})     # blank message -> the handler itself rejects ("message is empty"); that is the proof it ran
    assert 'error' not in response, response
    assert response['result']['status'] == 'rejected' and 'message is empty' in response['result']['reason']
    assert db.tool_row_retry('stored-A', 'call-1')[1] is None          # a rejection is not recorded as "retried"


def test_started_retry_creates_the_chat_under_the_callers_own_transport(shared_session, monkeypatch):
    """The new chat is stamped from the transport bound while it is created: the caller's, never a client param."""
    _home, db, member = shared_session
    seen = []

    def fake_create(_rid, params):
        seen.append((current_transport(), params))
        return {'result': {'session_id': 'B', 'stored_session_id': 'stored-B', 'info': {'profile_name': 'default'}}}

    monkeypatch.setattr(server, '_create_session', fake_create)
    monkeypatch.setitem(server._methods, 'prompt.submit', lambda _rid, _params: {'result': {'status': 'streaming'}})
    smuggled = _retry(member, args={'message': 'do the task', 'owner': 'mallory'})      # the contract refuses owner-ish args outright
    assert smuggled.get('error', {}).get('code') == 4000 and not seen, smuggled
    response = _retry(member, args={'message': 'do the task', 'title': 'Task'})
    assert 'result' in response, response
    assert response['result']['status'] == 'started', response
    assert [t for t, _p in seen] == [member]
    assert 'owner' not in seen[0][1] and 'auth_user_id' not in seen[0][1]
    assert db.tool_row_retry('stored-A', 'call-1')[1]['status'] == 'started'    # recorded: a second click cannot start a second chat
    again = _retry(member, args={'message': 'do the task', 'title': 'Task'})            # a second click / second window
    assert again['result']['status'] == 'started' and len(seen) == 1


@pytest.mark.parametrize('case', ['non_member', 'other_owner', 'unknown_session', 'malformed_policy'])
def test_retry_stays_denied_before_the_handler_runs(shared_session, monkeypatch, case):
    home, db, _member = shared_session
    started = []
    monkeypatch.setattr(server, '_create_session', lambda *_a, **_k: started.append(1) or {'error': {'message': 'must not run'}})
    caller = _transport({'provider': 'basic', 'user_id': 'mallory'}) if case == 'other_owner' else _transport()
    params = dict(session_id='missing') if case == 'unknown_session' else {}
    params['args'] = {'message': 'do the task'}
    if case == 'malformed_policy':
        (home / 'config.yaml').write_text('dashboard: [unterminated', encoding='utf-8')
        caller = server._sessions['A']['transport']
    response = _retry(caller, **params)
    assert response.get('error', {}).get('code') == 4403, response
    assert not started and db.tool_row_retry('stored-A', 'call-1')[1] is None


def test_retry_is_not_forwarded_by_the_shared_adapter(shared_session):
    """The adapter allowlist (the Forge bridge) is a separate surface: native admission must not widen it."""
    _home, db, member = shared_session
    token = server.bind_transport(member)
    try:
        response = server.handle_request({'jsonrpc': '2.0', 'id': 7, 'method': 'session.shared.rpc', 'params': {
            'runtime_epoch': server.shared_runtime_epoch(), 'method': 'session.start_chat',
            'params': dict(session_id='A', tool_call_id='call-1', args={'message': 'x'})}})
    finally:
        server.reset_transport(token)
    assert response.get('error', {}).get('code') == 4403, response
    assert db.tool_row_retry('stored-A', 'call-1')[1] is None
