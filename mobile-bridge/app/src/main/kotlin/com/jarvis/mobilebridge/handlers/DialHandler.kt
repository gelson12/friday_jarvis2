package com.jarvis.mobilebridge.handlers

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.telecom.TelecomManager
import androidx.core.content.ContextCompat
import org.json.JSONObject

/**
 * Places a phone call.
 *
 * Uses TelecomManager.placeCall — which dials through the device's DEFAULT phone app
 * DIRECTLY, with no "Open with" chooser. (ACTION_CALL pops that chooser whenever other
 * apps — Viber, Zoom — also register for tel:, which is exactly what the owner hit.)
 * Falls back to opening the dialer pre-filled (ACTION_DIAL) if CALL_PHONE isn't granted
 * or placeCall fails, so the command always degrades gracefully.
 */
object DialHandler {
    fun execute(ctx: Context, args: JSONObject): JSONObject {
        val number = args.optString("number").trim()
        if (number.isEmpty()) return JSONObject().put("error", "number is required")
        val dialOnly = args.optBoolean("dial_only", false)
        val canCall = !dialOnly && ContextCompat.checkSelfPermission(
            ctx, Manifest.permission.CALL_PHONE
        ) == PackageManager.PERMISSION_GRANTED

        if (canCall) {
            try {
                val tm = ctx.getSystemService(Context.TELECOM_SERVICE) as TelecomManager
                tm.placeCall(Uri.fromParts("tel", number, null), Bundle())
                return JSONObject().put("calling", true).put("number", number)
            } catch (e: SecurityException) {
                // fall through to the dialer
            } catch (e: Exception) {
                // fall through to the dialer
            }
        }

        return try {
            ctx.startActivity(
                Intent(Intent.ACTION_DIAL, Uri.parse("tel:$number"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            JSONObject().put("dialer_opened", true).put("number", number)
                .put("note", if (dialOnly) "dialer opened" else
                    "grant the Phone (CALL_PHONE) permission to place calls automatically")
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "call failed")
        }
    }
}
