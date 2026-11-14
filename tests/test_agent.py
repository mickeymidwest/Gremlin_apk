"""R3 agent: plans read-only checks, gathers evidence, diagnoses. No GPU."""
from magic_core import agent


def fake_call(*replies):
    seen, it = [], iter(replies)

    def call(messages, max_tokens, temperature, grammar=None):
        seen.append(messages[-1]["content"])
        return next(it), "stop", next(iter([None]))
    call.seen = seen
    return call


def test_plan_keeps_only_real_keys(tmp_path):
    call = fake_call("disk, memory, and maybe docker", "ignored")
    keys = agent.plan_checks(call, "my disk feels full")
    assert keys == ["disk", "memory", "docker"]


def test_plan_falls_back_to_keyword_routing():
    call = fake_call("none of these apply")   # model gives no valid keys
    keys = agent.plan_checks(call, "is my jellyfin container slow?")
    assert "docker" in keys


def test_plan_caps_at_max():
    call = fake_call("disk, memory, gpu, temps, kernel, bios, uptime, updates, network, units")
    assert len(agent.plan_checks(call, "everything", max_checks=3)) == 3


def test_run_is_read_only_and_reports(tmp_path):
    gathered = {}

    def gather(keys):
        gathered["keys"] = keys
        return "$ df -h\n/  234G  200G  20G  91%"
    call = fake_call("disk", "Your root filesystem is 91% full. 1. Clear the package cache. 2. Remove old logs.")
    out = agent.run(call, "why is my disk almost full", str(tmp_path), gather=gather)
    assert out["checks"] == ["disk"] and gathered["keys"] == ["disk"]
    assert "91% full" in out["report"]
    assert "didn't change anything" in out["report"]
    assert "read-only: disk" in out["report"]


def test_run_handles_no_relevant_check():
    call = fake_call("")   # no keys, and goal has no probe keyword
    out = agent.run(call, "tell me a joke", ".", gather=lambda k: "x")
    assert out["checks"] == [] and "which part of the machine" in out["report"]
