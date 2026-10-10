"""``session.control.read`` / ``session.control`` through REAL native dispatch under strict shared-runtime admission.

Desktop hydrates its goal / loop / heartbeat card with ``session.control.read`` and drives it with
``session.control``. Both carry a ``session_id`` but were left unclassified, so in strict shared mode a valid
owner/member was refused with "this session-scoped native method is not classified" and the card showed
"Session controls unavailable". Same class as ``orchestration.set`` and ``session.start_chat``.

Both contracts also accept a ``profile`` that ``_profile_scoped`` honors over the session's own home, so an
override could aim the session's key at a different profile's store. Admission therefore binds the override to the
session's own profile. Every denial test asserts the handler never ran AND that BOTH profiles' state is unchanged;
every success test asserts the real state change in the authorized profile only.
"""
import io
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tui_gateway import server
from tui_gateway.transport import StdioTransport

KEY = 'shared-control-key'      # the SAME stored key lives in both profiles, so a redirected call is observable
READ = 'session.control.read'
ACT = 'session.control'


class _AuthedTransport(StdioTransport):
    """A transport carrying a server-verified login, as the WebSocket upgrade stamps it."""

    auth_identity = None


def _transport(identity=None):
    transport = _AuthedTransport(lambda: io.StringIO(), threading.Lock())
    transport.auth_identity = identity
    return transport


@contextmanager
def _in(home):
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _seed(home, key=KEY, *, prompt):
    """Active goal + loop + heartbeat persisted in ``home``'s own store."""
    from hermes_cli.goals import GoalState, save_goal
    from hermes_cli.heartbeat import HeartbeatState, save_heartbeat
    from hermes_cli.loops import LoopState, save_loop

    with _in(home):
        save_goal(key, GoalState(goal=prompt, status='active', turns_used=3, max_turns=12,
                                 created_at=100.0, last_turn_at=200.0))
        save_loop(key, LoopState(prompt=prompt, status='active', mode='interval', interval_seconds=300,
                                 current_delay=300, created_at=100.0, next_due_at=400.0))
        save_heartbeat(key, HeartbeatState(prompt=prompt, interval_seconds=600, status='active',
                                           created_at=100.0, last_fired_at=150.0, fire_count=2))


def _state(home, key=KEY):
    """The full persisted control snapshot of ``key`` inside ``home`` — what a stray read or write would change."""
    with _in(home):
        return server._snapshot_control(key)


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """Strict mode with two disposable profiles: the launch profile and ``worker`` (same stored key, distinct state)."""
    launch = tmp_path / '.hermes'
    worker = launch / 'profiles' / 'worker'
    worker.mkdir(parents=True)
    (launch / 'config.yaml').write_text('dashboard:\n  shared_runtime:\n    enabled: true\n', encoding='utf-8')
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(launch))
    monkeypatch.setattr(server, '_hermes_home', launch)
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    _seed(launch, prompt='launch-profile work')
    _seed(worker, prompt='worker-profile work')
    member = _transport()
    for sid, home in (('A', launch), ('W', worker)):
        monkeypatch.setitem(server._sessions, sid, dict(
            transport=member, agent=None, session_key=KEY, profile_home=str(home), source='desktop',
            history=[], history_lock=threading.Lock(), history_version=0, running=False, attached_images=[],
            cols=120))
    yield SimpleNamespace(launch=launch, worker=worker, member=member)
    goals._DB_CACHE.clear()


def _call(transport, rpc_method, **params):
    # Pool-run handlers answer on a worker that runs exactly handle_request on the bound transport; run it inline.
    token = server.bind_transport(transport)
    try:
        return server.handle_request({'jsonrpc': '2.0', 'id': 7, 'method': rpc_method, 'params': params})
    finally:
        server.reset_transport(token)


def _spy_handlers(monkeypatch):
    reached = []
    for name in (READ, ACT):
        original = server._methods[name]

        def spy(rid, params, _name=name, _original=original):
            reached.append(_name)
            return _original(rid, params)

        monkeypatch.setitem(server._methods, name, spy)
    return reached


