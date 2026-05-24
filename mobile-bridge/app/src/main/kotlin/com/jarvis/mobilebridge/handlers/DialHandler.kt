package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import android.net.Uri
import org.json.JSONObject

/**
 * Opens the dialer pre-filled with the given number. Does NOT auto-call —
 * Android security: only the default phone app may silently place calls,
 * and our user-app shouldn't be one.
 */
object DialHandler {
    fun execute(ctx: Context, args: JSONObject): JSONObject {
        val number = args.optString("number").trim()
        if (number.isEmpty()) return JSONObject().put("error", "number is required")
        return try {
            val i = Intent(Intent.ACTION_DIAL, Uri.parse("tel:$number"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
            JSONObject().put("dialer_opened", true).put("number", number)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "dial failed")
        }
    }
}
