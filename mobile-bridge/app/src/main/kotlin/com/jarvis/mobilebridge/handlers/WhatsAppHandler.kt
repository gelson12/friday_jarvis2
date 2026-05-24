package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import android.net.Uri
import org.json.JSONObject
import java.net.URLEncoder

/**
 * Opens WhatsApp at a Click-to-Chat URL with the message pre-filled.
 * User taps send manually — there is no official personal-account API
 * for fully automatic sending. Phase 3 could add an Accessibility-
 * service auto-tap; deferred until v1 is solid.
 *
 *   https://wa.me/<digits-only>?text=<urlencoded>
 */
object WhatsAppHandler {
    fun send(ctx: Context, args: JSONObject): JSONObject {
        val rawNumber = args.optString("number").trim()
        val message = args.optString("message")
        if (rawNumber.isEmpty() || message.isEmpty()) {
            return JSONObject().put("error", "number and message are required")
        }
        // wa.me expects digits only (no +, spaces, or dashes).
        val digits = rawNumber.filter { it.isDigit() }
        if (digits.isEmpty()) return JSONObject().put("error", "invalid number")
        val url = "https://wa.me/$digits?text=" + URLEncoder.encode(message, "UTF-8")
        return try {
            val i = Intent(Intent.ACTION_VIEW, Uri.parse(url))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
            JSONObject()
                .put("whatsapp_opened", true)
                .put("note", "tap send to complete")
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "open failed")
        }
    }
}
