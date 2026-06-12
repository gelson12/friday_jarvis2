#!/usr/bin/env bash
# Build an Android debug APK on VS-Code-inspiring-cat and upload it as
# a GitHub Release asset.
#
# Defaults reproduce the original mobile-bridge build exactly (clone
# gelson12/friday_jarvis2, build the mobile-bridge module, tag
# mobile-bridge-v0.1.0-*). Voice-driven arbitrary-repo builds set the
# env vars below to redirect the clone, the module path, and the tag
# prefix while keeping the release host fixed.
#
# Used by:
#   - Manual: ssh into the cli-worker / code-server and run this.
#   - Voice agent: fj2 agent.py + OpenJarvis worker.py submit a /tasks
#     shell call that exports any overrides, then `curl`s this script
#     and pipes it to bash.
#
# Output contract: when successful, creates a GitHub release tagged
#   ${TAG_PREFIX}<YYYYMMDD-HHMMSS>
# on ${RELEASE_REPO} with one asset, app-debug.apk. The voice agent
# polls the GitHub API for new releases matching that prefix.
#
# Environment overrides (all optional):
#   SOURCE_REPO_URL  — https://github.com/<owner>/<repo>.git  (what to clone)
#   REPO_OWNER       — owner of the source repo (used in clone dir name)
#   REPO_NAME        — name of the source repo (used in clone dir name)
#   APK_MODULE_DIR   — sub-directory inside the repo containing the Android
#                      project (empty = repo root). For mobile-bridge: "mobile-bridge"
#   TAG_PREFIX       — release tag prefix (timestamp is appended)
#   RELEASE_REPO     — <owner>/<repo> that receives the gh release
#                      (defaults to gelson12/friday_jarvis2 — single host
#                      keeps the 24h cleanup workflow simple)
#
# Environment expected on inspiring-cat:
#   GITHUB_PAT or GH_TOKEN  — for gh CLI release upload (needs write
#                             access to RELEASE_REPO)
#   /opt/android-sdk         — installed in Dockerfile.cli
#   curl + unzip             — available; wget is NOT
#   default-jdk-headless     — installed
#
# Notes on past failures (do NOT remove the safeguards below):
#   v1: system gradle is 4.4.1 — way too old for AGP 8.x. Always force
#       a modern gradle into /opt/gradle.
#   v2: wget isn't in the container — use curl only.
#   v3: LiveKit Android SDK pulls com.github.davidliu:audioswitch from
#       JitPack only. settings.gradle.kts already adds the repo, but if
#       you're running this against a fork without that fix, builds fail
#       at dataBindingMergeDependencyArtifactsDebug.
#   v5/v6: DataPublishReliability lives in io.livekit.android.room.track,
#       not .participant — settings already correct in main.

set -eo pipefail

REPO_OWNER=${REPO_OWNER:-gelson12}
REPO_NAME=${REPO_NAME:-friday_jarvis2}
SOURCE_REPO_URL=${SOURCE_REPO_URL:-"https://github.com/${REPO_OWNER}/${REPO_NAME}.git"}
REPO_DIR="/workspace/${REPO_NAME}"
APK_MODULE_DIR="${APK_MODULE_DIR-mobile-bridge}"
TAG_PREFIX="${TAG_PREFIX:-mobile-bridge-v0.1.0-}"
RELEASE_REPO="${RELEASE_REPO:-gelson12/friday_jarvis2}"
# The build cd's INTO $MODULE_PATH before assembling, so the APK is ALWAYS at
# app/build/... relative to there. Prefixing it with $APK_MODULE_DIR doubled the path
# (mobile-bridge/mobile-bridge/app/...) -> the false "APK not found" failure.
if [ -n "$APK_MODULE_DIR" ]; then
    MODULE_PATH="/workspace/${REPO_NAME}/${APK_MODULE_DIR}"
else
    MODULE_PATH="/workspace/${REPO_NAME}"
fi
APK_REL_PATH="app/build/outputs/apk/debug/app-debug.apk"
GRADLE_VER="${GRADLE_VER:-8.9}"
LOG="${BUILD_LOG:-/tmp/mobile-bridge-build.log}"

