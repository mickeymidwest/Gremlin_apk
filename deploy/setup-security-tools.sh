#!/usr/bin/env bash
# Security-research toolchain for the vuln-research skills + FuzzVerifier.
# Needs root (pkexec dialog on a desktop session, or sudo from a terminal).
# Safe to re-run.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

priv_block() {
  set -e
  echo "== pacman: clang/llvm (libFuzzer+ASAN), radare2, checksec, unzip, ltrace =="
  pacman -S --needed --noconfirm clang llvm lld compiler-rt radare2 checksec unzip ltrace strace

  echo "== AFL++ (official repo package is 'afl++', provides 'afl') =="
  if pacman -Si afl++ >/dev/null 2>&1; then
    pacman -S --needed --noconfirm afl++
  elif pacman -Si aflplusplus >/dev/null 2>&1; then
    pacman -S --needed --noconfirm aflplusplus
  else
    echo "   not in the configured repos -- 'yay -S aflplusplus' or build from source"
  fi
}
export -f priv_block; export here

if [ "$(id -u)" -eq 0 ]; then priv_block
elif command -v pkexec >/dev/null && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  pkexec env here="$here" bash -c 'priv_block'
elif sudo -v 2>/dev/null; then sudo -E bash -c 'priv_block'
else
  echo "Need root. Run from a terminal:  ~/Downloads/gremlin/deploy/setup-security-tools.sh"; exit 1
fi

echo
echo "== venv python libs (semgrep / lief / capstone / pyelftools) =="
"$here/../venv/bin/pip" install -q semgrep lief capstone pyelftools frida-tools || true

echo
echo "-- jadx (APK decompiler) is a separate manual step: --"
echo "   yay -S jadx    OR    grab a release zip from github.com/skylot/jadx and put jadx/bin on PATH"
echo
echo "done. check:  clang --version ; r2 -v ; checksec --version ; venv/bin/semgrep --version"
