#!/usr/bin/env bash
# Fast warm-daemon Android unit test + debug APK build.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_SH="$HOME/android-build/env.sh"
if [ -f "$ENV_SH" ]; then
  # shellcheck disable=SC1090
  source "$ENV_SH"
fi

T0=$(date +%s.%N)
cd "$ROOT/android"
./gradlew testDebugUnitTest assembleDebug --console=plain
T1=$(date +%s.%N)

ELAPSED="$(awk -v a="$T0" -v b="$T1" 'BEGIN {printf "%.2fs", b - a}')"
APK_PATH="$ROOT/android/app/build/outputs/apk/debug/app-debug.apk"
if [ -f "$APK_PATH" ]; then
  APK_BYTES="$(stat -c '%s' "$APK_PATH")"
  APK_MB="$(awk -v b="$APK_BYTES" 'BEGIN {printf "%.2f MiB", b / (1024 * 1024)}')"
  echo "[build_apk] elapsed=${ELAPSED} apk=${APK_PATH} size=${APK_BYTES} bytes (${APK_MB})"
else
  echo "[build_apk] elapsed=${ELAPSED} (APK missing at ${APK_PATH})" >&2
  exit 1
fi
