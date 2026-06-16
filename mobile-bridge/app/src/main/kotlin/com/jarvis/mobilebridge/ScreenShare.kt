package com.jarvis.mobilebridge

import android.content.Context
import android.content.Intent
import java.io.File

/**
 * Process-global bridge between the MediaProjection consent Activity and the LiveKitClient.
 * MediaProjection can ONLY be granted from an Activity showing the system consent — but the
 * screen track is published by LiveKitClient. So the consent Activity stashes the result
 * here and LiveKitClient consumes it. File-logs the flow (filesDir/screen.log) for ADB
 * debugging since ColorOS filters logcat.
 */
object ScreenShare {
    @Volatile var pending: Pair<Int, Intent>? = null
    @Volatile var onGranted: ((Int, Intent) -> Unit)? = null
    @Volatile var lastDenied: Boolean = false
    @Volatile private var appCtx: Context? = null

    // Set while the system MediaProjection consent dialog is expected, so the bound
    // AccessibilityService can auto-accept it (select "Entire screen" -> "Start now") and
    // make the no-laptop screen share zero-touch. Cleared once consent is delivered, the
    // accessibility service finishes the tap sequence, or a short timeout elapses.
    @Volatile var awaitingScreenConsent: Boolean = false
    @Volatile var consentStage: Int = 0          // 0 open spinner, 1 pick entire-screen, 2 hit Start
    @Volatile var consentDeadlineMs: Long = 0L

    fun log(m: String) {
        try { appCtx?.let { File(it.filesDir, "screen.log").appendText("${System.currentTimeMillis()} $m\n") } } catch (_: Exception) {}
    }

    fun request(ctx: Context) {
        appCtx = ctx.applicationContext
        lastDenied = false
        // Arm the accessibility auto-accept just before the consent dialog appears.
        awaitingScreenConsent = true
        consentStage = 0
        consentDeadlineMs = System.currentTimeMillis() + 9000L
        log("request (onGranted=${onGranted != null}, autoAccept=${ControlAccessibilityService.enabled()})")
        ctx.startActivity(
            Intent(ctx, ScreenCaptureActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }

    fun deliver(resultCode: Int, data: Intent) {
        awaitingScreenConsent = false
        log("deliver rc=$resultCode onGranted=${onGranted != null}")
        pending = resultCode to data
        onGranted?.invoke(resultCode, data)
    }
}
