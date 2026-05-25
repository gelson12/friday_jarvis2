#!/usr/bin/env bash
# Build the mobile-bridge debug APK on VS-Code-inspiring-cat and upload
# it as a GitHub Release asset on gelson12/friday_jarvis2.
#
# Used by:
#   - Manual: ssh into the cli-worker / code-server and run this.
#   - Voice agent: fj2 agent.py + OpenJarvis worker.py submit a /tasks
#     shell call that `curl`s this script and pipes it to bash.
#
# Output contract: when successful, creates a GitHub release tagged
#   mobile-bridge-v0.1.0-<YYYYMMDD-HHMMSS>
# with one asset, app-debug.apk. The voice agent polls the GitHub API
# for new releases matching that prefix.
#
# Environment expected on inspiring-cat:
#   GITHUB_PAT or GH_TOKEN  — for gh CLI release upload
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
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
REPO_DIR="/workspace/${REPO_NAME}"
APK_REL_PATH="mobile-bridge/app/build/outputs/apk/debug/app-debug.apk"
GRADLE_VER="${GRADLE_VER:-8.9}"
LOG="${BUILD_LOG:-/tmp/mobile-bridge-build.log}"

# Pick up GITHUB_PAT under any of the conventional names gh CLI reads.
export GH_TOKEN="${GITHUB_PAT:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}"
export GITHUB_TOKEN="${GH_TOKEN}"

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

log "==== build start (REPO=$REPO_OWNER/$REPO_NAME GRADLE=$GRADLE_VER) ===="

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
cd /workspace
if [ -d "$REPO_NAME" ]; then
    log "[step] sync existing repo"
    cd "$REPO_NAME"
    git fetch --quiet origin main >> "$LOG" 2>&1
    git reset --hard origin/main >> "$LOG" 2>&1
    # Wipe any stale wrapper / build outputs from previous attempts.
    rm -rf mobile-bridge/.gradle mobile-bridge/build mobile-bridge/app/build \
           mobile-bridge/gradle mobile-bridge/gradlew mobile-bridge/gradlew.bat \
           2>/dev/null || true
else
    log "[step] clone repo"
    git clone --depth 1 "$REPO_URL" "$REPO_NAME" >> "$LOG" 2>&1
fi
cd "/workspace/${REPO_NAME}/mobile-bridge"

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
TAG="mobile-bridge-v0.1.0-$(date +%Y%m%d-%H%M%S)"
log "[step] gh release create $TAG"
gh release create "$TAG" "$APK_REL_PATH" \
    --repo "${REPO_OWNER}/${REPO_NAME}" \
    --title "Jarvis Mobile Bridge APK ($TAG)" \
    --notes "Debug-signed APK built via VS-Code-inspiring-cat (Gradle ${GRADLE_VER}, AGP 8.5). Sideload: enable Settings > Apps > Install unknown apps for your installer. See mobile-bridge/README.md for token-endpoint + permissions setup." >> "$LOG" 2>&1

log "[DONE] RELEASE_TAG=$TAG"
echo "RELEASE_TAG=$TAG"
