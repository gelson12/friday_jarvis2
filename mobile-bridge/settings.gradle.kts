pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // LiveKit Android SDK pulls com.github.davidliu:audioswitch from
        // JitPack (commit-hash version). Without this repo Gradle can't
        // resolve the transitive and the build fails at
        // dataBindingMergeDependencyArtifactsDebug.
        maven { url = uri("https://jitpack.io") }
    }
}

rootProject.name = "JarvisMobileBridge"
include(":app")
