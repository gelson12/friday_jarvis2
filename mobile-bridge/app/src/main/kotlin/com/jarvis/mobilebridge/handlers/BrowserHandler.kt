package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import android.net.Uri
import org.json.JSONObject

/**
 * Opens any URL in the default browser. Worker uses this for "search
 * YouTube for cats" → youtube.com/results?... or "show me John's
 * Instagram" → instagram.com/john.
 */
object BrowserHandler {
    fun openUrl(ctx: Context, args: JSONObject): JSONObject {
        var url = args.optString("url").trim()
        if (url.isEmpty()) return JSONObject().put("error", "url is required")
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "https://$url"
        }
        return try {
            val i = Intent(Intent.ACTION_VIEW, Uri.parse(url))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
            JSONObject().put("opened_url", url)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "open failed")
        }
    }
}
