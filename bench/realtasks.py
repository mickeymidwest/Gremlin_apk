"""Real-task benchmark (R1): the kind of work Mickey actually asks Gremlin
for -- fixing Python, parsing command output, and producing the exact
shell/Docker/systemd/cron/rsync command. All graded mechanically (exec the
code and assert, or match the command shape), never by an LLM judge.

Held out: these are NOT used to tune the harness loops. A loop that lifts
the main quality bench but not this one is fitted to the test.

build(q) returns [(name, prompt, grader)] where q is bench/quality.py (its
graders and the no-think suffix live there).
"""
import re

REALTASK_VERSION = 1


# ---- graders for "give the exact command" tasks (static, mechanical) ----

def _cmd(pattern, flags=re.I):
    """The reply's command line must match `pattern`. Fences/prose stripped;
    the last non-empty line that looks like a command is judged."""
    rx = re.compile(pattern, flags)

    def grade(out):
        text = re.sub(r"```[a-z]*", "", out or "").replace("```", "")
        lines = [l.strip().lstrip("$# ").strip() for l in text.splitlines() if l.strip()]
        for line in reversed(lines):
            if rx.search(line):
                return True, None
        return False, f"no line matched; last: {lines[-1][:80] if lines else '(empty)'!r}"
    return grade


def _all(*needles):
    """Every needle (case-insensitive substring) appears somewhere in the reply."""
    def grade(out):
        low = (out or "").lower()
        missing = [n for n in needles if n.lower() not in low]
        return (not missing), (f"missing {missing}" if missing else None)
    return grade


def build(q):
    code = q.g_pycode
    suffix = getattr(q, "NOTHINK_SUFFIX", "")
    tasks = [
        # --- fix / write Python he'd actually use ---
        ("rt_prune_logs",
         "Write a Python function `prune_logs(paths, keep)` that takes a list of file-path strings and an int "
         "keep, and returns the list of paths to DELETE so that only the `keep` newest survive. Treat the list "
         "as already ordered newest-first. Return [] if there are keep or fewer. Only the code.",
         code("assert prune_logs(['a','b','c','d'], 2) == ['c','d']\n"
              "assert prune_logs(['a'], 3) == []\n"
              "assert prune_logs([], 1) == []\n"
              "assert prune_logs(['a','b'], 0) == ['a','b']")),
        ("rt_parse_execstart",
         "Write a Python function `exec_start(unit_text)` that returns the value of the first ExecStart= line "
         "in a systemd unit file (the text after the '='), stripped, or None if there isn't one. Only the code.",
         code("assert exec_start('[Service]\\nExecStart=/usr/bin/foo --bar\\n') == '/usr/bin/foo --bar'\n"
              "assert exec_start('Type=simple\\n') is None\n"
              "assert exec_start('ExecStart = /x\\n') == '/x'")),
        ("rt_free_gb",
         "Write a Python function `free_gb(avail_bytes)` that takes an integer number of bytes and returns the "
         "whole number of gigabytes (1 GB = 1024**3 bytes), rounded down. Only the code.",
         code("assert free_gb(1073741824) == 1\nassert free_gb(0) == 0\n"
              "assert free_gb(5*1024**3 + 500) == 5\n"
              "assert free_gb(2*1024**3 - 1) == 1")),
        ("rt_fix_retry",
         "This function should try calling fn() up to `tries` times, returning its result, and re-raise the "
         "last exception if they all fail -- but it's buggy. Return the fixed function, only the code.\n\n"
         "def retry(fn, tries):\n    for i in range(tries):\n        return fn()\n",
         code("_c = {'n': 0}\n"
              "def _flaky():\n    _c['n'] += 1\n    if _c['n'] < 3: raise ValueError('x')\n    return 'ok'\n"
              "assert retry(_flaky, 5) == 'ok'\n"
              "def _bad():\n    raise RuntimeError('nope')\n"
              "try:\n    retry(_bad, 2)\n    assert False\nexcept RuntimeError:\n    pass")),
        ("rt_parse_ports",
         "Write a Python function `listening_ports(ss_output)` that takes the text of `ss -tlnH` and returns a "
         "sorted list of the unique integer ports in the Local Address:Port column (the number after the last "
         "colon on each line). Only the code.",
         code("_s = 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\\nLISTEN 0 128 [::]:8096 [::]:*\\n"
              "LISTEN 0 128 127.0.0.1:631 0.0.0.0:*'\n"
              "assert listening_ports(_s) == [22, 631, 8096]\nassert listening_ports('') == []")),
        # --- produce the exact command ---
        ("rt_docker_restart",
         "Give the single shell command to restart the Docker container named jellyfin. Only the command.",
         _cmd(r"^docker\s+restart\s+jellyfin\b")),
        ("rt_journalctl",
         "Give the command to show the last 50 log lines of the systemd USER service gremlin.service using "
         "journalctl. Only the command.",
         _all("journalctl", "--user", "gremlin", "-n 50")),
        ("rt_cron_230",
         "Write the crontab line that runs /home/mickey/backup.sh every day at 2:30 AM. Only the line.",
         _cmd(r"^30\s+2\s+\*\s+\*\s+\*\s+/home/mickey/backup\.sh\s*$")),
        ("rt_rsync_mirror",
         "Give the rsync command to mirror the local folder ~/Downloads to mickey@desktop:/backup over SSH, "
         "using archive mode and deleting files on the destination that no longer exist locally. Only the command.",
         _all("rsync", "-a", "--delete", "~/downloads", "mickey@desktop:/backup")),
        ("rt_find_suid",
         "Give the command to find all SUID files under /usr (regular files only). Only the command.",
         _all("find", "/usr", "-perm", "4000", "-type f")),
        # --- structured output / config ---
        ("rt_json_conf",
         "Respond with ONLY a JSON object, no prose, no code fence, with exactly the keys \"host\" (string), "
         "\"port\" (integer) and \"tls\" (boolean) describing a server at example.com on port 8765 with TLS off." + suffix,
         _g_server_json),
        ("rt_units_table",
         "Make a GitHub-flavored markdown table with columns Service and State for: sshd active, docker active, "
         "ufw inactive. Header row, separator row, then one row each, in that order. Only the table.",
         _g_units_table),
    ]
    tasks = [(n, p if p.endswith(suffix) or not isinstance(p, str) else p + suffix, g) for n, p, g in tasks]
    return tasks


def _g_server_json(out):
    import json
    try:
        d = json.loads((out or "").strip())
    except ValueError as e:
        return False, f"not JSON: {e}"
    if set(d) != {"host", "port", "tls"}:
        return False, f"keys={sorted(d)}"
    if d.get("host") != "example.com" or d.get("port") != 8765:
        return False, f"host/port wrong: {d.get('host')} {d.get('port')}"
    if d.get("tls") is not False:
        return False, f"tls should be false, got {d.get('tls')!r}"
    return True, None


def _g_units_table(out):
    rows = [l.strip() for l in (out or "").splitlines() if l.strip().startswith("|")]
    body = []
    for r in rows:
        cells = [c.strip().lower() for c in r.strip("|").split("|")]
        if not cells or not (set(cells[0]) - set("-: ")) or cells[0] == "service":
            continue
        body.append(cells)
    got = [(c[0], c[1]) for c in body if len(c) >= 2]
    want = [("sshd", "active"), ("docker", "active"), ("ufw", "inactive")]
    return (got == want), f"got={got}"