def _assert_unchanged(profiles, before):
    assert _state(profiles.launch) == before['launch']
    assert _state(profiles.worker) == before['worker']


def _snapshots(profiles):
    return {'launch': _state(profiles.launch), 'worker': _state(profiles.worker)}


# ── admitted: the real control surface works for the owner-member ────────────────────────────────


@pytest.mark.parametrize('sid,own,other', [('A', 'launch', 'worker'), ('W', 'worker', 'launch')])
def test_owner_member_reads_the_real_persisted_control_state(profiles, sid, own, other):
    home = getattr(profiles, own)
    response = _call(profiles.member, READ, session_id=sid)
    assert 'error' not in response, response
    control = response['result']['control']
    assert control == _state(home)                                   # exactly the session's own profile
    assert control['goal']['title'] == f'{own}-profile work'          # ...and not the same key's state in the other
    assert control['goal']['status'] == 'active' and control['loop']['status'] == 'active'
    assert control['heartbeat']['status'] == 'active'
    assert control != _state(getattr(profiles, other))


@pytest.mark.parametrize('sid,own,other', [('A', 'launch', 'worker'), ('W', 'worker', 'launch')])
def test_owner_member_pause_resume_changes_only_the_authorized_profile(profiles, sid, own, other):
    home, other_home = getattr(profiles, own), getattr(profiles, other)
    untouched = _state(other_home)

    paused = _call(profiles.member, ACT, session_id=sid, action='goal.pause')
    assert paused['result']['control']['goal']['status'] == 'paused', paused
    assert _state(home)['goal']['status'] == 'paused'
    resumed = _call(profiles.member, ACT, session_id=sid, action='goal.resume')
    assert resumed['result']['control']['goal']['status'] == 'active', resumed
    assert resumed['result']['dispatch']['display'] == '/goal resume'

    assert _call(profiles.member, ACT, session_id=sid, action='loop.pause')['result']['control']['loop']['status'] == 'paused'
    assert _state(home)['loop']['status'] == 'paused'
    assert _call(profiles.member, ACT, session_id=sid, action='loop.resume')['result']['control']['loop']['status'] == 'active'

    assert _call(profiles.member, ACT, session_id=sid, action='heartbeat.pause')['result']['control']['heartbeat']['status'] == 'paused'
    assert _state(home)['heartbeat']['status'] == 'paused'
    resumed_hb = _call(profiles.member, ACT, session_id=sid, action='heartbeat.resume')['result']['control']['heartbeat']
    assert resumed_hb['status'] == 'active' and resumed_hb['last_fired_at'] > 150.0

    assert _state(other_home) == untouched                           # the other profile never moved


# ── profile override: omitted / matching / conflicting ───────────────────────────────────────────


@pytest.mark.parametrize('override', [{}, {'profile': None}, {'profile': ''}, {'profile': '   '}], ids=['absent', 'null', 'empty', 'blank'])
def test_omitted_override_binds_the_sessions_own_profile(profiles, override):
    read = _call(profiles.member, READ, session_id='W', **override)
    assert read['result']['control'] == _state(profiles.worker)
    paused = _call(profiles.member, ACT, session_id='W', action='goal.pause', **override)
    assert paused['result']['control']['goal']['status'] == 'paused'
    assert _state(profiles.worker)['goal']['status'] == 'paused'
    assert _state(profiles.launch)['goal']['status'] == 'active'     # the same key in the launch profile is untouched


@pytest.mark.parametrize('sid,own,name', [('W', 'worker', 'worker'), ('A', 'launch', 'default')])
def test_matching_override_is_admitted_and_acts_on_that_profile(profiles, sid, own, name):
    home = getattr(profiles, own)
    other_home = profiles.launch if own == 'worker' else profiles.worker
    untouched = _state(other_home)
    read = _call(profiles.member, READ, session_id=sid, profile=name)
    assert 'error' not in read, read
    assert read['result']['control'] == _state(home)
    paused = _call(profiles.member, ACT, session_id=sid, action='loop.pause', profile=name)
    assert paused['result']['control']['loop']['status'] == 'paused', paused
    assert _state(home)['loop']['status'] == 'paused' and _state(other_home) == untouched


