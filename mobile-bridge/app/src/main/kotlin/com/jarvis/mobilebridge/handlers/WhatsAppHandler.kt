package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import com.jarvis.mobilebridge.WhatsAppSendService
import org.json.JSONObject
import java.net.URLEncoder

/**
 * Opens WhatsApp at a Click-to-Chat URL with the message pre-filled.
 *
 * WhatsApp has no personal-account send API, so by default the user taps send.
 * If the optional WhatsAppSendService accessibility service is ENABLED
 * (Settings → Accessibility → Jarvis Mobile Bridge), we arm a one-shot auto-send
 * just before opening the chat, and it taps Send for true hands-free messaging.
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
            val autoSend = isAutoSendEnabled(ctx)
            if (autoSend) WhatsAppSendService.arm()
            ctx.startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            JSONObject()
                .put("whatsapp_opened", true)
                .put("auto_send", autoSend)
                .put("note", if (autoSend) "auto-sending" else "tap send to complete")
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "open failed")
        }
    }

    /** True when the user has enabled our WhatsApp auto-send accessibility service. */
    private fun isAutoSendEnabled(ctx: Context): Boolean {
        val flat = Settings.Secure.getString(
            ctx.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        ) ?: return false
        return flat.contains("${ctx.packageName}/", ignoreCase = true) &&
            flat.contains("WhatsAppSendService", ignoreCase = true)
    }
}
