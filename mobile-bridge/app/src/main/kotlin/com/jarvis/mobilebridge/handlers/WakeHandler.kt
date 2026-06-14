package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import com.jarvis.mobilebridge.WakeActivity
import org.json.JSONObject

/**
 * Wakes the screen (and optionally asks the keyguard to dismiss) by launching the
 * transparent WakeActivity. `dismiss` only clears a swipe / Smart-Lock keyguard —
 * a secure PIN/pattern/biometric is never bypassable (no Android API allows it).
 */
object WakeHandler {
    fun wake(ctx: Context, dismiss: Boolean): JSONObject = try {
        ctx.startActivity(
            Intent(ctx, WakeActivity::class.java)
                .putExtra("dismiss", dismiss)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_USER_ACTION)
        )
        JSONObject()
            .put("woke", true)
            .put("dismiss_requested", dismiss)
            .put(
                "note",
                if (dismiss)
                    "screen on; a swipe/Smart-Lock screen is dismissed, but a secure PIN/pattern/biometric can't be bypassed by any app"
                else "screen woken"
            )
    } catch (e: Exception) {
        JSONObject().put("error", e.message ?: "couldn't wake the screen")
    }
}
