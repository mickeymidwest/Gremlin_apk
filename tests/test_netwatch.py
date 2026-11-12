"""S6 network watch: LAN-neighbour / Tailscale-node parsing + diff findings."""
from gremlin_core import guard


def test_lan_neighbors_parses_stable_entries(monkeypatch):
    def fake_run(argv, timeout=30.0):
        if argv[:2] == ["ip", "-4"]:
            return ("192.168.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:01 REACHABLE\n"
                    "192.168.1.50 dev eth0 lladdr aa:bb:cc:dd:ee:02 STALE\n"
                    "192.168.1.99 dev eth0  FAILED\n"            # no lladdr -> skipped
                    "192.168.1.77 dev eth0 lladdr aa:bb:cc:dd:ee:03 INCOMPLETE\n")  # skipped
        return ""
    monkeypatch.setattr(guard, "_run", fake_run)
    rows = guard.lan_neighbors()
    assert rows == ["192.168.1.1 aa:bb:cc:dd:ee:01", "192.168.1.50 aa:bb:cc:dd:ee:02"]


def test_tailscale_nodes_parses_peers(monkeypatch):
    def fake_run(argv, timeout=30.0):
        if argv[:1] == ["tailscale"]:
            return ("100.0.0.1  manjaro      mickey@  linux   -\n"
                    "100.0.0.2     pixel        mickey@  android active; direct\n"
                    "# header line that should not match\n")
        return ""
    monkeypatch.setattr(guard, "_run", fake_run)
    assert guard.tailscale_nodes() == ["manjaro 100.0.0.1", "pixel 100.0.0.2"]


def test_diff_flags_new_device_and_mac_change_and_node():
    base = {"lan_neighbors": ["192.168.1.1 aa:bb:cc:dd:ee:01", "192.168.1.50 de:ad:be:ef:00:01"],
            "tailscale_nodes": ["manjaro 100.0.0.1"]}
    now = {"lan_neighbors": ["192.168.1.1 aa:bb:cc:dd:ee:01",      # unchanged
                             "192.168.1.50 de:ad:be:ef:99:99",      # MAC CHANGED -> spoof signal
                             "192.168.1.80 11:22:33:44:55:66"],     # NEW device
           "tailscale_nodes": ["manjaro 100.0.0.1", "pixel 100.0.0.2"]}  # NEW node
    found = guard.diff(base, now)
    whats = " || ".join(f"{f.level}:{f.what}" for f in found)
    assert "high:LAN device 192.168.1.50 changed MAC" in whats
    assert "medium:New device on the LAN: 192.168.1.80 11:22:33:44:55:66" in whats
    assert "medium:New Tailscale node: pixel 100.0.0.2" in whats
    # the unchanged device is not reported
    assert "192.168.1.1" not in whats


def test_diff_quiet_when_network_unchanged():
    snap = {"lan_neighbors": ["192.168.1.1 aa:bb:cc:dd:ee:01"], "tailscale_nodes": ["manjaro 100.1.1.1"]}
    assert guard.diff(snap, snap) == []


def test_standing_reports_lan_count():
    snap = {"lan_neighbors": ["192.168.1.1 aa:bb:cc:dd:ee:01", "192.168.1.2 aa:bb:cc:dd:ee:02"],
            "firewall": {"x": "active"}, "ssh": {"passwordauthentication": "no"}}
    whats = [f.what for f in guard.standing(snap)]
    assert any("2 device(s) on the LAN" in w for w in whats)


def test_rotating_ipv6_of_known_mac_is_not_flagged():
    # same physical device (same MAC) shows a new IPv6 -- must NOT be "new device"
    base = {"lan_neighbors": ["192.168.1.5 aa:bb:cc:dd:ee:10",
                              "fe80::1 aa:bb:cc:dd:ee:10"]}
    now = {"lan_neighbors": ["192.168.1.5 aa:bb:cc:dd:ee:10",
                             "2600:dead::beef aa:bb:cc:dd:ee:10"]}  # new v6, known MAC
    assert guard.diff(base, now) == []


def test_standing_flags_duplicate_ipv4_mac():
    snap = {"lan_neighbors": ["192.168.1.2 aa:bb:cc:dd:ee:20",
                              "192.168.1.3 aa:bb:cc:dd:ee:20",   # same MAC, 2 IPv4 -> spoof signal
                              "192.168.1.2 aa:bb:cc:dd:ee:20"]}
    whats = [f.what for f in guard.standing(snap)]
    assert any("One MAC (aa:bb:cc:dd:ee:20) answers for IPs" in w for w in whats)


def test_standing_does_not_flag_dualstack_same_mac():
    # one host's IPv4 + IPv6 share a MAC -- legitimate, not a duplicate-IPv4
    snap = {"lan_neighbors": ["192.168.1.9 aa:bb:cc:dd:ee:30", "fe80::9 aa:bb:cc:dd:ee:30"]}
    whats = [f.what for f in guard.standing(snap)]
    assert not any("answers for IPs" in w for w in whats)
