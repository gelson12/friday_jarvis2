package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.media.AudioManager
import org.json.JSONObject
import kotlin.math.roundToInt

/**
 * Media-volume control. No special permission needed for STREAM_MUSIC.
 * Args (any one):
 *   level     0-100  -> set absolute media volume
 *   direction "up"/"down"  -> step the volume
 *   mute      true   -> toggle mute
 * No args -> report the current volume percent.
 */
object VolumeHandler {
    fun set(ctx: Context, args: JSONObject): JSONObject {
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val stream = AudioManager.STREAM_MUSIC
        val max = am.getStreamMaxVolume(stream).coerceAtLeast(1)
        val flags = AudioManager.FLAG_SHOW_UI
        return when {
            args.has("level") -> {
                val pct = args.optInt("level").coerceIn(0, 100)
                am.setStreamVolume(stream, (pct * max / 100.0).roundToInt(), flags)
                JSONObject().put("volume_percent", pct)
            }
            args.optString("direction").equals("up", true) || args.optBoolean("up") -> {
                am.adjustStreamVolume(stream, AudioManager.ADJUST_RAISE, flags)
                JSONObject().put("adjusted", "up").put("volume_percent", am.getStreamVolume(stream) * 100 / max)
            }
            args.optString("direction").equals("down", true) || args.optBoolean("down") -> {
                am.adjustStreamVolume(stream, AudioManager.ADJUST_LOWER, flags)
                JSONObject().put("adjusted", "down").put("volume_percent", am.getStreamVolume(stream) * 100 / max)
            }
            args.optBoolean("mute") -> {
                am.adjustStreamVolume(stream, AudioManager.ADJUST_TOGGLE_MUTE, flags)
                JSONObject().put("muted", true)
            }
            else -> JSONObject().put("volume_percent", am.getStreamVolume(stream) * 100 / max)
        }
    }
}
