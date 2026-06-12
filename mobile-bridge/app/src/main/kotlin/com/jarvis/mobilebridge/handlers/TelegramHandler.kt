package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import android.net.Uri
import org.json.JSONObject

/**
 * Telegram messaging. Telegram's intents are less rich than WhatsApp's: there's no reliable
 * "prefill text to a specific contact" deep link. So:
 *   - username given  -> open that @username chat (tg://resolve), user types/sends.
 *   - otherwise       -> share the text into Telegram's chooser (user picks the chat).
 * Honest limit: we can't fully auto-send to a named contact the way WhatsApp's wa.me allows.
 */
object TelegramHandler {
    private const val PKG = "org.telegram.messenger"

    fun send(ctx: Context, args: JSONObject): JSONObject {
        val username = args.optString("username").trim().removePrefix("@")
        val text = args.optString("text").ifEmpty { args.optString("message") }
        return try {
            if (username.isNotEmpty()) {
                val i = Intent(Intent.ACTION_VIEW, Uri.parse("tg://resolve?domain=$username"))
                    .setPackage(PKG)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                ctx.startActivity(i)
                JSONObject().put("telegram_chat_opened", username)
                    .put("note", "Opened the chat; type/send (Telegram blocks auto-send to a contact).")
            } else if (text.isNotEmpty()) {
                val i = Intent(Intent.ACTION_SEND).setType("text/plain")
                    .putExtra(Intent.EXTRA_TEXT, text)
                    .setPackage(PKG)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                ctx.startActivity(i)
                JSONObject().put("telegram_share", true)
            } else {
                val i = ctx.packageManager.getLaunchIntentForPackage(PKG)
                    ?: return JSONObject().put("error", "Telegram is not installed")
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                ctx.startActivity(i)
                JSONObject().put("telegram_opened", true)
            }
        } catch (e: Exception) {
            JSONObject().put("error", "Telegram not installed or unavailable: ${e.message}")
        }
    }
}
