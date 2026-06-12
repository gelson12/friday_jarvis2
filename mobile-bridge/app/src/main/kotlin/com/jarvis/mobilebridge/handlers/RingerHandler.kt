package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.media.AudioManager
import org.json.JSONObject

/**
 * Ringer mode: silent / vibrate / normal. SILENT (and VIBRATE on some OEMs) needs
 * Do-Not-Disturb access (ACCESS_NOTIFICATION_POLICY) — we catch the SecurityException and
 * tell the user rather than failing silently.
 */
object RingerHandler {
    fun set(ctx: Context, args: JSONObject): JSONObject {
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val mode = args.optString("mode").lowercase()
        val ringer = when (mode) {
            "silent", "mute", "quiet" -> AudioManager.RINGER_MODE_SILENT
            "vibrate", "vibration", "vibrator" -> AudioManager.RINGER_MODE_VIBRATE
            "normal", "ring", "loud", "sound" -> AudioManager.RINGER_MODE_NORMAL
            else -> return JSONObject().put("error", "mode must be silent / vibrate / normal")
        }
        return try {
            am.ringerMode = ringer
            JSONObject().put("ringer_mode", mode)
        } catch (e: SecurityException) {
            JSONObject().put("error", "needs Do-Not-Disturb access for '$mode' mode")
                .put("note", "grant the app DND access in Settings, sir")
        }
    }
}
