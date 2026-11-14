"""R1 real-task graders: each passes a correct answer, fails a wrong one,
accepts valid alternate command forms, and rejects dangerous ones."""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "bench", "quality.py")


@pytest.fixture(scope="module")
def q():
    spec = importlib.util.spec_from_file_location("bench_quality_rt", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tasks(q):
    return {name: (prompt, grader) for name, prompt, grader in q._realtasks()}


GOOD = {
    "rt_prune_logs": "```python\ndef prune_logs(paths, keep):\n    return list(paths[keep:]) if keep > 0 else list(paths)\n```",
    "rt_parse_execstart": "```python\ndef exec_start(unit_text):\n    for line in unit_text.splitlines():\n"
                          "        if line.strip().replace(' ','').startswith('ExecStart='):\n"
                          "            return line.split('=',1)[1].strip()\n    return None\n```",
    "rt_free_gb": "```python\ndef free_gb(avail_bytes):\n    return avail_bytes // (1024**3)\n```",
    "rt_fix_retry": "```python\ndef retry(fn, tries):\n    last = None\n    for i in range(tries):\n"
                    "        try:\n            return fn()\n        except Exception as e:\n            last = e\n    raise last\n```",
    "rt_parse_ports": "```python\ndef listening_ports(ss_output):\n    out = set()\n"
                      "    for line in ss_output.splitlines():\n        parts = line.split()\n"
                      "        if len(parts) >= 4:\n            tail = parts[3].rsplit(':', 1)[-1]\n"
                      "            if tail.isdigit():\n                out.add(int(tail))\n    return sorted(out)\n```",
    "rt_docker_restart": "`docker container restart jellyfin`",                         # container + backticks
    "rt_journalctl": "`journalctl --user -u magic.service -n50`",                      # inline backticks + -n50
    "rt_cron_230": "30 02 * * * /home/mickey/backup.sh",                                 # 02 for the hour
    "rt_rsync_mirror": "rsync --archive --delete ~/Downloads/ mickey@desktop:/backup",   # --archive form
    "rt_find_suid": "find /usr -perm -u+s -type f",                                      # symbolic SUID
    "rt_json_conf": '{"host": "example.com", "port": 8765, "tls": false}',
    "rt_units_table": "| Service | State |\n|---|---|\n| sshd | active |\n| docker | active |\n| ufw | inactive |",
}
BAD = {
    "rt_prune_logs": "```python\ndef prune_logs(paths, keep):\n    return list(paths[:keep])\n```",
    "rt_parse_execstart": "```python\ndef exec_start(unit_text):\n    return unit_text.strip()\n```",
    "rt_free_gb": "```python\ndef free_gb(avail_bytes):\n    return avail_bytes // (1000**3)\n```",
    "rt_fix_retry": "```python\ndef retry(fn, tries):\n    for i in range(tries):\n        return fn()\n```",
    "rt_parse_ports": "```python\ndef listening_ports(s):\n    return []\n```",
    "rt_docker_restart": "docker stop jellyfin",
    "rt_journalctl": "journalctl --user magic -n 50",                                  # missing -u (invalid cmd)
    "rt_cron_230": "0 2 * * * /home/mickey/backup.sh",
    "rt_rsync_mirror": "rsync -a --delete mickey@desktop:/backup ~/Downloads",           # REVERSED: wipes local
    "rt_find_suid": "find /usr -perm 4000 -type f",                                      # exact mode, not SUID-set
    "rt_json_conf": '["host", "port", "tls"]',                                           # array, not object
    "rt_units_table": "| Service | State |\n|---|---|\n| docker | active |\n| sshd | active |\n| ufw | inactive |",
}


def test_all_tasks_have_references(tasks):
    assert set(tasks) == set(GOOD) == set(BAD)


@pytest.mark.parametrize("task", sorted(GOOD))
def test_good_passes_bad_fails(tasks, task):
    grader = tasks[task][1]
    assert grader(GOOD[task])[0], (task, grader(GOOD[task]))
    assert not grader(BAD[task])[0], task


@pytest.mark.parametrize("reply", [
    "journalctl --user --user-unit magic.service --lines=50",
    "journalctl --user -u magic.service --lines 50",
])
def test_journalctl_accepts_valid_forms(tasks, reply):
    assert tasks["rt_journalctl"][1](reply)[0]


@pytest.mark.parametrize("reply", [
    "rsync -va --delete ~/Downloads mickey@desktop:/backup",
    "rsync -a --delete /home/mickey/Downloads/ mickey@desktop:/backup",
])
def test_rsync_accepts_valid_forms(tasks, reply):
    assert tasks["rt_rsync_mirror"][1](reply)[0]


def test_realtask_prompts_are_held_out(q, tasks):
    main = {p for _, p, _ in q.TASKS}
    assert not (main & {p for p, _ in tasks.values()})
    assert q.suite_hash("s", ("quality", "realtask")) != q.suite_hash("s", ("quality",))
