"""Real-task benchmark (R1): the kind of work Mickey actually asks Magic
for -- fixing Python, parsing command output, and producing the exact
shell/Docker/systemd/cron/rsync command. All graded mechanically (exec the
code and assert, or match the command shape), never by an LLM judge.

Held out: these are NOT used to tune the harness loops. Graders reject both
wrong AND dangerous answers (a reversed rsync, an exact-mode SUID find), and
accept the valid forms of a command (-n50 / --lines=50, -va / --archive,
symbolic -perm -u+s), per the 2026-11-08 review.
"""
import re

REALTASK_VERSION = 2


def _lines(out):
    text = re.sub(r"```[a-z]*", "", out or "").replace("```", "")
    rows = []
    for l in text.splitlines():
        l = l.strip().strip("`").strip()
        l = re.sub(r"^[$#]\s*", "", l).strip()
        if l:
            rows.append(l)
    return rows


def _cmd(pattern, flags=re.I):
    rx = re.compile(pattern, flags)

    def grade(out):
        rows = _lines(out)
        for line in reversed(rows):
            if rx.search(line):
                return True, None
        return False, f"no line matched; last: {rows[-1][:80] if rows else '(empty)'!r}"
    return grade


def _grader(fn, why):
    def grade(out):
        for line in _lines(out):
            if fn(line, line.lower()):
                return True, None
        return False, why
    return grade


def _journalctl(line, low):
    return bool("journalctl" in low and "--user" in low
                and re.search(r"(?:-u|--unit|--user-unit)[=\s]+magic(?:\.service)?\b", low)
                and re.search(r"(?:-n\s*50|--lines[=\s]+50)\b", low))


def _rsync(line, low):
    if not low.startswith("rsync") or "--delete" not in low:
        return False
    if not ("--archive" in low or re.search(r"(?:^|\s)-[a-z]*a[a-z]*(?:\s|$)", line)):
        return False
    src = re.search(r"(?:~|\$home|/home/mickey)/downloads/?", low)
    dst = low.find("mickey@desktop:/backup")
    return bool(src) and dst != -1 and src.start() < dst


def _find_suid(line, low):
    return bool(low.startswith("find") and "/usr" in line and re.search(r"-type\s+f\b", low)
                and re.search(r"-perm\s+[-/](?:0?4000|u[=+]s)", low))


def build(q):
    code = q.g_pycode
    tasks = [
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
         code("assert exec_start('[Service]\\nExecStartPre=/bin/true\\nExecStart=/usr/bin/foo --bar\\n') == '/usr/bin/foo --bar'\n"
              "assert exec_start('Type=simple\\n') is None\n"
              "assert exec_start('ExecStart = /x\\n') == '/x'")),
        ("rt_free_gb",
         "Write a Python function `free_gb(avail_bytes)` that takes an integer number of bytes and returns the "
         "whole number of gigabytes (1 GB = 1024**3 bytes), rounded down. Only the code.",
         code("assert free_gb(1073741824) == 1\nassert free_gb(0) == 0\n"
              "assert free_gb(5*1024**3 + 500) == 5\nassert free_gb(2*1024**3 - 1) == 1")),
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
        ("rt_docker_restart",
         "Give the single shell command to restart the Docker container named jellyfin. Only the command.",
         _cmd(r"^docker\s+(?:container\s+)?restart\s+jellyfin\s*$")),
        ("rt_journalctl",
         "Give the command to show the last 50 log lines of the systemd USER service magic.service using "
         "journalctl. Only the command.",
         _grader(_journalctl, "needs journalctl --user with -u/--user-unit magic.service and -n 50 (any form)")),
        ("rt_cron_230",
         "Write the crontab line that runs /home/mickey/backup.sh every day at 2:30 AM. Only the line.",
         _cmd(r"^30\s+0?2\s+\*\s+\*\s+\*\s+/home/mickey/backup\.sh\s*$")),
        ("rt_rsync_mirror",
         "Give the rsync command to mirror the local folder ~/Downloads to mickey@desktop:/backup over SSH, "
         "using archive mode and deleting files on the destination that no longer exist locally. Only the command.",
         _grader(_rsync, "needs rsync -a/--archive --delete with ~/Downloads BEFORE mickey@desktop:/backup")),
        ("rt_find_suid",
         "Give the command to find all files with the SUID bit set under /usr (regular files only). Only the command.",
         _grader(_find_suid, "needs find /usr -perm -4000 (or /4000 or -u+s) -type f")),
        ("rt_json_conf",
         "Respond with ONLY a JSON object, no prose, no code fence, with exactly the keys \"host\" (string), "
         "\"port\" (integer) and \"tls\" (boolean) describing a server at example.com on port 8765 with TLS off.",
         _g_server_json),
        ("rt_units_table",
         "Make a GitHub-flavored markdown table with columns Service and State for: sshd active, docker active, "
         "ufw inactive. Header row, separator row, then one row each, in that order. Only the table.",
         _g_units_table),
    ]
    suffix = getattr(q, "NOTHINK_SUFFIX", "")
    return [(n, p + suffix, g) for n, p, g in tasks]


def _g_server_json(out):
    import json
    try:
        d = json.loads((out or "").strip())
    except ValueError as e:
        return False, f"not JSON: {e}"
    if not isinstance(d, dict):
        return False, f"not a JSON object: {type(d).__name__}"
    if set(d) != {"host", "port", "tls"}:
        return False, f"keys={sorted(d)}"
    if d.get("host") != "example.com" or d.get("port") != 8765 or isinstance(d.get("port"), bool):
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
