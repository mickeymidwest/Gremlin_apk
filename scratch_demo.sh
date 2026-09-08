#!/usr/bin/env bash
# One-command walkthrough for a demo. Stops the service (VRAM), runs the
# three proof points, restarts the service.  ~10-15 min.
#   cd ~/Downloads/gremlin && bash scratch_demo.sh
set -u
cd "$(dirname "$0")"
source ~/android-build/env.sh 2>/dev/null || true

echo "============================================================"
echo " GREMLIN + MAGIC  --  demo walkthrough"
echo "============================================================"
echo
echo ">> stopping the chat service so the GPU is free for the agent"
systemctl --user stop gremlin.service gremlin-watchdog.timer 2>/dev/null
pkill -9 -f "main.py serve" 2>/dev/null; sleep 3

echo
echo "------------------------------------------------------------"
echo " 1/3  FINDING A BUG  --  Gremlin writes a fuzzer for a C parser"
echo "------------------------------------------------------------"
venv/bin/python scratch_fuzz_battle.py ~/Downloads/fuzz-practice-3 2>&1 \
  | grep -E "^\[battle|^\[verify|FOUND A CRASH|score=" | tail -20

echo
echo "------------------------------------------------------------"
echo " 2/3  BUILDING AN APP  --  method-by-method against the tests"
echo "------------------------------------------------------------"
venv/bin/python - <<'PY' 2>&1 | grep -E "method_builder|FINAL"
import shutil, tempfile
from pathlib import Path
from gremlin_core.registry import ModelRegistry
from gremlin_core.magic.model import BackendModel
from gremlin_core.magic.method_builder import build_from_scaffold
m = BackendModel(ModelRegistry.from_yaml("config/models.yaml").get("qwen2.5-coder-7b"), temperature=0.2)
w = Path(tempfile.mkdtemp()); shutil.rmtree(w)
shutil.copytree(Path.home()/"Downloads/buildalot-work", w, ignore=shutil.ignore_patterns(".git"))
r = build_from_scaffold(str(w), "app/src/main/java/com/buildalot/game/Game.kt",
    "./gradlew testDebugUnitTest --offline --console=plain", m, best_of=2, repair_rounds=3,
    compile_cmd="./gradlew :app:compileDebugKotlin --offline --console=plain -q")
print(f"FINAL: {r.passed}/{r.passed+r.failed} tests pass  ({r.score:.0%})")
shutil.rmtree(w, ignore_errors=True)
PY

echo
echo "------------------------------------------------------------"
echo " 3/3  IT LEARNS  --  the skill library it's grown"
echo "------------------------------------------------------------"
echo "skill cards: $(ls data/skills/*.yaml | wc -l)   (started at 30, hand-written)"
ls data/skills/ | sed 's/\.yaml//' | column -c 100
echo
tail -n 25 "$(ls -t data/zoid/run-*.log 2>/dev/null | head -1)" 2>/dev/null | grep -E "score=|best so far" || echo "(no zoid run logged yet)"

echo
echo ">> restarting the chat service"
systemctl --user start gremlin-watchdog.timer gremlin.service 2>/dev/null
echo "done -- give it ~90s to warm the model, then chat on the phone"