@pytest.mark.parametrize('sid,profile', [
    ('A', 'worker'),            # launch-profile session aimed at the worker store
    ('W', 'default'),           # worker session aimed at the launch store
    ('W', 'hermes'),            # the legacy launch-profile basename alias
    ('A', 'ghost'),             # a profile that does not exist
    ('A', '../profiles/worker'),  # traversal spelling of another profile
])
@pytest.mark.parametrize('rpc_method,extra', [(READ, {}), (ACT, {'action': 'goal.pause'}), (ACT, {'action': 'heartbeat.pause'})])
def test_conflicting_override_is_denied_before_the_handler_and_changes_nothing(profiles, monkeypatch, sid, profile, rpc_method, extra):
    reached = _spy_handlers(monkeypatch)
    before = _snapshots(profiles)
    response = _call(profiles.member, rpc_method, session_id=sid, profile=profile, **extra)
    assert response.get('error', {}).get('code') == 4403, response
    assert 'profile' in response['error']['message']
    assert reached == []
    _assert_unchanged(profiles, before)


def test_malformed_override_is_denied_and_changes_nothing(profiles, monkeypatch):
    reached = _spy_handlers(monkeypatch)
    before = _snapshots(profiles)
    for bad in (5, ['worker'], {'name': 'worker'}):
        response = _call(profiles.member, ACT, session_id='A', action='goal.pause', profile=bad)
        assert response.get('error', {}).get('code') in (4000, 4403), response
    assert reached == []
    _assert_unchanged(profiles, before)


# ── ownership / membership denial (unchanged from the other member methods) ──────────────────────


@pytest.mark.parametrize('case', ['non_member', 'other_owner', 'unknown_session', 'malformed_policy'])
@pytest.mark.parametrize('rpc_method,extra', [(READ, {}), (ACT, {'action': 'goal.pause'}), (ACT, {'action': 'loop.stop'})])
def test_denied_callers_never_reach_the_handler_and_both_profiles_stay_unchanged(profiles, monkeypatch, case, rpc_method, extra):
    reached = _spy_handlers(monkeypatch)
    before = _snapshots(profiles)
    caller = {'other_owner': _transport({'provider': 'basic', 'user_id': 'mallory'})}.get(case, _transport())
    sid = 'missing' if case == 'unknown_session' else 'A'
    if case == 'malformed_policy':
        (profiles.launch / 'config.yaml').write_text('dashboard: [unterminated', encoding='utf-8')
        caller = profiles.member
    response = _call(caller, rpc_method, session_id=sid, **extra)
    assert response.get('error', {}).get('code') == 4403, response
    assert reached == []
    _assert_unchanged(profiles, before)


# ── restrictions that must NOT move ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize('inner,params', [
    (READ, {'session_id': 'A'}),
    (ACT, {'session_id': 'A', 'action': 'goal.pause'}),
])
def test_control_is_not_forwarded_by_the_shared_adapter(profiles, monkeypatch, inner, params):
    """The adapter allowlist (the Forge bridge) is a separate surface: native admission must not widen it."""
    reached = _spy_handlers(monkeypatch)
    before = _snapshots(profiles)
    token = server.bind_transport(profiles.member)
    try:
        response = server.handle_request({'jsonrpc': '2.0', 'id': 7, 'method': 'session.shared.rpc', 'params': {
            'runtime_epoch': server.shared_runtime_epoch(), 'method': inner, 'params': params}})
    finally:
        server.reset_transport(token)
    assert response.get('error', {}).get('code') == 4403, response
    assert 'not available through the shared adapter' in response['error']['message']
    assert reached == []
    _assert_unchanged(profiles, before)


def test_other_unclassified_session_methods_stay_denied(profiles):
    """Classifying the control pair must not broadly allow session methods."""
    for rpc_method in ('session.save', 'session.delete', 'process.kill'):
        response = _call(profiles.member, rpc_method, session_id='A')
        assert response.get('error', {}).get('code') == 4403, (rpc_method, response)
        assert 'not classified' in response['error']['message']
