package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import android.provider.AlarmClock
import org.json.JSONObject

/**
 * Alarms via the standard AlarmClock intents — no permission required, works with the
 * device's default clock app. EXTRA_SKIP_UI sets/dismisses silently (no clock UI popping up).
 */
object AlarmHandler {
    /** Args: hour (0-23, required), minute (0-59, default 0), label (optional). */
    fun set(ctx: Context, args: JSONObject): JSONObject {
        val hour = args.optInt("hour", -1)
        if (hour !in 0..23) return JSONObject().put("error", "hour (0-23) required")
        val minute = args.optInt("minute", 0).coerceIn(0, 59)
        val label = args.optString("label").ifEmpty { "Jarvis alarm" }
        val i = Intent(AlarmClock.ACTION_SET_ALARM)
            .putExtra(AlarmClock.EXTRA_HOUR, hour)
            .putExtra(AlarmClock.EXTRA_MINUTES, minute)
            .putExtra(AlarmClock.EXTRA_MESSAGE, label)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            ctx.startActivity(i)
            JSONObject().put("alarm_set", "%02d:%02d".format(hour, minute)).put("label", label)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "no alarm app available")
        }
    }

    /** Dismiss the next/firing alarm (ACTION_DISMISS_ALARM, API 23+). */
    fun dismiss(ctx: Context, @Suppress("UNUSED_PARAMETER") args: JSONObject): JSONObject {
        val i = Intent(AlarmClock.ACTION_DISMISS_ALARM)
            .putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, AlarmClock.ALARM_SEARCH_MODE_NEXT)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            ctx.startActivity(i)
            JSONObject().put("alarm_dismissed", true)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "could not dismiss alarm")
        }
    }
}
