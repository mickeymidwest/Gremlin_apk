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
        name="compiler-says-what-it-means",
        purpose="a compile error names the real problem -- fix THAT, not a guess",
        trigger_when="a compile or type-check error just came back",
        trigger_matcher=r"undeclared|undefined (reference|identifier|symbol)|unknown type|"
                        r"no member|not a member|cannot find symbol|has no attribute|"
                        r"expected .* before|implicit declaration",
        procedure=[
            "'undeclared identifier X' / 'unknown type X' -> X is spelled wrong or "
            "from a header you haven't included: grep the headers for the real name, "
            "don't just re-add an include you already have",
            "'undefined reference to X' at link time -> the definition exists but "
            "isn't being compiled/linked: add the .c that defines it to the build",
            "re-read the header and copy names EXACTLY before editing again",
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

    # --- vulnerability research (mickey does authorized bug-bounty work).
    #     the craft: fuzzing, static analysis, RE, crash triage, PoC.
    #     Scope discipline is the first card for a reason. ---
    dict(
        name="scope-first",
        purpose="only test what a program's written scope + your authorization actually covers",
        trigger_when="starting any security testing, recon, fuzzing, or exploitation",
        trigger_matcher=r"bug bounty|bounty|pentest|fuzz|exploit|recon|target|in.?scope|authorization|CTF|vuln research",
        procedure=[
            "confirm the exact asset is in the program's scope doc (domain, IP range, app package, repo) -- screenshot/save it",
            "note the rules: rate limits, no-DoS, no data exfiltration beyond proof, no social engineering unless allowed",
            "if it's your own lab / a downloaded target / a CTF, that's automatically fine -- say so and move on",
            "when the scope is unclear, ask the program (or mickey) before touching it, not after",
        ],
    ),
    dict(
        name="fuzz-harness",
        purpose="turn a parser/decoder into a fuzz target that finds memory bugs fast",
        trigger_when="fuzzing a library, file format, protocol parser, or codec",
        trigger_matcher=r"fuzz|libfuzzer|afl\+\+|honggfuzz|LLVMFuzzerTestOneInput|corpus|sanitizer|ASAN",
        procedure=[
            "write the smallest entry point: LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) that calls ONE parse function",
            "build with -fsanitize=address,undefined,fuzzer  (clang); for AFL++ use afl-clang-lto + a persistent-mode loop",
            "seed a corpus from real valid samples of that format; add a dictionary of magic bytes / keywords",
            "run with -max_len set near real inputs; watch for ASAN/UBSAN reports, not just SIGSEGV",
            "minimize any crash: libfuzzer -minimize_crash=1, or afl-tmin, then reproduce standalone",
        ],
    ),
    dict(
        name="static-vuln-sweep",
        purpose="grep the codebase for the vuln classes before reading everything",
        trigger_when="auditing source for vulnerabilities",
        trigger_matcher=r"static analysis|audit .* (code|source)|semgrep|codeql|weggli|sink|taint|injection|deserial|ssrf|path traversal",
        procedure=[
            "run semgrep --config auto + a language CodeQL pack + weggli for C/C++ memory patterns",
            "memory safety: memcpy/strcpy/sprintf/alloca with attacker length; input-derived array index; integer overflow before malloc",
            "injection: string-built SQL/shell/HTML/LDAP, format strings, template rendering of user data. auth: IDOR (missing check on an object id), JWT alg=none/weak secret, TOCTOU",
            "deserialization: pickle/unserialize/readObject/yaml.load on untrusted bytes. SSRF: input URL into a fetch. path traversal: '..' or absolute path into an open()",
            "trace each hit source (input) -> sink -- confirm the data reaches it unfiltered before flagging",
        ],
    ),
    dict(
        name="binary-recon",
        purpose="first pass on an unknown native binary -- what it is and where the attack surface is",
        trigger_when="reversing an ELF / Mach-O / PE, or handed a binary to analyze",
        trigger_matcher=r"revers|ghidra|radare|r2\b|IDA|binary|ELF|disassembl|decompil|checksec|\.so\b",
        procedure=[
            "file / checksec (NX, PIE, RELRO, canary) / strings / nm -D / rabin2 -I -- know the mitigations before you start",
            "load in Ghidra or radare2 (r2 -A); look at main, then every function that reads a socket, a file, argv, or env",
            "mark the parsers: fixed-size stack buffers, memcpy/read with a length from the wire, loops with an index from input",
            "for a network daemon, script the protocol with pwntools; for a file parser, feed it malformed samples under gdb + ASAN if you can rebuild",
        ],
    ),
    dict(
        name="apk-recon",
        purpose="map an Android app's attack surface: components, data flow, native code",
        trigger_when="analyzing or pentesting an Android APK",
        trigger_matcher=r"apk|android app|jadx|apktool|frida|drozer|exported|deep link|AndroidManifest|dex|smali",
        procedure=[
            "jadx-gui the apk; read AndroidManifest for exported=true activities/services/receivers/providers, and intent-filter deep links",
            "grep decompiled source for: hardcoded secrets/keys, cleartext http, WebView addJavascriptInterface, SQL string-building, file paths from intents, exported ContentProvider queries",
            "check network_security_config, certificate pinning, root/debug/emulator checks -- and how to bypass them (Frida hooks)",
            "runtime: Frida to hook crypto, auth, and the checks above; mitmproxy for the traffic; look for IDOR in the API the app calls",
            "if there's a lib*.so, pull it and run binary-recon on it",
        ],
    ),
    dict(
        name="crash-triage-exploitability",
        purpose="decide whether a crash is a security bug and what primitive it gives",
        trigger_when="a fuzzer or test produced a crash / segfault and you need to classify it",
        trigger_matcher=r"crash|segfault|SIGSEGV|exploitab|primitive|UAF|use.after.free|OOB|heap overflow|double free|type confusion|ASAN report",
        procedure=[
            "reproduce deterministically with the minimized input; run under ASAN (or gdb + exploitable/gef) for the real bug type",
            "classify: OOB read (info leak), OOB write (control), use-after-free, double free, type confusion, uninitialized use",
            "check WHAT you control: the offset, the length, the written value, the freed-then-reused object's contents",
            "note the mitigations that matter here: ASLR (need a leak), NX (need ROP/JOP), stack canary (need a leak or overwrite past it), CFI, PAC, MTE",
            "write it up as 'attacker-controlled <primitive> at <location> given <preconditions>' -- that's the finding, exploit is optional",
        ],
    ),
    dict(
        name="linux-memory-corruption",
        purpose="the primitives and the modern mitigation stack for Linux userland exploitation",
        trigger_when="developing a PoC/exploit for a memory-corruption bug on Linux",
        trigger_matcher=r"buffer overflow|stack overflow|ROP|ret2|heap exploit|tcache|GOT overwrite|one_gadget|pwntools|libc leak|canary",
        procedure=[
            "stack BOF: leak or brute the canary, then overwrite saved RIP; no gadgets in the binary -> leak libc (puts/printf GOT) then ROP in libc",
            "heap: tcache/fastbin poisoning for an arbitrary-write, UAF to overlap objects, house-of-* techniques for older glibc; check the glibc version's protections (tcache key, safe-linking)",
            "PIE -> you need a text/PIE-base leak; full RELRO -> can't overwrite GOT, target __free_hook/__malloc_hook (old glibc) or a struct with a fn pointer",
            "build with pwntools: context.binary, ROP(elf), cyclic() to find offsets, one_gadget for a single-shot libc RCE",
            "keep the PoC minimal and local -- it proves the bug for the report, it's not a deployed tool",
        ],
    ),
    dict(
        name="patch-diff-nday",
        purpose="turn a security patch into an understanding of (and a trigger for) the bug it fixed",
        trigger_when="analyzing a CVE, a security advisory, or a suspicious commit",
        trigger_matcher=r"CVE|patch diff|n-?day|advisory|security fix|bindiff|regression|1day|silent fix",
        procedure=[
            "get the vulnerable and patched versions; for source, git diff the fix commit; for binaries, BinDiff / Diaphora the two",
            "the added check IS the bug boundary -- what condition does the guard now reject that used to be allowed",
            "work backward: what input reaches that code path, and what state made the old code wrong (missing bounds check, sign confusion, TOCTOU, missing auth)",
            "write a trigger that hits the pre-patch condition; confirm it crashes/misbehaves on the old version and is clean on the new",
        ],
    ),
    dict(
        name="bounty-report",
        purpose="a report that gets triaged fast and paid fairly",
        trigger_when="writing up a vulnerability finding for a bug-bounty program or a client",
        trigger_matcher=r"bounty report|write.?up|disclosure|CVSS|severity|remediation|repro steps|impact statement",
        procedure=[
            "title: <vuln class> in <component> leading to <impact>. One line.",
            "severity: CVSS v3.1 vector + score, and a plain-English impact sentence (what an attacker gains)",
            "repro: numbered, copy-pasteable steps from a clean state; include the exact request/input and the observed vs expected result",
            "PoC: minimal, self-contained, with any account/setup noted; a short video only if the steps are UI-heavy",
            "remediation: the specific fix (parameterize the query, add the bounds check, enforce the ACL server-side), not just 'sanitize input'",
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
