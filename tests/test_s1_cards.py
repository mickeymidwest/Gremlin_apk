"""S1 security skill cards: valid schema, active, trigger regexes fire, load via Store."""
import re
from pathlib import Path

import yaml

from gremlin_core.magic.store import Store

REPO = Path(__file__).resolve().parents[1]
S1 = [
    "ssh-brute-force-defense", "exposed-port-defense", "rogue-persistence-units",
    "suid-sudo-privesc", "android-accessibility-abuse", "android-sideload-overlay",
    "arp-spoof-mitm-defense", "sim-swap-phishing-awareness",
]


def _card(name):
    return yaml.safe_load((REPO / "data" / "skills" / f"{name}.yaml").read_text())


def test_all_s1_cards_valid_and_active():
    for name in S1:
        p = REPO / "data" / "skills" / f"{name}.yaml"
        assert p.is_file(), name
        d = _card(name)
        assert d["id"].startswith("s1_")
        assert d["status"] == "active" and d["council_reviewed"] is True
        assert d["purpose"] and d["trigger_when"]
        assert isinstance(d["procedure"], list) and len(d["procedure"]) >= 2
        re.compile(d["trigger_matcher"])            # must be a valid regex


def test_trigger_matchers_fire_on_their_topic():
    cases = {
        "ssh-brute-force-defense": "someone is brute forcing my sshd on port 22",
        "exposed-port-defense": "what services are listening on 0.0.0.0",
        "rogue-persistence-units": "did anything add a new systemd unit or cron job",
        "suid-sudo-privesc": "check for suid privilege escalation paths",
        "android-accessibility-abuse": "an app has accessibility access, is it stalkerware",
        "android-sideload-overlay": "a sideloaded app wants to draw an overlay",
        "arp-spoof-mitm-defense": "is someone doing an arp spoof mitm on my lan",
        "sim-swap-phishing-awareness": "i got a phishing text, sim swap risk",
    }
    for name, phrase in cases.items():
        d = _card(name)
        assert re.search(d["trigger_matcher"], phrase, re.I), f"{name} did not fire on {phrase!r}"


def test_matchers_do_not_fire_on_ordinary_chat():
    benign = "what's a good recipe for chicken soup and how was your day"
    for name in S1:
        d = _card(name)
        assert not re.search(d["trigger_matcher"], benign, re.I), f"{name} false-fired"


def test_cards_load_through_the_store():
    names = {s.name for s in Store(str(REPO)).read_skills()}
    for name in S1:
        assert name in names, f"{name} not loaded by Store.read_skills()"
