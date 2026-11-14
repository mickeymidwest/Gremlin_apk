"""Campaign governor gate (Organoid #4): _await_headroom holds a battle while
the box is low, and fail-safe proceeds so it never stalls forever."""
import magic_core.magic.campaign as campaign


class _Stub:
    """Just enough of Campaign to call the real _await_headroom."""
    def __init__(self):
        self.logs = []
    def log(self, *a):
        self.logs.append(" ".join(str(x) for x in a))


# bind the real method onto the stub
_Stub._await_headroom = campaign.Campaign._await_headroom


def test_passes_through_when_healthy(monkeypatch):
    monkeypatch.setattr(campaign.resource, "ok_for_heavy", lambda: (True, "ok"))
    slept = []
    monkeypatch.setattr(campaign.time, "sleep", lambda s: slept.append(s))
    _Stub()._await_headroom()
    assert slept == []                      # no wait on a healthy box


def test_holds_then_proceeds_when_low(monkeypatch):
    monkeypatch.setattr(campaign.resource, "ok_for_heavy",
                        lambda: (False, "low RAM: 0.3GB free (< 0.9GB)"))
    slept = []
    monkeypatch.setattr(campaign.time, "sleep", lambda s: slept.append(s))
    s = _Stub()
    s._await_headroom(max_wait_s=30.0, poll_s=10.0)
    # it held (3 polls) then proceeded anyway -- never stalls forever
    assert slept == [10.0, 10.0, 10.0]
    assert any("governor: holding" in m for m in s.logs)


def test_recovers_midwait(monkeypatch):
    seq = iter([(False, "low disk"), (False, "low disk"), (True, "ok")])
    monkeypatch.setattr(campaign.resource, "ok_for_heavy", lambda: next(seq))
    slept = []
    monkeypatch.setattr(campaign.time, "sleep", lambda s: slept.append(s))
    _Stub()._await_headroom(max_wait_s=120.0, poll_s=5.0)
    assert slept == [5.0, 5.0]              # stopped waiting as soon as it recovered
