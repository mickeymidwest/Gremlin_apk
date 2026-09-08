# android template — harness-owned reference

The boilerplate half of an Android app: everything the model should **never**
write. `android_scaffold.render()` copies this tree, substitutes the four
placeholders, and hands the result to `method_builder.build_project`, which
fills the feature stubs one method at a time.

## Provenance

Derived from `~/Downloads/buildalot` and `~/Downloads/solitaire` — Gremlin's
own Android projects that **build on this box** with the no-sudo toolchain
(`~/android-build`: Temurin JDK 17, Gradle 9.4.1; `~/Android/Sdk`:
build-tools/platform 35). Not copied from the internet — version drift in a
fetched template is exactly what breaks a local build. Update this tree by
re-deriving from a project that currently builds, not by bumping versions
blind.

## Stack (deliberately minimal)

- AGP `com.android.application` 9.2.0 (bundles Kotlin — no separate plugin)
- Gradle 9.4.1 (wrapper jar vendored)
- Plain Android `View` / `Activity`, **no Jetpack Compose** — the 7B writes
  far more compilable plain-Kotlin-View code than Compose
- JUnit 4.13.2 + `unitTests.isReturnDefaultValues = true` so
  `./gradlew testDebugUnitTest` is the scaffold's oracle
- compileSdk 35 / minSdk 24 / targetSdk 34, arm64-v8a only

## Placeholders

| token           | example                    |
|-----------------|----------------------------|
| `__APP_NAME__`  | `Tip Calculator`           |
| `__PKG__`       | `com.gremlin.tipcalc`      |
| `__PKG_PATH__`  | `com/gremlin/tipcalc` (dir)|
| `__THEME__`     | `Tipcalc`                  |
