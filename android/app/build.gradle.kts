plugins {
    id("com.android.application")
}

android {
    namespace = "com.gremlin.app"
    compileSdk = 35

    // No NDK/CMake config here any more: the on-device offline model was
    // removed (again -- see android/README.md's history note). This app
    // is pure Kotlin: away from home it reaches Claude/Gemini directly
    // instead of a phone-local model.

    defaultConfig {
        applicationId = "com.gremlin.app"
        minSdk = 24
        targetSdk = 35
        versionCode = 2
        versionName = "1.1"

        // Caps ML Kit's bundled OCR model, which ships .so files for
        // four ABIs (~41MB). Dropping this filter once took the APK from
        // 30MB to 49MB. arm64-v8a is every real phone this runs on.
        ndk {
            abiFilters += "arm64-v8a"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("com.google.android.material:material:1.12.0")
    // QR scanning without needing Google Play Services -- opens its own
    // camera activity and hands back the scanned text.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    // On-device OCR for overlay mode (reading what's on screen) and for
    // attached images/PDFs. This is the BUNDLED model, not the
    // Play-Services-backed one: it ships inside the APK and works with
    // no network and no Google Play, which matters because the rest of
    // this app goes out of its way to keep working offline.
    implementation("com.google.mlkit:text-recognition:16.0.1")
}
