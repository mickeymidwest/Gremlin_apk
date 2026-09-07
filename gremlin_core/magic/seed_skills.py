"""A small starter set of skill cards -- the procedures a good engineer
runs almost without thinking, distilled from how the strong coding
harnesses (Aider, SWE-agent, Claude's own skill patterns) actually work.

They seed `data/skills/` as `candidate`s: they're loaded into battles
from the start and earn `active` the normal way, by winning. `/skill
seed` (idempotent -- skips names that already exist).
"""
from __future__ import annotations

from .types import Skill

_SEED = [
    dict(
        name="read-the-error-first",
        purpose="a failing command tells you what's wrong -- read it before touching code",
        trigger_when="a build, test, or command has just failed",
        trigger_matcher=r"fail|error|exception|traceback|exit [1-9]",
        procedure=[
            "read the actual error message and the file:line it names, not the surrounding output",
            "form a one-sentence hypothesis about the cause",
            "only then look at the code that hypothesis points to",
        ],
    ),
    dict(
        name="reproduce-before-fixing",
        purpose="confirm you can see the bug before you try to fix it",
        trigger_when="asked to fix a bug or a failing test",
        trigger_matcher=r"fix|bug|failing|broken|doesn't work|not working",
        procedure=[
            "run the failing test or the exact case that shows the bug",
            "confirm you see the failure and note what it actually says",
            "make the fix, then re-run the same case -- it must now pass",
        ],
    ),
    dict(
        name="one-change-at-a-time",
        purpose="a small model loses the thread on multi-file edits -- go one step at a time",
        trigger_when="a task needs more than one edit",
        procedure=[
            "make the single smallest edit that could move things forward",
            "run the check (tests / compile / lint) to see if it helped",
            "only make the next edit once you know the last one's effect",
        ],
    ),
    dict(
        name="search-before-you-guess",
        purpose="don't assume where a symbol lives or how it's used -- look",
        trigger_when="you need to change or call something defined elsewhere",
        trigger_matcher=r"import|call|use|where is|defined|function|class|method",
        procedure=[
            "grep / repo_map for the name to find every definition and use",
            "read the definition and one real call site before editing",
        ],
    ),
    dict(
        name="verify-before-done",
        purpose="never say DONE on code work without a green check",
        trigger_when="you think the task is finished",
        trigger_matcher=r"fix|bug|implement|add|change|test",
        procedure=[
            "run the task's tests (or compile / lint if there are none)",
            "if red, read the error and iterate",
            "say DONE only when the check is green",
        ],
    ),
    dict(
        name="edit-file-over-rewrite",
        purpose="rewriting a whole file is where a 7B mangles it -- use a targeted edit",
        trigger_when="changing a few lines of an existing file",
        procedure=[
            "read the exact block you want to change",
            "use edit_file with that block verbatim as `search`",
            "keep write_file for creating a new file or a total rewrite",
        ],
    ),
    dict(
        name="snapshot-before-risky-change",
        purpose="anything hard to undo gets a rollback point first",
        trigger_when="about to change a system config, run a package op, or edit outside a git repo",
        trigger_matcher=r"config|/etc|pacman|systemctl|install|upgrade|sysctl|firewall",
        procedure=[
            "take a BTRFS snapshot (snapper) or a git commit of the current state",
            "make the change",
            "note how to roll back if it goes wrong",
        ],
    ),
    dict(
        name="service-status-then-logs",
        purpose="the fix for a broken service is in its logs, not in guessing",
        trigger_when="a systemd service or container is misbehaving",
        trigger_matcher=r"service|systemd|jellyfin|docker|container|daemon|not running|crashed",
        procedure=[
            "systemctl --user status <service>  (or docker ps -a) for the state",
            "journalctl --user -u <service> -n 50  (or docker logs) for the real error",
            "act on what the log says, not on a guess",
        ],
    ),

    # --- this box specifically: Manjaro, RTX 2070 Super 8GB, 7.5GB RAM,
    #     one 7200rpm HDD, models on that HDD, Jellyfin in docker ---
    dict(
        name="manjaro-full-upgrade-only",
        purpose="a partial upgrade breaks Arch/Manjaro -- never sync the db without upgrading",
        trigger_when="installing a package or updating the system on Manjaro/Arch",
        trigger_matcher=r"pacman|-Sy\b|install .* package|update (the )?(system|box|desktop)|upgrade",
        procedure=[
            "use `pacman -Syu` (or `pacman -S <pkg>` on an already-current system) -- never a bare `pacman -Sy`",
            "a lone `-Sy` leaves the db newer than installed packages; the next install pulls mismatched deps and breaks the box",
            "if updates are large, snapshot first (see snapshot-before-risky-change)",
        ],
    ),
    dict(
        name="vram-budget-8gb",
        purpose="this card holds ~5.6GB for the primary model -- a second 7B won't load",
        trigger_when="loading, swapping, or benchmarking a local model",
        trigger_matcher=r"model|gguf|vram|nvidia-smi|load .* model|swap .* model|n_gpu_layers|benchmark",
        procedure=[
            "nvidia-smi first -- the primary alone is ~5556 MiB of the 8192",
            "unload the current local model before loading another (Magic's vram.ensure_only); two full 7B GGUFs do not co-exist here",
            "never partial-offload the primary (n_gpu_layers < -1) -- benched at 15 tok/s vs 60",
        ],
    ),
    dict(
        name="gremlin-not-answering",
        purpose="Gremlin silent on the phone = the service, not the network",
        trigger_when="Gremlin/the desktop stops responding to the app",
        trigger_matcher=r"not answer|no response|not connect|gremlin.*(down|stuck|silent|hung)|desktop.*(down|stuck)",
        procedure=[
            "systemctl --user status gremlin.service  and  journalctl --user -u gremlin.service -n 40",
            "check nvidia-smi for stale VRAM held by a crashed CUDA context (agent idle but ~6GB still used)",
            "systemctl --user restart gremlin.service -- first /chat after is a ~90s cold read off the HDD, that's normal",
        ],
    ),
    dict(
        name="hdd-cold-start-patience",
        purpose="slow first response after a restart is the spinning disk, not a hang",
        trigger_when="the model or a build seems stuck for the first ~90s after a service start",
        trigger_matcher=r"cold start|slow (first|to load)|stuck loading|90s|warmup|took forever to (start|load)",
        procedure=[
            "the model GGUF is ~5GB read off a 7200rpm HDD -- 60-150s cold, ~2s warm in page cache",
            "check /status model_loaded before assuming a wedge; the watchdog already tolerates this window",
            "don't restart again mid-load -- that just restarts the 90s clock",
        ],
    ),

    # --- Android / Kotlin / Gradle: how to actually build an app here ---
    dict(
        name="android-project-layout",
        purpose="where every file goes in a single-module Android/Gradle project",
        trigger_when="creating, reading, or editing an Android app project",
        trigger_matcher=r"android|\.kt\b|gradle|kotlin|apk|activity|manifest|jetpack|compose",
        procedure=[
            "root: settings.gradle.kts (includes :app), build.gradle.kts (plugin versions, apply false), gradle.properties, gradle/wrapper/",
            "app/build.gradle.kts: the android{} block (namespace, compileSdk, defaultConfig{applicationId,minSdk,targetSdk,versionCode,versionName}), dependencies{}",
            "app/src/main/AndroidManifest.xml, app/src/main/res/values/*.xml (strings, themes), app/src/main/java/<pkg>/*.kt",
            "app/src/test/java/<pkg>/*.kt = JVM unit tests (run by testDebugUnitTest); app/src/androidTest/ = on-device tests (don't use here)",
        ],
    ),
    dict(
        name="android-gradle-build-loop",
        purpose="build an Android feature by iterating against the compiler, not by guessing",
        trigger_when="implementing or fixing Kotlin in an Android project",
        trigger_matcher=r"android|\.kt\b|gradle|assembleDebug|testDebugUnitTest|compileDebugKotlin|kotlin",
        procedure=[
            "make ONE change, then run the check: ./gradlew testDebugUnitTest --offline --console=plain  (or assembleDebug if there are no tests)",
            "read the FIRST real error only -- the rest usually cascade from it",
            "fix that one, re-run; a green 'BUILD SUCCESSFUL' is the only proof",
            "commit / stop only when the check passes -- see verify-before-done",
        ],
    ),
    dict(
        name="android-read-gradle-errors",
        purpose="gradle output has three error shapes and they mean different things",
        trigger_when="a gradle build or test run failed",
        trigger_matcher=r"gradle.*fail|BUILD FAILED|What went wrong|Caused by|Unresolved reference|e: file",
        procedure=[
            "'e: file:///...Foo.kt:12:5 ...' = a Kotlin COMPILE error at that line:col -- open the file, fix the code",
            "'> Task :app:xyz FAILED' then 'What went wrong' = a Gradle CONFIG problem (a plugin, a version, a missing dep) -- fix build.gradle.kts",
            "'X tests completed, Y failed' with 'SomeTest > case FAILED' = logic wrong -- read the assertion, fix the implementation, not the test",
            "'Caused by:' is the root; ignore the wrapper stack above it",
        ],
    ),
    dict(
        name="android-minimal-deps-offline",
        purpose="on this box gradle runs --offline; only what's already cached resolves",
        trigger_when="choosing dependencies for an Android project on this machine",
        trigger_matcher=r"dependencies|implementation\(|androidx|appcompat|--offline|Could not resolve|No cached version",
        procedure=[
            "prefer framework classes: android.app.Activity, android.view.View, android.graphics.* -- no dependency needed",
            "'No cached version of X available for offline mode' means drop that dep or run once WITHOUT --offline to populate ~/.gradle/caches",
            "androidx.core:core-ktx and junit:junit:4.13.2 are cached and safe; appcompat pulls customview which may not be",
            "a custom View + a plain Activity covers most simple apps with zero deps",
        ],
    ),
    dict(
        name="android-targetsdk-edge-to-edge",
        purpose="targetSdk 35 force-enables edge-to-edge -- content draws under the status/nav bars",
        trigger_when="setting targetSdk, or the top/bottom of the UI is hidden or clipped",
        trigger_matcher=r"targetSdk|edge.to.edge|status bar|nav bar|inset|WindowInsets|hidden behind|clipped|fitsSystemWindows",
        procedure=[
            "if the top row of the UI is behind the status bar: either set targetSdk = 34, or handle insets",
            "insets: setOnApplyWindowInsetsListener { _, i -> topPad = i.systemWindowInsetTop; ... }  and offset your layout by it",
            "an ActionBar (Theme.Material, non-NoActionBar) also takes vertical space above the content view",
        ],
    ),
    dict(
        name="android-custom-view",
        purpose="draw a whole screen by hand with one View + Canvas",
        trigger_when="building a game board, chart, or any custom-drawn Android screen",
        trigger_matcher=r"custom view|onDraw|Canvas|onTouchEvent|invalidate|SurfaceView|drawRect|Paint\b|game board",
        procedure=[
            "extend View(context); do layout math in onSizeChanged(w,h,...) storing sizes as fields",
            "draw in onDraw(canvas): canvas.drawColor(bg) first, then shapes with reusable Paint objects (don't allocate in onDraw)",
            "input in onTouchEvent(e): handle only e.action == MotionEvent.ACTION_DOWN, hit-test against your stored rects, then call invalidate() to redraw",
            "an options menu needs an Activity with onCreateOptionsMenu/onOptionsItemSelected and a theme that has an ActionBar",
        ],
    ),
    dict(
        name="android-debug-apk-install",
        purpose="assembleDebug produces a signed, installable APK -- no manual signing",
        trigger_when="producing an APK to install on a phone",
        trigger_matcher=r"assembleDebug|app-debug\.apk|apksigner|install .*apk|debug keystore|unsigned",
        procedure=[
            "./gradlew assembleDebug -> app/build/outputs/apk/debug/app-debug.apk, auto-signed with the debug keystore (v2 scheme)",
            "same debug key every build, so it installs as an update over a prior debug build with no signature conflict",
            "bump versionCode every build so the installer shows it as an update, not 'app not installed'",
            "verify: apksigner verify --print-certs app-debug.apk",
        ],
    ),
    dict(
        name="kotlin-kdoc-terminator",
        purpose="a bare */ anywhere inside a /** ... */ comment body ends the comment early",
        trigger_when="writing a KDoc / block comment that mentions a path, a glob, or math",
        trigger_matcher=r"/\*\*|kdoc|block comment|Expecting a top level declaration",
        procedure=[
            "'Syntax error: Expecting a top level declaration' right after a doc comment usually means the comment closed early",
            "check the comment text for a literal '*/' (e.g. writing 'foo*/bar' or 'a */ b') and reword it",
            "same for '/*' opening a nested comment -- Kotlin block comments don't nest cleanly in older tooling",
        ],
    ),
    dict(
        name="android-agp9-kotlin",
        purpose="AGP 9.x compiles .kt with no separate Kotlin Gradle plugin",
        trigger_when="setting up an Android module's plugins block on this toolchain",
        trigger_matcher=r"org\.jetbrains\.kotlin\.android|kotlin plugin|kotlinOptions|plugin was not found|AGP|com\.android\.application",
        procedure=[
            "app/build.gradle.kts plugins { id(\"com.android.application\") } is enough -- .kt files compile via AGP's built-in Kotlin",
            "do NOT add id(\"org.jetbrains.kotlin.android\") here: it fails to resolve offline and isn't needed",
            "so no kotlinOptions{} block either; set the JVM target via compileOptions{ sourceCompatibility/targetCompatibility }",
        ],
    ),
]


def cards() -> list[Skill]:
    out = []
    for i, s in enumerate(_SEED):
        out.append(Skill(
            id=f"seed_{i:02d}", name=s["name"], purpose=s["purpose"],
            trigger_when=s["trigger_when"], trigger_matcher=s.get("trigger_matcher"),
            procedure=s["procedure"], provenance=["seed"], status="candidate",
        ))
    return out


def seed(project_root: str) -> list[str]:
    """Write any seed card not already present. Returns the names added."""
    from .store import Store
    store = Store(project_root)
    existing = {s.name for s in store.read_skills()}
    to_add = [c for c in cards() if c.name not in existing]
    if to_add:
        store.write_skills(store.read_skills() + to_add)
    return [c.name for c in to_add]
