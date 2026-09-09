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
        name="kotlin-is-not-c-or-java",
        purpose="the syntax mistakes a C/Java habit makes in Kotlin, and the fix",
        trigger_when="writing Kotlin and hitting compile errors on loops, conditionals, or literals",
        trigger_matcher=r"\.kt:|kotlin|Expecting|expected .* but|unresolved reference|for \(.*;|\bnew \w+\(|\?\s*:.*:",
        procedure=[
            "counted loop: `for (i in 0 until n) { }` or `for (i in a..b)` -- NOT `for (i = 0; i < n; i++)`",
            "no ternary: use `val x = if (c) a else b` or `c ?: fallback`; `when (x) { 1 -> ...; else -> ... }` for multi-branch",
            "collections are functions: `listOf(1,2)`, `mutableListOf()`, `mapOf(k to v)`, `IntArray(n)` -- no `[]` literals; index with `a[i]`",
            "no `new`: `Foo(args)` constructs; `val`/`var` not a type; nullable is `Foo?` and you must handle it (`?.`, `?:`, or check)",
            "string templates: `\"$x\"` and `\"${obj.prop}\"`; multiple statements on one line need `;` between them",
        ],
    ),
    dict(
        name="implement-one-method-then-check",
        purpose="on a scaffold with many stubbed methods, do them one at a time",
        trigger_when="a file has several TODO()/NotImplementedError stubs to fill in against tests",
        trigger_matcher=r"TODO\(\)|implement (every|all|each)|stub|NotImplementedError|method bod|scaffold|failing tests",
        procedure=[
            "pick the method the earliest/most tests depend on (constructor, deal, init, parse) -- do THAT one first",
            "write just that body; run the check; read which tests moved from error to fail-or-pass",
            "a compile error blocks ALL tests -- fix syntax before judging logic; a plain test failure is progress",
            "repeat for the next method; don't write three bodies before running the check",
        ],
    ),
    dict(
        name="many-bugs-one-file-fix-each",
        purpose="a file with several planted bugs -- each failing test is a DIFFERENT bug, not one",
        trigger_when="pytest shows 2+ failing tests across different functions and you're fixing bug bodies",
        trigger_matcher=r"failing test|[0-9]+ failed|each failing test|has bugs|fix the (function|method) bodies|planted",
        procedure=[
            "run pytest -q FIRST and list every failing test name -- there are usually 3-5, one per function",
            "fix ONE function, re-run, confirm THAT test went green, then move to the NEXT failing function",
            "never edit the same function a 3rd time -- if two attempts didn't fix it, re-read its test and the docstring word-for-word, the bug is an off-by-one / wrong operator / missing .strip()",
            "common planted bugs: `a+b <= w` should count the separator (`a+1+b`); `s[:n]+suffix` is n+len(suffix) chars; `.split(' ')` breaks on double spaces, want `.split()`; a `return s` that forgot `.strip(...)`",
            "you are NOT done until pytest prints 0 failed -- a partial fix scores partial",
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

    # --- debugging craft: turn guessing into a search ---
    dict(
        name="bisect-the-failure",
        purpose="each step should cut the suspect space roughly in half, not by one line",
        trigger_when="you have a failure but not a location, and the code is more than a screenful",
        trigger_matcher=r"where is|can't find|not sure why|somewhere in|narrow (it |down)|isolate|which (function|line|commit)",
        procedure=[
            "pick a checkpoint halfway through the suspect path; print/assert the state there",
            "the bug is on exactly one side of that checkpoint -- discard the other half",
            "repeat on the half that still fails; 3-4 cuts localizes almost anything",
            "for a regression, `git bisect` does this over commits automatically -- give it a one-command test",
        ],
    ),
    dict(
        name="minimize-the-repro",
        purpose="a 3-line failing case is debuggable; a 300-line one is not",
        trigger_when="you can trigger the bug but the case is large or noisy",
        trigger_matcher=r"minim|reduce|smallest (case|input|repro)|strip (it |down)|delta debug|shrink",
        procedure=[
            "delete half the input / config / setup; if it still fails, keep going; if it passes, restore and cut elsewhere",
            "replace dependencies with constants (fixed clock, hardcoded response) until only the buggy bit is live",
            "stop when nothing else can be removed without the failure disappearing -- that residue is the bug",
        ],
    ),
    dict(
        name="read-the-stack-trace-bottom-up",
        purpose="a stack trace names the failure site and the path to it -- read both ends",
        trigger_when="an exception, panic, or traceback was printed",
        trigger_matcher=r"traceback|stack trace|at .*\(.*:\d+\)|panic:|\bthrew\b|caused by|  File \"",
        procedure=[
            "the innermost frame (Python: last line; JVM/JS: top) is WHERE it broke -- open that file:line",
            "the outermost frame your code owns is WHY it was called -- the bad argument usually originates near there",
            "'Caused by:' / 'During handling of the above' -- the LAST cause is the real one, the rest are wrappers",
            "the exception TYPE + message already narrows it: NullPointer/AttributeError = a None slipped through a boundary",
        ],
    ),
    dict(
        name="change-one-thing",
        purpose="two changes at once means you can't attribute the result to either",
        trigger_when="a fix didn't work and you're tempted to try several things",
        trigger_matcher=r"still (failing|broken)|didn't (work|help)|try (something|another)|nothing works|multiple",
        procedure=[
            "revert to the last known state; make exactly one change with a clear hypothesis",
            "run the check; record what happened against the hypothesis (confirmed / refuted / no signal)",
            "keep the change only if it helped; otherwise revert it before the next attempt",
        ],
    ),
    dict(
        name="when-stuck-widen-then-narrow",
        purpose="repeating the same failing move is not progress -- change the altitude",
        trigger_when="the same action or edit has failed 2+ times",
        trigger_matcher=r"stuck|loop|again|same (error|result)|third time|going in circles|not converging",
        procedure=[
            "stop editing; re-read the actual error and the code around the failure site fresh",
            "state the assumption that must be wrong for this to keep failing, then check it directly (grep, a print, the docs)",
            "if still stuck, try the SMALLEST possible version of the change, verify that, then grow it",
        ],
    ),

    # --- tests as the ground truth ---
    dict(
        name="failing-test-first",
        purpose="a red test that pins the bug turns 'is it fixed?' into a yes/no",
        trigger_when="fixing a reported bug that has no test covering it",
        trigger_matcher=r"no test|add a test|repro.*test|regression test|cover this|TDD",
        procedure=[
            "write the smallest test that asserts the correct behavior for the reported case -- watch it FAIL for the right reason",
            "make the fix; the new test goes green and every existing test stays green",
            "add one or two boundary cases around the same code while you're there",
        ],
    ),
    dict(
        name="test-the-boundaries",
        purpose="bugs cluster at the edges: 0, 1, empty, full, negative, max, off-by-one",
        trigger_when="writing tests or reasoning about where an implementation is wrong",
        trigger_matcher=r"edge case|boundary|off.by.one|empty|zero|negative|overflow|corner case|\bnull\b|first or last",
        procedure=[
            "for a range/loop: test length 0, 1, 2, and the max; check the first and last element land right",
            "for numbers: 0, -1, the max value, and the value that causes a carry/overflow",
            "for containers: empty, one element, duplicates, and the not-found case",
            "an off-by-one shows up as the boundary case failing while the middle passes",
        ],
    ),
    dict(
        name="dont-edit-the-test-to-pass",
        purpose="changing the assertion to match a wrong result hides the bug, doesn't fix it",
        trigger_when="a test fails and the fix that comes to mind is to change the test",
        trigger_matcher=r"change the (test|assert)|update the expected|the test is wrong|adjust the expectation",
        procedure=[
            "assume the test is the spec; make the implementation satisfy it",
            "only touch a test if you can state exactly why its expectation is provably wrong AND the task allows it",
            "if the test really is wrong, fix it in its own step with a note -- don't fold it into the code fix",
        ],
    ),

    # --- reading code you didn't write ---
    dict(
        name="trace-the-data-flow",
        purpose="follow one value from where it enters to where it's used -- bugs live on that path",
        trigger_when="understanding how a system handles a particular input or field",
        trigger_matcher=r"how does .* (get|flow|reach)|where is .* (set|used|passed)|data flow|from .* to|call (path|chain)",
        procedure=[
            "grep for the field/param name; find where it's first assigned from outside (request, file, argv, env)",
            "follow each assignment and call forward -- note every place it's validated, transformed, or stored",
            "the sink is where it's finally acted on (query, write, render, exec) -- check what protects it just before that",
        ],
    ),
    dict(
        name="entry-points-first",
        purpose="find the few places the outside world calls in before reading everything",
        trigger_when="starting on an unfamiliar codebase or a large module",
        trigger_matcher=r"unfamiliar|new (codebase|repo|project)|where do I start|orient|overview|main\(|route|handler|endpoint",
        procedure=[
            "locate main / the router / the CLI parser / the test files -- these frame what the code is FOR",
            "repo_map or a tree of the source dirs; note the 3-5 files that everything imports",
            "read one full request/command path end to end before touching anything",
        ],
    ),

    # --- safe change ---
    dict(
        name="characterize-before-refactor",
        purpose="you can't safely restructure code whose current behavior isn't captured",
        trigger_when="refactoring or restructuring code that has thin or no tests",
        trigger_matcher=r"refactor|restructure|clean up|extract|rename|move .* (to|into)|tidy|reorganize",
        procedure=[
            "add tests that lock in what the code does NOW (even quirks) -- run them green",
            "refactor in small steps, re-running those tests after each; a red means you changed behavior",
            "only change behavior deliberately, in a separate commit, with the test updated to match",
        ],
    ),
    dict(
        name="keep-the-tree-green",
        purpose="never stack a new change on top of a broken build",
        trigger_when="partway through a multi-step change",
        trigger_matcher=r"multi.?step|several (files|changes)|next I'll|then I'll|step \d|partway",
        procedure=[
            "after each edit, run the fastest check that would catch a break (compile, lint, the nearest test)",
            "if it's red, fix that before the next edit -- don't pile on",
            "commit at each green point so you always have somewhere to fall back to",
        ],
    ),

    # --- performance ---
    dict(
        name="measure-before-optimizing",
        purpose="the slow part is almost never where you'd guess -- profile first",
        trigger_when="asked to make something faster or reduce resource use",
        trigger_matcher=r"slow|optim|performance|faster|speed ?up|latency|too long|profil|bottleneck|hot ?path",
        procedure=[
            "reproduce the slowness with a timer around the whole operation -- get a baseline number",
            "profile it (cProfile / perf / a sampling profiler) or bisect with timers to find the dominant cost",
            "fix only the top cost, re-measure against the baseline, stop when it's good enough",
        ],
    ),
    dict(
        name="the-usual-perf-suspects",
        purpose="most real slowness is one of a short list of patterns",
        trigger_when="looking for why a loop, request, or job is slow",
        trigger_matcher=r"N\+1|in a loop|per (row|item|request)|repeated (query|call)|allocat|O\(n\^?2\)|quadratic|blocking",
        procedure=[
            "a query / network call / file open INSIDE a loop -> batch it or hoist it out",
            "quadratic scan (list `in` inside a loop) -> use a set/dict for the lookup",
            "re-parsing / re-compiling / re-connecting each call -> cache or reuse the handle",
            "sync I/O on the hot path -> make it async or move it off the request",
        ],
    ),

    # --- concurrency ---
    dict(
        name="shared-mutable-state-is-the-bug",
        purpose="intermittent, load-dependent, 'works when I step through it' = a data race",
        trigger_when="a bug is flaky, timing-dependent, or only shows under concurrency",
        trigger_matcher=r"race|flaky|intermittent|sometimes|only under load|non.?determin|thread|concurren|async.*bug",
        procedure=[
            "list every piece of state touched by more than one thread/task/request",
            "for each: is every read+write of it under the same lock, atomic, or confined to one owner?",
            "the fix is usually: make it immutable, give it one owner, or guard the whole read-modify-write (not just the write)",
        ],
    ),
    dict(
        name="lock-ordering-prevents-deadlock",
        purpose="a deadlock is two code paths taking the same two locks in opposite orders",
        trigger_when="the program hangs with no CPU use, or you're adding a second lock",
        trigger_matcher=r"deadlock|hang|frozen|stuck (waiting|acquiring)|two locks|lock order|mutex.*mutex",
        procedure=[
            "dump the stacks of all threads/tasks -- two of them blocked on acquire() is a deadlock",
            "define ONE global order for locks (e.g. always A before B) and make every path follow it",
            "better: hold one lock at a time; do the second phase after releasing the first",
        ],
    ),

    # --- python footguns ---
    dict(
        name="python-mutable-default-arg",
        purpose="def f(x=[]) shares that one list across every call",
        trigger_when="writing or reviewing a Python function with a list/dict/set default",
        trigger_matcher=r"def .*=\s*(\[\]|\{\}|set\(\))|mutable default|shared .* between calls|accumulat",
        procedure=[
            "use `x=None` in the signature and `if x is None: x = []` in the body",
            "same trap for default values captured at def-time (datetime.now(), a config lookup)",
        ],
    ),
    dict(
        name="float-equality-and-money",
        purpose="0.1 + 0.2 != 0.3; never == two floats, never store money as float",
        trigger_when="comparing non-integer numbers or handling currency",
        trigger_matcher=r"float|0\.1|round|== .*\d\.\d|money|currency|price|cents|decimal|approx",
        procedure=[
            "compare with a tolerance: abs(a - b) < 1e-9  (pytest.approx in tests)",
            "for money use integer cents, or decimal.Decimal -- never binary float",
            "round only at the display boundary, and pick the rounding mode deliberately",
        ],
    ),
    dict(
        name="python-truthiness-vs-none",
        purpose="`if not x` also fires on 0, '', [], {} -- often not what you meant",
        trigger_when="checking whether an optional value was provided",
        trigger_matcher=r"if not \w+:|if \w+:|is None|default.*fell|falsy|truthy|empty (string|list) treated",
        procedure=[
            "mean 'was it passed'? -> `if x is None`",
            "mean 'is it empty'? -> `if not x` (and know 0 and '' count as empty)",
            "a function that can return a falsy-but-valid value must be distinguished from 'not found'",
        ],
    ),

    # --- C / C++ building ---
    dict(
        name="c-compile-basics",
        purpose="the gcc/clang invocation for a small C/C++ program or test",
        trigger_when="building a C or C++ file from the command line",
        trigger_matcher=r"\bgcc\b|\bclang\b|\bg\+\+\b|clang\+\+|\.c\b|\.cpp\b|\.cc\b|compile .* (c|cpp)|-std=|-o \w",
        procedure=[
            "single file: `cc -std=c11 -g -O1 -Wall -Wextra -fsanitize=address,undefined main.c -o main` (use clang for -fsanitize=fuzzer)",
            "multiple files: compile each to an object (`cc -c a.c -o a.o`) then link (`cc a.o b.o -o app`); headers are #included, never compiled",
            "`-I dir` adds a header search path; `-L dir -lfoo` links libfoo; `-D NAME=val` sets a macro",
            "C++ needs g++/clang++ (not gcc) so the standard library links; pick `-std=c++17` unless told otherwise",
            "keep `-g` and a sanitizer on while developing -- a clean ASAN run is the bar, not just 'it compiled'",
        ],
    ),
    dict(
        name="c-read-linker-errors",
        purpose="'undefined reference' and 'multiple definition' are link errors, not compile errors",
        trigger_when="a C/C++ build fails after the files compiled",
        trigger_matcher=r"undefined reference|multiple definition|ld returned|ld:|collect2|cannot find -l|relocation|__imp_",
        procedure=[
            "'undefined reference to X' -> the .c/.o that DEFINES X isn't in the link line, or a `-lLIB` is missing; add it",
            "'multiple definition of X' -> a non-inline function/variable defined in a header that's included twice; move the definition to one .c, leave a declaration in the header",
            "'cannot find -lfoo' -> the library isn't installed or needs `-L` pointing at its directory",
            "link order matters with static libs: list them AFTER the objects that use them",
        ],
    ),
    dict(
        name="c-build-systems",
        purpose="drive a Makefile or a CMake project instead of hand-compiling",
        trigger_when="a C/C++ repo has a Makefile, CMakeLists.txt, or configure script",
        trigger_matcher=r"Makefile|makefile|CMakeLists|cmake|\.\/configure|make (all|check|test)|autoreconf|meson",
        procedure=[
            "Makefile: `make` builds, `make test` / `make check` runs tests, `make clean` resets; `make -j` parallelizes",
            "CMake: `cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug` then `cmake --build build`; tests via `ctest --test-dir build`",
            "to add sanitizers to someone's build: `make CFLAGS='-g -O1 -fsanitize=address,undefined'` or `-DCMAKE_C_FLAGS=...`",
            "autotools: `./configure && make`; if configure is missing run `autoreconf -i` first",
        ],
    ),
    dict(
        name="c-header-hygiene",
        purpose="what goes in a .h vs a .c, and the include guard",
        trigger_when="creating or editing a C/C++ header, or hitting redefinition / incomplete-type errors",
        trigger_matcher=r"\.h\b|#include|include guard|#ifndef|#pragma once|redefinition|incomplete type|forward declar",
        procedure=[
            "header holds: declarations (prototypes), typedefs/structs, macros, `extern` globals -- NOT function bodies or global definitions",
            "guard every header: `#ifndef FOO_H` / `#define FOO_H` / ... / `#endif`  (or `#pragma once`)",
            "a .c includes its own .h first, then others; a C header used from C++ needs `extern \"C\" { }` around the declarations",
            "'incomplete type' -> you only forward-declared a struct; include the header that fully defines it",
        ],
    ),

    # --- C / C++ footguns ---
    dict(
        name="c-off-by-one-and-nul",
        purpose="buffer sizes, loop bounds, and the string terminator are where C bugs live",
        trigger_when="writing or auditing C/C++ that copies into or indexes a buffer",
        trigger_matcher=r"\bchar \w+\[|memcpy|strcpy|strncpy|strcat|snprintf|buf\[|\bi <=|\bi <|\+ 1\b|null.?termin",
        procedure=[
            "a buffer of N holds indices 0..N-1; `for (i=0; i<=N; i++)` writes one past the end",
            "strncpy does NOT nul-terminate if the source fills the buffer -- set buf[n-1]=0 yourself",
            "a string of length L needs L+1 bytes; sizeof on a pointer is the pointer size, not the buffer",
            "the copy length must be checked against the DESTINATION capacity, not the source length",
        ],
    ),
    dict(
        name="c-integer-overflow-before-alloc",
        purpose="len1 + len2 or count * size can wrap, then you allocate too little",
        trigger_when="a size/length for malloc/memcpy is computed from input values",
        trigger_matcher=r"malloc\(|calloc\(|alloca|\* size|len \+|count \*|size_t|integer overflow|realloc",
        procedure=[
            "any arithmetic on an attacker-influenced size can overflow -- check for wrap BEFORE the allocation",
            "use calloc(n, size) (it checks n*size) or an explicit `if (a > SIZE_MAX - b) fail`",
            "signed length compared with `<` can be negative -> passes the check, then huge as size_t",
        ],
    ),
    dict(
        name="c-lifetime-and-ownership",
        purpose="use-after-free and double-free come from unclear ownership of a pointer",
        trigger_when="writing C/C++ that frees, returns, or stores a pointer",
        trigger_matcher=r"\bfree\(|delete |dangling|use.after.free|double free|owns|lifetime|return &|out of scope",
        procedure=[
            "for every allocation name exactly one owner responsible for freeing it, once",
            "set the pointer to NULL right after free() so a stray reuse crashes instead of corrupting",
            "never return the address of a local; a freed struct's function pointers are an exploit primitive",
        ],
    ),

    # --- kotlin / android depth ---
    dict(
        name="kotlin-nullability-at-the-edge",
        purpose="platform types from Java/JNI/Intent extras are the NPE source in Kotlin",
        trigger_when="handling values that come from Java APIs, Bundles, JSON, or the system",
        trigger_matcher=r"NullPointerException|!!|platform type|getStringExtra|Bundle|nullable|\?\.|lateinit.*not initialized",
        procedure=[
            "treat every value crossing into Kotlin from Java/Android as nullable; handle null explicitly at that line",
            "avoid `!!` -- use `?:` with a real default or an early return",
            "'lateinit property has not been initialized' = you read it before onCreate/setup ran -- guard with ::x.isInitialized or restructure",
        ],
    ),
    dict(
        name="android-no-work-on-main-thread",
        purpose="file/network/db/heavy work on the UI thread = jank or ANR",
        trigger_when="adding I/O, parsing, or a loop to an Activity/View/callback",
        trigger_matcher=r"ANR|jank|NetworkOnMainThread|StrictMode|UI thread|runOnUiThread|Dispatchers|AsyncTask|freeze",
        procedure=[
            "move the work to a background thread / coroutine (Dispatchers.IO); post only the result back to the UI",
            "onDraw / onTouchEvent / onBindViewHolder must be allocation-free and fast -- no parsing, no I/O",
            "for a game loop use a dedicated thread or Choreographer, not a tight loop on main",
        ],
    ),

    # --- API / web design ---
    dict(
        name="validate-at-the-boundary",
        purpose="check and normalize untrusted input once, where it enters -- then trust it inside",
        trigger_when="adding a request handler, CLI arg, config load, or message consumer",
        trigger_matcher=r"request|payload|param|user input|deserializ|parse .* (body|json)|validate|sanitize|endpoint",
        procedure=[
            "parse into a typed structure at the edge; reject anything that doesn't fit with a clear error",
            "range/format/length/enum checks happen here, not scattered through the business logic",
            "canonicalize now (trim, lowercase host, resolve path) so downstream comparisons are safe",
        ],
    ),
    dict(
        name="make-writes-idempotent",
        purpose="clients retry; a POST that ran twice shouldn't double-charge",
        trigger_when="designing an endpoint or job that creates or mutates state",
        trigger_matcher=r"idempoten|retry|exactly once|duplicate (request|charge|row)|POST|at.least.once|dedupe",
        procedure=[
            "accept a client-supplied idempotency key; on a repeat key return the first result, don't re-run",
            "or make the operation naturally idempotent (upsert by natural key, set-not-add)",
            "guard the check-then-act with a unique constraint or a lock so two concurrent tries can't both pass",
        ],
    ),

    # --- security: web classes worth their own card ---
    dict(
        name="toctou-in-web-logic",
        purpose="check-then-act with a gap lets a concurrent request slip between the two",
        trigger_when="auditing balance checks, coupon/quota redemption, or 'first one wins' logic",
        trigger_matcher=r"race condition|TOCTOU|check.*then|balance|coupon|quota|redeem|withdraw|double.spend|limit.*exceeded",
        procedure=[
            "find code that reads a value, decides, then writes -- with no lock/transaction spanning both",
            "fire the request many times in parallel (Turbo Intruder / a small script) and watch for over-redemption",
            "the fix is an atomic DB update with a WHERE guard, SELECT ... FOR UPDATE, or a unique constraint",
        ],
    ),
    dict(
        name="jwt-and-session-pitfalls",
        purpose="token bugs: alg confusion, no expiry check, weak secret, no revocation",
        trigger_when="testing or reviewing JWT / bearer-token / session authentication",
        trigger_matcher=r"jwt|bearer|alg.*none|HS256|RS256|token.*expir|session (fixation|token)|kid|refresh token",
        procedure=[
            "try alg:none and alg swap (RS256->HS256 signing with the public key as the HMAC secret)",
            "check exp/nbf are actually enforced; try an old token, a token with exp removed",
            "crack HS256 with a wordlist (hashcat -m 16500); check the token still works after 'logout' / password change",
        ],
    ),
    dict(
        name="ssrf-and-url-parsing",
        purpose="a server fetch of a user URL reaches internal hosts, cloud metadata, and localhost",
        trigger_when="code takes a URL/host from input and makes a request to it",
        trigger_matcher=r"ssrf|fetch.*url|requests\.get\(.*input|webhook|url=|proxy|import.*from url|render.*remote|169\.254",
        procedure=[
            "targets: 169.254.169.254 (cloud metadata), 127.0.0.1 / localhost, 10./172.16./192.168. ranges, ::1",
            "bypasses to try: DNS rebinding, redirect to an internal host, IP encodings (decimal, octal, IPv6-mapped), @ in the userinfo",
            "the fix is an allowlist of destination hosts + blocking redirects + resolving then re-checking the IP",
        ],
    ),
    dict(
        name="command-and-arg-injection",
        purpose="user data in a shell string is RCE; even argv can inject flags",
        trigger_when="code builds a command line, calls system/popen/exec, or passes input as an argument",
        trigger_matcher=r"os\.system|subprocess.*shell=True|popen|exec\w*\(|`.*\$|backtick|shell (out|command)|Runtime\.exec",
        procedure=[
            "shell=True with any input -> `;`, `|`, `$()`, backticks all execute -- switch to an argv list, no shell",
            "even argv: input starting with `-` can be read as a flag (`--output=/etc/x`) -- put `--` before user args",
            "for a filename argument, also block path traversal and absolute paths",
        ],
    ),
    dict(
        name="xxe-and-unsafe-parsers",
        purpose="XML/YAML/pickle parsers execute or fetch by default in many libs",
        trigger_when="code parses XML, YAML, or deserializes an object format from untrusted input",
        trigger_matcher=r"XXE|lxml|etree|xml\.|yaml\.load\b|pickle\.load|unserialize|readObject|SnakeYAML|DocumentBuilder|ENTITY",
        procedure=[
            "XML: disable DTDs / external entities (defusedxml in Python, setFeature disallow-doctype-decl in Java)",
            "YAML: yaml.safe_load, never yaml.load; SnakeYAML with a restricted constructor",
            "pickle / native deserialization of untrusted bytes is RCE by design -- use JSON or a schema'd format",
        ],
    ),
    dict(
        name="secrets-in-git-history",
        purpose="a removed key still lives in every prior commit and every clone",
        trigger_when="recon on a repo, or you spot a credential in the code",
        trigger_matcher=r"secret|api.?key|password|token|credential|\.env|git log -p|trufflehog|gitleaks|AKIA|-----BEGIN",
        procedure=[
            "scan full history: gitleaks detect / trufflehog git file://. -- not just the working tree",
            "check the provider (AWS/GCP/GitHub/Slack) for whether the key is live before reporting it as valid",
            "remediation in the report: rotate the secret first, THEN rewrite history / use the platform's secret scanning",
        ],
    ),
    dict(
        name="dependency-and-cve-check",
        purpose="the vuln is often in a pinned transitive dependency, not the app code",
        trigger_when="auditing a project, or a lockfile / SBOM is in scope",
        trigger_matcher=r"dependenc|CVE|lockfile|package-lock|requirements\.txt|pom\.xml|go\.mod|osv|npm audit|SBOM|outdated",
        procedure=[
            "run osv-scanner / npm audit / pip-audit against the lockfile -- get exact vulnerable versions",
            "for each hit, confirm the vulnerable code path is actually reachable from this app before rating it",
            "check for a known exploit / PoC for that CVE to demonstrate impact in the report",
        ],
    ),

    # --- agent meta: how to behave in a battle ---
    dict(
        name="state-the-plan-then-work-it",
        purpose="a 3-5 step plan up front keeps a long task from wandering",
        trigger_when="a task has multiple parts or will take many steps",
        trigger_matcher=r"implement|build|add .* feature|several (things|parts)|first.*then|plan|multi",
        procedure=[
            "write the ordered steps before touching code; put the riskiest / most-uncertain one first",
            "do one step, verify it, then move on -- adjust the plan if a step teaches you something",
            "if you've done 5+ actions with no progress on the current step, stop and re-plan",
        ],
    ),
    dict(
        name="use-exact-names-from-the-source",
        purpose="guessing an identifier wastes a compile cycle -- copy it from the file",
        trigger_when="calling a function, constructing a type, or importing from code you've read",
        trigger_matcher=r"undeclared|undefined|not defined|no attribute|unresolved reference|cannot find symbol|NameError|ImportError",
        procedure=[
            "before writing a call, open the definition and copy the name, argument order, and types verbatim",
            "'undefined/undeclared X' means the name is wrong or unimported -- grep the headers/modules for the real spelling",
            "don't invent a plausible name (foo_t, getFoo) -- the codebase has one true name",
        ],
    ),
    dict(
        name="finish-what-you-changed",
        purpose="a half-applied change that breaks the build is worse than no change",
        trigger_when="a step turned out bigger than expected or you're running low on budget",
        trigger_matcher=r"running low|out of (time|steps|budget)|bigger than|half|incomplete|revert|leave it",
        procedure=[
            "get back to a compiling, test-passing state even if the feature is smaller than planned",
            "if you can't finish cleanly, revert this step's edits rather than leave the tree broken",
            "say plainly what's done and what's left -- don't claim done when a check is red",
        ],
    ),
    dict(
        name="docker-sandbox-untrusted-code",
        purpose="run a bug-bounty target / unknown binary / scraped repo without giving it your box",
        trigger_when="about to build or execute code from an external target, sample, or untrusted repo",
        trigger_matcher=r"untrusted|sandbox|target (binary|app|apk|sample)|isolate|run (it|this) safely|scraped|unknown (code|binary)|malware|sample",
        procedure=[
            "`docker run --rm -it --network none -v \"$PWD\":/work:ro -w /work <image> <cmd>` -- --rm throws the container away, --network none cuts it off, :ro keeps it from writing your files",
            "for a build that must write, mount a scratch dir: `-v \"$PWD/out\":/out` and write only there",
            "add `--pids-limit 512 --memory 2g --cpus 2` so a fork bomb / OOM in the target can't take the host down",
            "never `-v /:/host` or `--privileged` or `-v /var/run/docker.sock` for untrusted code -- that's a full escape",
            "when done: `docker image rm <image>` if you pulled it just for this",
        ],
    ),
    dict(
        name="dockerfile-reproducible-build",
        purpose="a Dockerfile that builds the same next month and doesn't rebuild the world on every edit",
        trigger_when="writing or fixing a Dockerfile",
        trigger_matcher=r"Dockerfile|FROM \w|docker build|base image|multi-stage|layer cache",
        procedure=[
            "pin the base image by digest or an exact tag (`python:3.12.7-slim`, not `python:latest` or `python:3`)",
            "COPY the dependency manifest and install deps BEFORE COPYing the source -- an app-code change then reuses the cached deps layer",
            "one logical step per RUN, chained with `&&`, cleaning up in the SAME RUN (`apt-get ... && rm -rf /var/lib/apt/lists/*`) or the cruft is baked into the layer",
            "multi-stage: a `builder` stage with the toolchain, then `COPY --from=builder` only the artifact into a slim runtime stage that runs as a non-root `USER`",
            "add a .dockerignore (.git, node_modules, __pycache__, build/) so the build context isn't huge",
        ],
    ),
    dict(
        name="docker-debug-a-failing-container",
        purpose="a container that exits, hangs, or can't reach a service -- find out why fast",
        trigger_when="`docker run` / `docker compose up` exits non-zero, restarts, or the app inside is unreachable",
        trigger_matcher=r"docker (run|compose|logs|exec|ps)|container (exit|restart|crash|unhealthy)|Exited \(\d+\)|CrashLoopBackOff|cannot connect|connection refused.*container",
        procedure=[
            "`docker ps -a` -> the STATUS column: 'Exited (0)' = it finished on purpose (wrong CMD, or a one-shot); 'Exited (1/137/139)' = crash/OOM-kill/segfault",
            "`docker logs <name>` (add `--tail 50 -f`) for the actual error -- the run command usually swallows it",
            "shell in on a running one: `docker exec -it <name> sh`; on a dead one: `docker run --rm -it --entrypoint sh <image>` and reproduce by hand",
            "'connection refused' between containers: they must share a user-defined network and you connect by SERVICE NAME + the container's INTERNAL port, not localhost, not the published port",
            "137 = OOM-killed -> raise `--memory` or fix the leak; 139 = segfault in the process",
        ],
    ),
    dict(
        name="docker-compose-local-stack",
        purpose="stand up a multi-service dev stack (app + db + cache) that actually talks to itself",
        trigger_when="you need more than one service running together locally for a test or demo",
        trigger_matcher=r"docker[- ]compose|compose\.ya?ml|multi-service|app \+ (db|database|redis|postgres)|stack",
        procedure=[
            "one service per block; `depends_on` orders startup but does NOT wait for readiness -- add a healthcheck and `condition: service_healthy` if the app needs the db up",
            "services reach each other at `http://<service-name>:<internal-port>` on the default compose network -- no `ports:` needed for internal-only traffic",
            "`ports:` is host:container and only for what YOU hit from the host; keep the db's off unless you need a client",
            "config via `environment:` / `env_file:`; persist data with a named `volumes:` entry, not a bind mount, for db state",
            "`docker compose up --build`, `docker compose logs -f <svc>`, `docker compose down -v` to wipe volumes too",
        ],
    ),

    # --- Linux / Manjaro the box actually runs ---
    dict(
        name="arch-manjaro-specifics",
        purpose="this box is Arch-family (Manjaro), not Debian -- the tools and rules differ",
        trigger_when="anything about packages, updates, or system config on this machine",
        trigger_matcher=r"pacman|makepkg|\bAUR\b|manjaro|arch linux|yay\b|PKGBUILD|/etc/pacman|mkinitcpio|pamac",
        procedure=[
            "packages: `pacman -Q` installed, `pacman -Qo <file>` who owns it, `pacman -Ql <pkg>` its files, `pacman -Si` info -- NO apt/dpkg",
            "AUR is user-submitted: read the PKGBUILD and the .install before `yay -S`; an AUR package can run arbitrary code at build time",
            "rolling release: NEVER partial-upgrade (`pacman -Sy <one pkg>`) -- it half-updates the system and breaks it; only ever `pacman -Syu` (see [[manjaro-full-upgrade-only]])",
            "known-vulnerable installed packages: `arch-audit` lists CVEs for what's installed; `pacman -Qu` shows what an upgrade would change",
            "config lives in /etc as plain files + systemd units; `journalctl -xe`, `systemctl --user` for user services like gremlin",
        ],
    ),
    dict(
        name="linux-privesc-enumeration",
        purpose="the standard first sweep for a local privilege-escalation path on a Linux host",
        trigger_when="you have a shell on a Linux box and want to know how to become root",
        trigger_matcher=r"privesc|privilege escalation|local root|SUID|sudo -l|linpeas|GTFOBins|capabilit|become root",
        procedure=[
            "SUID/SGID: `find / -perm -4000 -type f 2>/dev/null` -- cross-ref each against GTFOBins for a known escape",
            "`sudo -l` -- any NOPASSWD entry or a binary with a GTFOBins shell escape is game over; `id` for group membership (docker/lxd/disk/wheel = root-equivalent)",
            "capabilities: `getcap -r / 2>/dev/null` -- cap_setuid / cap_dac_override on a scriptable binary is a path",
            "writable stuff root runs: cron (`/etc/cron*`), systemd units + timers (`systemctl list-timers`, unit-file perms), PATH entries you can write",
            "kernel + files: `uname -a` -> search LPE for that exact version; check `/etc/passwd` / `/etc/shadow` perms; run linpeas.sh to catch what a manual pass missed, then verify each hit by hand",
        ],
    ),

    # --- bug bounty: recon + the bugs that actually pay ---
    dict(
        name="web-recon-map-the-target",
        purpose="before testing anything, build the full picture of what's in scope and reachable",
        trigger_when="starting recon on a web / API / cloud bug-bounty target",
        trigger_matcher=r"recon|subdomain|asset discovery|attack surface|amass|subfinder|httpx|content discovery|enumerat",
        procedure=[
            "re-read the scope doc (see [[scope-first]]); list every in-scope domain, wildcard, IP range, mobile app, and repo -- test NOTHING outside it",
            "subdomains: crt.sh + subfinder/amass; resolve them; `httpx` for which are live, their titles, tech, status codes",
            "endpoints: wayback/gau + `katana`/hakrawler for URLs; pull and read every JS bundle for API routes, param names, and leaked keys",
            "fingerprint: server, framework, CDN/WAF, auth scheme, GraphQL (`/graphql` + introspection), Swagger/OpenAPI docs",
            "note the interesting surface: auth flows, file upload, anything that fetches a URL, admin/internal hostnames, staging, S3 buckets, `.git`/`.env`/backup files",
        ],
    ),
    dict(
        name="idor-and-broken-object-auth",
        purpose="the single most common bounty finding -- an object reference the server doesn't authorize",
        trigger_when="testing any endpoint that takes an id, or an app that fetches user-specific data",
        trigger_matcher=r"\bIDOR\b|BOLA|broken (object|access)|authoriz|/api/.*/\d+|user_?id|account_?id|tenant|multi.?tenant",
        procedure=[
            "make two accounts (A and B). do an action as A that references an object by id (numeric, UUID, hash, filename, email)",
            "replay A's request but swap in B's object id -- if you get B's data or change B's state, that's the bug",
            "try it on every verb (GET/PUT/PATCH/DELETE) and on nested resources; a read-only IDOR and a write IDOR are different severities",
            "check indirect refs too: predictable filenames, sequential invoice numbers, `?export=`, batch/GraphQL endpoints that skip the per-object check",
            "confirm impact with B's own view (the data really changed), then stop -- don't enumerate other users' real data (see [[bounty-report]])",
        ],
    ),
    dict(
        name="android-app-attack-surface",
        purpose="the parts of an Android app another app or a link can reach -- where the bugs are",
        trigger_when="assessing an Android app (carrier app, Google app, any APK) for vulnerabilities",
        trigger_matcher=r"exported|intent-filter|deep link|content provider|WebView|android:exported|am start|pending intent|content://",
        procedure=[
            "AndroidManifest: every `android:exported=\"true\"` activity/service/receiver/provider, and every `<intent-filter>` scheme/host (deep links) -- these take input from ANY app",
            "hit exported activities with `adb shell am start` + crafted extras / deep-link URIs; look for auth bypass (a login-gated screen opened directly), injection, or a crash",
            "ContentProviders: `content query --uri content://<authority>/...` for readable data; check for SQL injection in the selection arg and path traversal in file-backed providers",
            "WebViews: `setJavaScriptEnabled` + `addJavascriptInterface` reachable from a loaded URL = RCE-ish; `file://` access + a deep-link-controlled URL = local file read",
            "PendingIntents handed to other apps with a mutable base intent; implicit intents carrying sensitive data; check `exported` receivers for broadcast injection",
        ],
    ),
    dict(
        name="secrets-in-mobile-apps",
        purpose="APKs ship their secrets -- pull them before doing anything dynamic",
        trigger_when="you have an APK and want its API keys, endpoints, and hidden config",
        trigger_matcher=r"jadx|apktool|strings .*apk|hardcoded|api key|firebase|google_api_key|BuildConfig|resources.arsc",
        procedure=[
            "`jadx` the apk; grep decompiled source + `res/values/strings.xml` + `assets/` for: api_key, secret, token, password, `https://` base URLs, `-----BEGIN`",
            "Firebase: a `google-services.json` / `firebaseio.com` URL -> test `<db>.firebaseio.com/.json` for world-readable data; check Firestore/Storage rules",
            "`BuildConfig` fields, obfuscated string tables (run the app + Frida to dump them decrypted), native `.so` strings",
            "check the keys' scope before reporting -- a Google Maps browser key restricted to the app is not a finding; an unrestricted cloud key or a private API secret is",
        ],
    ),
    dict(
        name="hardened-targets-realism",
        purpose="know which parts of a big target a solo researcher can realistically find bugs in",
        trigger_when="scoping work on Pixel, Android, a carrier, or any heavily-tested product",
        trigger_matcher=r"pixel|tensor|titan|baseband|trustzone|secure ?element|bootloader|firmware|carrier|verizon|at&t|spectrum",
        procedure=[
            "out of realistic reach solo: baseband, TrustZone/StrongBox, bootloader, the Linux kernel core, anything Google/Qualcomm fuzz continuously -- these need a team and years of specialization",
            "where the wins are: app-layer bugs (see [[android-app-attack-surface]], [[secrets-in-mobile-apps]]), web/API/misconfig on the target's infra (see [[web-recon-map-the-target]], [[idor-and-broken-object-auth]]), n-day on unpatched devices",
            "open-source components in scope with real source: write a fuzz harness (see [[fuzz-harness]]) -- that's a solo-doable memory-bug path",
            "Android monthly bulletin: diff a recent patch (see [[patch-diff-nday]]); a device behind on updates is exploitable with that n-day",
            "pick ONE narrow surface and go deep; a broad shallow sweep of a hardened target finds nothing",
        ],
    ),

    # --- building small tools for the phone ---
    dict(
        name="termux-tool-building",
        purpose="build a script/CLI tool that runs in Termux on the phone",
        trigger_when="asked to build something that runs in Termux / on Android from the command line",
        trigger_matcher=r"termux|on my phone|android cli|android shell|\$PREFIX|pkg install",
        procedure=[
            "Termux is a real Linux-ish userland: Python 3, bash, coreutils, git all work via `pkg install` -- a pure Python or shell tool needs no cross-compiling",
            "paths are not FHS: home is `~` (`/data/data/com.termux/files/home`), binaries in `$PREFIX/bin`; never hardcode `/usr` or `/tmp` -- use `$PREFIX` and `$TMPDIR`",
            "no systemd, no root by default; for scheduled runs use `termux-job-scheduler` or cron via `termux-services`; phone-specific features need the `termux-api` package + the Termux:API app",
            "build + test it on the desktop first (it's the same Python), keep deps to the stdlib or pure-Python packages, then it drops straight into Termux",
            "for a native (C) tool, that needs the Termux NDK cross-toolchain -- not set up here; stick to script tools unless mickey installs it",
        ],
    ),

    # --- rootkits / malware: detect, contain, clean (defensive) ---
    dict(
        name="linux-rootkit-detection",
        purpose="tell whether a Linux box is rootkitted, and at what layer",
        trigger_when="a machine is behaving oddly -- hidden processes, unexplained traffic, tampered binaries",
        trigger_matcher=r"rootkit|compromis|backdoor|hidden process|LD_PRELOAD|ld\.so\.preload|kallsyms|infected|malware.*linux",
        procedure=[
            "userland rootkit: `cat /etc/ld.so.preload` (should not exist), `env | grep LD_`, compare `ls` vs `echo *`, `ps` vs `ls /proc/[0-9]*`, `ss -tlnp` vs `/proc/net/tcp` -- mismatches = something hooking libc",
            "kernel/LKM rootkit: `lsmod` vs `cat /proc/modules` vs `ls /sys/module`, `dmesg | grep -i taint`, `cat /proc/kallsyms | grep -iE 'hook|hide'`, unexpected entries in the syscall table (via a check module or volatility)",
            "integrity: `pacman -Qkk` flags every package file that changed on disk (Arch's tripwire); on a live box also diff against a known-good hash set",
            "the authoritative check is offline: image the disk, run rkhunter/chkrootkit/unhide against the mounted image, and a memory dump through Volatility -- a rootkit lies to tools running under it",
        ],
    ),
    dict(
        name="linux-rootkit-response",
        purpose="what to actually do once a Linux compromise is confirmed",
        trigger_when="a rootkit or persistent backdoor on Linux has been confirmed",
        trigger_matcher=r"confirmed (compromise|rootkit)|incident response|remediat|reimage|clean(ing)? .*(rootkit|infect)|persistence",
        procedure=[
            "FIRST: forensic image (dd/dc3dd) + a memory capture before touching anything -- you only get one shot at the evidence",
            "isolate the box from the network; do NOT reboot yet (loses memory-only artifacts) unless it's actively causing harm",
            "map persistence from the image: kernel modules, initramfs, systemd units + timers, cron, ~/.*rc, ld.so.preload, authorized_keys, PAM modules, package post-install hooks",
            "userland-only + you can enumerate every persistence point -> targeted removal + rotate all creds is possible; kernel-level or any doubt -> reimage from trusted media, restore data (not binaries) from backup",
            "after: rotate every credential that touched the box, review what the attacker could have reached, patch the entry vector",
        ],
    ),
    dict(
        name="android-malware-and-root-detection",
        purpose="check an Android device for unwanted root, spyware, or a malicious app",
        trigger_when="an Android phone is suspected of being compromised or carrying stalkerware",
        trigger_matcher=r"android.*(malware|spyware|stalkerware|rootkit|compromis)|magisk|unwanted root|rogue app|device admin",
        procedure=[
            "root/tamper: `adb shell which su`, look for Magisk/SuperSU, `adb shell getprop ro.boot.verifiedbootstate` (should be `green`), Play Integrity / `ro.boot.flash.locked`",
            "apps: `pm list packages -3` (third-party) and `-s` (system-that-shouldn't-be); `dumpsys package <pkg>` for requested permissions; flag anything with BIND_ACCESSIBILITY_SERVICE, device-admin, SYSTEM_ALERT_WINDOW, or NOTIFICATION_LISTENER it doesn't need",
            "behaviour: `dumpsys activity services`, running native procs, `/data/local/tmp` contents, battery/data usage by app, `adb logcat` for repeated network to one host",
            "pull suspicious APKs (`pm path` -> `adb pull`) and run apk-recon + secrets-in-mobile-apps on them; capture traffic through a proxy for C2",
            "clean-up: uninstall + revoke accessibility/admin first (some apps block uninstall via device-admin); a rooted/tampered `verifiedbootstate` means factory reset, and if bootloader was unlocked, reflash stock",
        ],
    ),
    dict(
        name="analyze-untrusted-apk-safely",
        purpose="run a possibly-malicious APK without risking your own device or data",
        trigger_when="you need to observe what an unknown or suspected-malicious APK actually does",
        trigger_matcher=r"dynamic analysis|sandbox .*apk|detonat|run .*(malware|untrusted).*apk|emulator.*analysis|behavioou?r.*apk",
        procedure=[
            "static FIRST (apk-recon, secrets-in-mobile-apps, YARA) -- never run it before you've read it",
            "use a throwaway AVD emulator (or a spare device you'll wipe), no real Google account, no personal data, a fresh snapshot to revert to",
            "route traffic through mitmproxy/PCAP; consider offline / fake-services so it can't phone home for real",
            "install, snapshot, exercise it; diff filesystem + `pm list packages` + granted perms before/after; `frida-trace` or `strace` the process; watch logcat",
            "record IOCs (C2 domains/IPs, dropped files, package names, cert hashes) and revert the snapshot when done",
        ],
    ),

    # --- the 7B's recurring coding misses (from the zoid loop) ---
    dict(
        name="integer-math-multiply-before-divide",
        purpose="in whole-number / cents math, multiply before you divide -- a/c*b loses everything",
        trigger_when="a method does percent, rate, tax, tip, or any x-of-y arithmetic in integers",
        trigger_matcher=r"percent|\bcents\b|basis point|\bbp\b|/ ?100|integer division|\btax\b|\btip\b|\brate\b|round|floor|\bratio\b",
        procedure=[
            "`x percent of n` in whole units is `n * percent / 100` -- NEVER `n / 100 * percent` (n/100 floors to 0 for small n, e.g. 101/100 -> 1)",
            "integer division already floors toward zero -- do NOT add `.round()`, `- (x % 10)`, or 'nearest dollar' logic unless a test asserts it",
            "half-up rounding only when a test wants it: `(a * b + c / 2) / c`",
            "before running, plug the SMALLEST test input into your formula by hand and check it matches the expected value",
        ],
    ),
    dict(
        name="the-assertions-are-the-spec",
        purpose="assertEquals lines define the exact output required -- produce those numbers, add nothing",
        trigger_when="implementing a method body that a test suite pins",
        trigger_matcher=r"assertEquals|assert .*==|expected:.*but was|implement|TODO\(|NotImplementedError|method body|stub",
        procedure=[
            "list every test line that calls this method and the exact expected value for each input",
            "work out the formula/logic that turns each input into its expected output -- on paper",
            "write exactly that: no extra clamping, rounding, defaults, randomness, or 'sensible' behaviour the tests don't check",
            "if two assertions seem to contradict, you've misread one -- re-read before coding around it",
        ],
    ),
    dict(
        name="similar-names-different-jobs",
        purpose="near-twin methods (x / firstX, deposit / withdraw) do DIFFERENT things -- pin the difference first",
        trigger_when="a class has two methods with related names and you're implementing them",
        trigger_matcher=r"(deposit|withdraw|credit|debit)|first[A-Z]\w+|base.?share|remainder|per.?person|\bgross\b.*\bnet\b",
        procedure=[
            "for each near-twin, write ONE line: what makes this one different from its sibling",
            "e.g. `shareCents` = total / people (the floor share everyone pays); `firstShareCents` = that PLUS total % people (carries the leftover cents)",
            "`deposit` adds; `withdraw` subtracts AND guards against overdraft -- opposite sign, extra check",
            "implement the simpler twin, then define the other in terms of it where you can",
        ],
    ),
    dict(
        name="kotlin-stdlib-that-exists",
        purpose="don't invent Kotlin functions -- use the real collection/return idioms",
        trigger_when="writing Kotlin that iterates, indexes, or builds a collection",
        trigger_matcher=r"\.kt\b|kotlin|allIndexed|anyIndexed|Unresolved reference|forEachIndexed|mutableListOf|MutableList|withIndex",
        procedure=[
            "indexed iteration: `for ((i, x) in list.withIndex())`, `list.forEachIndexed { i, x -> }`, `list.mapIndexed`, `list.indices` -- there is NO `allIndexed`/`anyIndexed`, use `list.withIndex().all { (i, x) -> ... }`",
            "mutable list: `mutableListOf<T>()` + `.add()`, or `MutableList(n) { i -> ... }`; `listOf` / `List(n){}` are read-only",
            "no `?:`-as-ternary for logic: `if (c) a else b` IS an expression; a value-returning method needs an explicit `return` on every path",
            "before finishing, count `{` vs `}` -- a missing brace is the 7B's most common Kotlin compile error",
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
