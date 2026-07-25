# Gremlin (Android)

Talks to a Gremlin desktop instance over your home Wi-Fi when it's
reachable, and falls back -- in order -- to its own
offline model, then Claude, then Gemini (using your own API keys,
entered in Settings) when it's not. Same hologram widget as the
desktop version.

Needs `git clone --recurse-submodules` (or `git submodule update --init
--recursive` after a plain clone) -- `android/llama.cpp` is a pinned
submodule used by the on-device offline model.

## One offline model: it chats AND it sees

Settings -> "Offline model" puts a single vision-language model on the
phone. A VLM *is* a language model, so the same weights answer a question
and read a screenshot -- there was never a reason to ship a separate chat
model and a separate vision model, and doing so meant two downloads and
two code paths to keep in sync.

**Synced from the desktop** when paired: it serves whatever
`persona.phone_model` names in `config/models.yaml`, so the desktop
decides what the phone runs. That key is deliberately separate from
`primary_model` -- the primary is ~5GB and tuned for an 8GB GPU, and
would be refused by the ceiling below anyway. Unpaired, it falls back to
downloading SmolVLM-500M (~546MB) directly.

**Hard 2GB ceiling** on anything that lands on the phone, enforced three
ways: against the advertised size before a byte is written, against the
running total mid-stream (a server can omit Content-Length and otherwise
stream forever, which makes the first check trivially bypassable), and
against free space. Transfers resume rather than restart.

`phone_model` must be a VLM (weights + `mmproj` projector). The phone
initialises through mtmd, which needs a projector even for a text-only
reply. With weights alone it won't load; with a projector missing after a
partial sync it would chat but silently not see -- which is why both
parts are promoted together or not at all.

Honest limits: a ~0.5B VLM is a much weaker conversationalist than the
desktop's primary. It's a fallback, not a peer, and the desktop is always
tried first.

## Overlay mode

Settings → "Turn on overlay mode" floats a bubble over other apps. Tap
it anywhere -- in a browser doing homework, say -- and it captures the
screen once, OCRs it on-device (ML Kit, bundled model, no network), and
answers about what's actually on the page. There's also an "Attach"
button in the main screen for files, PDFs, and images.

**This folder is part of the combined `gremlin` repo, not its own
separate repo** -- see the root `README.md`'s "Putting this on GitHub"
section for the actual push instructions. The short version: one
`git init` / `git push` at the repo root pushes both this and the
desktop project together; `.github/workflows/android-build.yml` (at
the repo root, not in here -- GitHub Actions only looks there) builds
this specifically whenever something under `android/` changes.

## Getting a built APK without installing Android Studio

Once the combined repo is pushed (see the root README), check your
repo's **Actions** tab → the `Android Build` workflow run → scroll to
**Artifacts** → download `gremlin-debug-apk`. Unzip that, and you have
an installable APK -- copy it to your phone and open it (you'll need to
allow "install from unknown sources" the first time, standard for
anything not from the Play Store).

## Building locally in Android Studio instead

Open this `android/` folder specifically in Android Studio (not the
repo root) and let it sync. One thing worth knowing up front: **this
repo doesn't include the Gradle wrapper jar**
(`gradle/wrapper/gradle-wrapper.jar`). That file is a small compiled
binary, and I don't consider it safe to hand-produce without a working
Gradle installation to generate it correctly -- a subtly wrong or
corrupted jar would fail in a much more confusing way than just not
having one. Android Studio handles this fine on its own (it can
generate the wrapper automatically on import), or generate it yourself
once if you have Gradle installed:

```bash
gradle wrapper --gradle-version 8.13
```

After that, `./gradlew assembleDebug` works locally too, and you could
switch the CI workflow to use `./gradlew` instead of `gradle` if you'd
rather it use your committed wrapper version specifically.

## Honesty note

I (Claude) wrote this app without ever being able to compile or run it
myself -- no Android SDK, emulator, or Kotlin compiler available in my
environment. Everything I could check without a real build was
checked: XML validity, Kotlin brace/paren balance, every `R.id.*` and
`R.array.*` reference matched against what's actually declared in the
layouts, current library/plugin versions verified via live search
rather than trusted from memory. The GitHub Actions workflow is the
first time this project gets an actual compile check from a real
toolchain. If it fails, that's genuinely useful information -- paste me
the exact error from the Actions log and I'll fix it against something
real instead of guessing again.

The native/JNI piece (`app/src/main/cpp/gremlin_llama.cpp`,
`LocalLlama.kt`) carries the same caveat but more so -- it's adapted
line-by-line from the real, working `ai_chat.cpp` in the pinned
llama.cpp submodule (read directly off disk, not reconstructed from
memory or a lossy web summary, specifically to avoid guessing at C API
signatures that drift across llama.cpp versions), with
`processUserPrompt()` + `generateNextToken()`'s per-token JNI loop
collapsed into one blocking `generate()` call. The CMake/NDK wiring in
`app/build.gradle.kts` and `app/src/main/cpp/CMakeLists.txt` is new
territory for this project's CI (`android-build.yml` now installs NDK
27 and checks out the submodule) and has never actually run yet as of
this writing -- expect at least one round of real build errors on the
first push, most likely in the CMake config rather than the C++ itself.
