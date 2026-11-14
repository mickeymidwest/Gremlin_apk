"""The pre-push secret scan blocks a push carrying a secret and allows clean
code (incl. assignments that only LOOK secret, like token = get_token())."""
import os
import subprocess
import tempfile

import pytest

HOOK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "deploy", "hooks", "pre-push"))
_Z = "0" * 40


def _run_push(content: str, env_extra=None):
    """Make a one-commit repo with `content`, run the hook as if pushing it to
    a new (empty) remote branch. Returns the hook's exit code."""
    with tempfile.TemporaryDirectory() as d:
        g = ["git", "-C", d]
        subprocess.run(g + ["init", "-q"], check=True)
        subprocess.run(g + ["config", "user.email", "x@y"], check=True)
        subprocess.run(g + ["config", "user.name", "x"], check=True)
        with open(os.path.join(d, "f.py"), "w") as f:
            f.write(content)
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "c"], check=True)
        sha = subprocess.run(g + ["rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        p = subprocess.run(["bash", HOOK, "origin", "url"], cwd=d,
                           input=f"refs/heads/main {sha} refs/heads/main {_Z}\n",
                           capture_output=True, text=True, env=env)
        return p.returncode


# NB: secrets are built by concatenation so this test file's own source never
# literally matches the hook's patterns (belt-and-suspenders; the hook also
# excludes this path from its scan).
CLEAN = (
    "token = get_or_create_token(data_dir)\n"          # token = func() -> not a secret
    "password = input('enter password: ')\n"           # password = input() -> not a secret
    "tokenizer_name = 'Qwen/Qwen2.5-7B-Instruct'\n"     # 'token' prefix, not the keyword
    "token_file = '/home/mickey/.magic/server_token'\n"  # a PATH, not the secret itself
    "URL = 'http://127.0.0.1:8765/status'\n"
    "def f():\n    return 1\n"
)

SECRETS = [
    "AWS = '" + "AKIA" + "1234567890ABCDEF'\n",
    'tok' + 'en = "' + "abcd1234efgh5678ijklmnop" + '"\n',   # secret-named quoted literal
    "host = '" + "100.122" + ".58.85'\n",                    # tailscale IP
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----" + "\nMIIabc\n",
]


def test_clean_code_passes():
    assert _run_push(CLEAN) == 0


@pytest.mark.parametrize("secret", SECRETS)
def test_secret_is_blocked(secret):
    assert _run_push(secret) == 1


def test_override_allows_push():
    assert _run_push(SECRETS[1], env_extra={"MAGIC_ALLOW_SECRETS": "1"}) == 0
