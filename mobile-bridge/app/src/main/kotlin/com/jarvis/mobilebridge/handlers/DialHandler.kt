package com.jarvis.mobilebridge.handlers

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import androidx.core.content.ContextCompat
import org.json.JSONObject

/**
 * Places a phone call.
 *
 * If the CALL_PHONE permission is granted, the call is placed IMMEDIATELY
 * (ACTION_CALL) — true remote auto-dial. If it isn't granted (or args carries
 * `dial_only=true`), it falls back to opening the dialer pre-filled
 * (ACTION_DIAL) so the user can tap to call. This way the same `dial` command
 * works either way and degrades gracefully until the user grants the permission
 * (which MainActivity already requests on launch).
 */
object DialHandler {
    fun execute(ctx: Context, args: JSONObject): JSONObject {
        val number = args.optString("number").trim()
        if (number.isEmpty()) return JSONObject().put("error", "number is required")
        val uri = Uri.parse("tel:$number")
        val dialOnly = args.optBoolean("dial_only", false)
        val canCall = !dialOnly && ContextCompat.checkSelfPermission(
            ctx, Manifest.permission.CALL_PHONE
        ) == PackageManager.PERMISSION_GRANTED
        return try {
            val action = if (canCall) Intent.ACTION_CALL else Intent.ACTION_DIAL
            ctx.startActivity(Intent(action, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            if (canCall) {
                JSONObject().put("calling", true).put("number", number)
            } else {
                JSONObject().put("dialer_opened", true).put("number", number)
                    .put("note", "grant the Phone (CALL_PHONE) permission to place calls automatically")
            }
        } catch (e: SecurityException) {
            // CALL_PHONE was revoked between the check and the call → open dialer.
            ctx.startActivity(Intent(Intent.ACTION_DIAL, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            JSONObject().put("dialer_opened", true).put("number", number)
                .put("error", "CALL_PHONE not granted")
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "call failed")
        }
    }
}
