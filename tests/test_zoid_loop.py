"""zoid_loop: the memory guard that keeps the autonomous loop from
crashing this 7.5GB box."""
import zoid_loop as Z


def test_mem_available_is_an_int():
    v = Z._mem_available_mb()
    assert isinstance(v, int) and v > 0


def test_mem_gate_noop_when_ram_is_fine(monkeypatch):
    calls = []
    monkeypatch.setattr(Z, "_mem_available_mb", lambda: 4000)
    monkeypatch.setattr(Z, "_reclaim_memory", lambda log: calls.append("reclaim") or 4000)
    Z._mem_gate(lambda m: calls.append(m), floor=900)
    assert calls == []          # never touched reclaim or logged


def test_mem_gate_reclaims_when_tight(monkeypatch):
    seq = iter([500, 1200, 1200, 1200])   # low, then healthy after reclaim
    monkeypatch.setattr(Z, "_mem_available_mb", lambda: next(seq))
    reclaimed = []
    monkeypatch.setattr(Z, "_reclaim_memory",
                        lambda log: reclaimed.append(True) or 1200)
    logs = []
    Z._mem_gate(lambda m: logs.append(m), floor=900)
    assert reclaimed == [True]
    assert any("reclaim" in m.lower() for m in logs)


def test_mem_gate_waits_when_reclaim_isnt_enough(monkeypatch):
    # stays low through reclaim + one 30s wait, then recovers
    vals = iter([400, 400, 500, 900, 900])
    monkeypatch.setattr(Z, "_mem_available_mb", lambda: next(vals))
    monkeypatch.setattr(Z, "_reclaim_memory", lambda log: 500)
    sleeps = []
    monkeypatch.setattr(Z.time, "sleep", lambda s: sleeps.append(s))
    Z._mem_gate(lambda m: None, floor=900)
    assert 30 in sleeps          # it waited at least once
