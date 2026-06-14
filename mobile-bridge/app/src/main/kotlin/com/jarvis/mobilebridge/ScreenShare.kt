package com.jarvis.mobilebridge

import android.content.Context
import android.content.Intent

/**
 * Process-global bridge between the MediaProjection consent Activity and the
 * LiveKitClient.
 *
 * MediaProjection (screen capture) can ONLY be granted from an Activity showing
 * the system "Start recording / casting?" dialog — it can't be silent. But the
 * screen track is published by LiveKitClient, which holds the Room. So the
 * consent Activity stashes the granted result here and LiveKitClient consumes it.
 */
object ScreenShare {
    /** The most recent granted projection result (resultCode + the data Intent). */
    @Volatile var pending: Pair<Int, Intent>? = null

    /** LiveKitClient registers this to be called the instant consent is granted. */
    @Volatile var onGranted: ((Int, Intent) -> Unit)? = null

    /** True when the user denied / cancelled the consent (surfaced to the worker). */
    @Volatile var lastDenied: Boolean = false

    /** Pop the system consent dialog (from a fresh task — we may be in the bg). */
    fun request(ctx: Context) {
        lastDenied = false
        ctx.startActivity(
            Intent(ctx, ScreenCaptureActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }

    fun deliver(resultCode: Int, data: Intent) {
        pending = resultCode to data
        onGranted?.invoke(resultCode, data)
    }
}
