package com.jarvis.mobilebridge.handlers

import android.content.Context
import com.jarvis.mobilebridge.NotificationStore
import org.json.JSONObject

/**
 * Returns the phone's current notifications (cached by PhoneNotificationListener).
 * If notification access hasn't been granted the list is empty + access_granted
 * is false, so the dashboard can prompt the user to enable it.
 */
object NotificationsHandler {
    fun list(ctx: Context, args: JSONObject): JSONObject {
        val limit = args.optInt("limit", 30).coerceIn(1, 100)
        val out = JSONObject()
            .put("notifications", NotificationStore.snapshot(limit))
            .put("access_granted", NotificationStore.connected)
        if (!NotificationStore.connected) {
            out.put("note", "grant Notification access to Jarvis Mobile Bridge in " +
                "Settings → Notifications → Notification access")
        }
        return out
    }

    fun clear(ctx: Context, args: JSONObject): JSONObject {
        NotificationStore.clear()
        return JSONObject().put("cleared", true)
    }
}
