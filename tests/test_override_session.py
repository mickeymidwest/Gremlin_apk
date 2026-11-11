"""Override session: activate, dead-man idle expiry, touch, status."""
import gremlin_core.override_session as O


def _reset():
    O.deactivate()


def test_starts_inactive():
    _reset()
    assert O.is_active() is False
    assert O.status()["active"] is False


def test_activate_and_deactivate():
    _reset()
    O.activate()
    assert O.is_active() is True
    assert O.status()["active"] is True and O.status()["seconds_remaining"] > 0
    O.deactivate()
    assert O.is_active() is False


def test_idle_expiry_kills_it(monkeypatch):
    _reset()
    t = [1000.0]
    monkeypatch.setattr(O.time, "time", lambda: t[0])
    O.activate()
    assert O.is_active() is True
    t[0] += O.IDLE_SECONDS + 1          # 5 min + 1s of no interaction
    assert O.is_active() is False        # dead-man switch fired
    assert O.status()["active"] is False


def test_touch_keeps_it_alive(monkeypatch):
    _reset()
    t = [1000.0]
    monkeypatch.setattr(O.time, "time", lambda: t[0])
    O.activate()
    for _ in range(10):                  # interacting every 4 min keeps it hot
        t[0] += O.IDLE_SECONDS - 60
        O.touch()
        assert O.is_active() is True
    t[0] += O.IDLE_SECONDS + 1           # then go quiet -> expires
    assert O.is_active() is False


def test_touch_on_inactive_is_noop():
    _reset()
    O.touch()
    assert O.is_active() is False


def test_status_counts_down(monkeypatch):
    _reset()
    t = [1000.0]
    monkeypatch.setattr(O.time, "time", lambda: t[0])
    O.activate()
    assert O.status()["seconds_remaining"] == O.IDLE_SECONDS
    t[0] += 120
    assert O.status()["seconds_remaining"] == O.IDLE_SECONDS - 120
