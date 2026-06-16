plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.jarvis.mobilebridge"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.jarvis.mobilebridge"
        minSdk = 26          // Android 8.0 — needed for foreground-service notification channels
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"

        // Shared key for one-tap recovery provisioning (matches the worker's BRIDGE_RECOVERY_KEY).
        // Injected at build time (-PBRIDGE_RECOVERY_KEY=… or env); empty for ordinary builds so the
        // deep-link provisioning is simply inert until an update-recovery build bakes it in.
        val recoveryKey = (project.findProperty("BRIDGE_RECOVERY_KEY") as String?)
            ?: System.getenv("BRIDGE_RECOVERY_KEY") ?: ""
        buildConfigField("String", "BRIDGE_RECOVERY_KEY", "\"$recoveryKey\"")
    }

    // STABLE signing key (committed jarvis-debug.keystore) so every build shares ONE
    // signature — installs/updates go OVER the top, no uninstall + no re-entering the
    // token. The build machine's auto-generated ~/.android/debug.keystore is ephemeral
    // (regenerates on container reset → signature mismatch), which is exactly what we
    // avoid by pinning our own. A debug keystore's password is the public default, so
    // committing it is safe (it is NOT a release/Play key).
    signingConfigs {
        getByName("debug") {
            storeFile = file("jarvis-debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Signed locally — instructions in README.md.
            // signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures {
        viewBinding = true
        buildConfig = true
    }
}

dependencies {
    // LiveKit Android SDK — joins the same control room desktop-bridge uses.
    implementation("io.livekit:livekit-android:2.5.+")

    // Standard Android
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.activity:activity-ktx:1.9.0")

    // Encrypted shared prefs for the token-endpoint + bridge-token storage.
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // Coroutines (LiveKit SDK requires them; convenient for IO + main mixing).
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.0")

    // WorkManager — system-scheduled periodic worker that RESTARTS the bridge service
    // even after the OS has killed the whole process (a plain in-process loop can't, it
    // dies with the process). Core of the self-revival on aggressive OEMs like ColorOS.
    implementation("androidx.work:work-runtime-ktx:2.9.1")

    // HTTP — for the token-endpoint POST.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // JSON
    implementation("org.json:json:20240303")
}
