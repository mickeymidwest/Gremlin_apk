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
        versionCode = 11
        versionName = "2.0"

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
    // On-device OCR for attached images and PDFs (attach/ImageText.kt).
    // The BUNDLED model: ships in the APK, works with no network and no
    // Google Play, like the rest of the app.
    implementation("com.google.mlkit:text-recognition:16.0.1")
    // Deep scan (S4b): the app's own ADB client for this phone's Wireless
    // debugging (the library App Manager uses) and Conscrypt for the TLS 1.3
    // pairing.
    implementation("com.github.MuntashirAkon:libadb-android:3.1.1")
    // certificate for the ADB identity; same BouncyCastle family/version libadb ships (bcprov-jdk15to18 1.81)
    implementation("org.bouncycastle:bcpkix-jdk15to18:1.81")
    implementation("org.conscrypt:conscrypt-android:2.5.2")
}
