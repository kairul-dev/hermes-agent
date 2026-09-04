"""Event-controlled initialization: file visibility must not bypass coordination."""

import contextlib
import threading

import pytest


@pytest.mark.parametrize("dashboard", [False, True])
def test_visible_zero_byte_bootstrap_waits_for_initializer(tmp_path, monkeypatch, dashboard):
    import hermes_state as hs
    from hermes_cli import web_server

    path = tmp_path / "state.db"
    visible = threading.Event()
    release = threading.Event()
    contender_at_guard = threading.Event()
    errors = []
    results = []
    connect = hs._connect_tracked_db
    guard = hs.quarantine_cross_process_lock

    def paused_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        if threading.current_thread().name == "bootstrap-owner":
            assert path.stat().st_size == 0
            visible.set()
            assert release.wait(10)
        return conn

    @contextlib.contextmanager
    def observed_guard(*args, **kwargs):
        if threading.current_thread().name == "bootstrap-contender":
            contender_at_guard.set()
        with guard(*args, **kwargs) as acquired:
            yield acquired

    # Observe contention on the dashboard lock as well: that layer must keep
    # readers out until eager initialization finishes, not merely block writers.
    real_lock = threading.RLock()
    class DashboardLock:
        def __enter__(self):
            if threading.current_thread().name == "bootstrap-contender":
                contender_at_guard.set()
            real_lock.acquire()
            return self
        def __exit__(self, *args):
            real_lock.release()

    monkeypatch.setattr(hs, "_connect_tracked_db", paused_connect)
    monkeypatch.setattr(hs, "quarantine_cross_process_lock", observed_guard)
    monkeypatch.setattr(web_server, "_session_db_bootstrap_lock", DashboardLock())

    def worker(owner):
        try:
            if dashboard and owner:
                web_server._eager_reconcile_own_session_db(path)
                return
            db = (web_server._open_session_db_at_path(path, read_only=True)
                  if dashboard else hs.SessionDB(db_path=path))
            try:
                row = db._conn.execute(
                    "SELECT generation, epoch_id FROM session_usage_trusted_epoch"
                ).fetchone()
                assert row[0] == 1
                results.append(tuple(row))
            finally:
                db.close()
        except BaseException as exc:
            errors.append(exc)

    owner = threading.Thread(target=worker, args=(True,), name="bootstrap-owner")
    contender = threading.Thread(target=worker, args=(False,), name="bootstrap-contender")
    owner.start()
    try:
        assert visible.wait(10)
        contender.start()
        assert contender_at_guard.wait(10), "visible file bypassed initialization guard"
    finally:
        release.set()
        owner.join(15)
        if contender.ident is not None:
            contender.join(15)
    assert not owner.is_alive() and not contender.is_alive()
    assert not errors, errors
    assert results
    assert len(set(results)) == 1
    assert not list(tmp_path.glob("state.db.zeroed-*.bak"))
