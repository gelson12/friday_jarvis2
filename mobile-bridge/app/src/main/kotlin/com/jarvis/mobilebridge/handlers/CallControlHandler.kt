package com.jarvis.mobilebridge.handlers

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioManager
import android.os.Build
import android.telecom.TelecomManager
import androidx.core.content.ContextCompat
import org.json.JSONObject

/**
 * Answer / end / mute / speaker for the live call, so the owner can take a call
 * from the dashboard.
 *
 * NOTE on "listen from the dashboard": Android forbids a normal app from tapping
 * the raw call-audio stream, so the supported path is `speaker` (loudspeaker on) +
 * the bridge's mic-publish — the dashboard then hears the call through the phone's
 * speaker. Whether the mic can be captured during a cellular call is device/OEM
 * dependent, so this is best-effort by design.
 */
object CallControlHandler {

    private fun has(ctx: Context, perm: String) =
        ContextCompat.checkSelfPermission(ctx, perm) == PackageManager.PERMISSION_GRANTED

    fun answer(ctx: Context): JSONObject {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O)
            return JSONObject().put("error", "answering needs Android 8 or newer")
        if (!has(ctx, Manifest.permission.ANSWER_PHONE_CALLS))
            return JSONObject().put("error", "grant the Phone (answer calls) permission, sir")
        return try {
            val tm = ctx.getSystemService(Context.TELECOM_SERVICE) as TelecomManager
            tm.acceptRingingCall()
            JSONObject().put("answered", true)
        } catch (e: SecurityException) {
            JSONObject().put("error", "answer-call permission not granted")
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "couldn't answer the call")
        }
    }

    fun end(ctx: Context): JSONObject {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P)
            return JSONObject().put("error", "ending a call needs Android 9 or newer")
        if (!has(ctx, Manifest.permission.ANSWER_PHONE_CALLS))
            return JSONObject().put("error", "grant the Phone (answer calls) permission, sir")
        return try {
            val tm = ctx.getSystemService(Context.TELECOM_SERVICE) as TelecomManager
            val ok = tm.endCall()
            JSONObject().put("ended", ok)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "couldn't end the call")
        }
    }

    fun mute(ctx: Context, args: JSONObject): JSONObject {
        val mute = args.optBoolean("mute", true)
        return try {
            val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            am.isMicrophoneMute = mute
            JSONObject().put("muted", mute)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "couldn't change mute")
        }
    }

    fun speaker(ctx: Context, args: JSONObject): JSONObject {
        val on = args.optBoolean("on", true)
        return try {
            val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            @Suppress("DEPRECATION")
            am.isSpeakerphoneOn = on
            JSONObject().put("speaker", if (on) "on" else "off")
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "couldn't toggle the speaker")
        }
    }
}