# Pick up GITHUB_PAT under any of the conventional names gh CLI reads.
export GH_TOKEN="${GITHUB_PAT:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}"
export GITHUB_TOKEN="${GH_TOKEN}"

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

log "==== build start (SRC=$SOURCE_REPO_URL MOD=$APK_MODULE_DIR PREFIX=$TAG_PREFIX HOST=$RELEASE_REPO GRADLE=$GRADLE_VER) ===="

# ── 1. Ensure modern Gradle (container ships 4.4.1, too old for AGP 8.x) ──
if [ ! -d "/opt/gradle/gradle-${GRADLE_VER}" ]; then
    log "[step] download Gradle ${GRADLE_VER}"
    mkdir -p /opt/gradle && cd /opt/gradle
    curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VER}-bin.zip" -o g.zip >> "$LOG" 2>&1
    log "[step] unzip Gradle"
    unzip -q -o g.zip >> "$LOG" 2>&1
    rm -f g.zip
fi
export PATH="/opt/gradle/gradle-${GRADLE_VER}/bin:$PATH"
log "[step] gradle in use: $(command -v gradle)"
gradle --version >> "$LOG" 2>&1 || true

# ── 2. Sync the repo ──
# Always fresh-clone arbitrary repos (the workspace might still hold a
# different repo under the same dir name from a previous run, and we
# can't trust the remote to match). For the mobile-bridge default we
# keep the fast-sync path so repeat builds stay quick.
cd /workspace
if [ "$SOURCE_REPO_URL" = "https://github.com/${REPO_OWNER}/${REPO_NAME}.git" ] && [ -d "$REPO_NAME/.git" ]; then
    log "[step] sync existing repo"
    cd "$REPO_NAME"
    git fetch --quiet origin main >> "$LOG" 2>&1
    git reset --hard origin/main >> "$LOG" 2>&1
    # Wipe any stale wrapper / build outputs from previous attempts.
    if [ -n "$APK_MODULE_DIR" ]; then
        rm -rf "$APK_MODULE_DIR/.gradle" "$APK_MODULE_DIR/build" \
               "$APK_MODULE_DIR/app/build" \
               "$APK_MODULE_DIR/gradle" "$APK_MODULE_DIR/gradlew" \
               "$APK_MODULE_DIR/gradlew.bat" 2>/dev/null || true
    else
        rm -rf .gradle build app/build gradle gradlew gradlew.bat 2>/dev/null || true
    fi
else
    log "[step] clone repo ($SOURCE_REPO_URL)"
    rm -rf "$REPO_NAME"
    git clone --depth 1 "$SOURCE_REPO_URL" "$REPO_NAME" >> "$LOG" 2>&1
fi
cd "$MODULE_PATH"

# ── 3. Bootstrap the wrapper at the version we want ──
log "[step] bootstrap wrapper at ${GRADLE_VER}"
gradle wrapper --gradle-version "${GRADLE_VER}" --distribution-type bin >> "$LOG" 2>&1

# ── 4. Build ──
log "[step] assembleDebug"
./gradlew assembleDebug --no-daemon --stacktrace --warning-mode summary >> "$LOG" 2>&1

if [ ! -f "$APK_REL_PATH" ]; then
    log "[FAIL] APK not found at $APK_REL_PATH"
    find app/build/outputs -type f 2>/dev/null >> "$LOG" || true
    exit 1
fi
APK_SIZE=$(stat -c%s "$APK_REL_PATH")
log "[step] APK built: $APK_REL_PATH ($APK_SIZE bytes)"

# ── 5. Publish as a GitHub release asset ──
TAG="${TAG_PREFIX}$(date +%Y%m%d-%H%M%S)"
log "[step] gh release create $TAG on $RELEASE_REPO"
gh release create "$TAG" "$APK_REL_PATH" \
    --repo "$RELEASE_REPO" \
    --title "Jarvis APK ($TAG)" \
    --notes "Debug-signed APK built via VS-Code-inspiring-cat from $SOURCE_REPO_URL (Gradle ${GRADLE_VER}, AGP 8.5). Sideload: enable Settings > Apps > Install unknown apps for your installer. Auto-deleted ~24h after publish." >> "$LOG" 2>&1

log "[DONE] RELEASE_TAG=$TAG"
echo "RELEASE_TAG=$TAG"
