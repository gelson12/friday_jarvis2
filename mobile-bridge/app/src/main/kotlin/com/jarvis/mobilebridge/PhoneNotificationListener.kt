package com.jarvis.mobilebridge

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONArray
import org.json.JSONObject

/**
 * Process-global cache of the phone's active notifications, written by the
 * listener and read by NotificationsHandler (both live in the bridge process).
 */
object NotificationStore {
    @Volatile var connected: Boolean = false
    private val lock = Any()
    private val items = LinkedHashMap<String, JSONObject>()   // keyed by sbn.key

    fun put(key: String, o: JSONObject) {
        synchronized(lock) {
            items.remove(key)        // re-insert so newest-updated sorts last
            items[key] = o
        }
    }

    fun remove(key: String) { synchronized(lock) { items.remove(key) } }
    fun clear() { synchronized(lock) { items.clear() } }

    fun snapshot(limit: Int): JSONArray {
        synchronized(lock) {
            val arr = JSONArray()
            items.values.toList().asReversed().take(limit).forEach { arr.put(it) }   // newest first
            return arr
        }
    }
}

/**
 * Captures posted notifications so the dashboard can show them. Requires the user
 * to grant "Notification access" once (Settings → Notifications → Notification
 * access). Our own ongoing service notification is filtered out.
 */
class PhoneNotificationListener : NotificationListenerService() {

    override fun onListenerConnected() {
        NotificationStore.connected = true
        try {
            NotificationStore.clear()
            activeNotifications?.forEach { record(it) }
        } catch (_: Exception) {
        }
    }

    override fun onListenerDisconnected() {
        NotificationStore.connected = false
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        sbn?.let { record(it) }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {
        sbn?.let { NotificationStore.remove(it.key) }
    }

    private fun record(sbn: StatusBarNotification) {
        try {
            if (sbn.packageName == packageName) return            // skip our own bridge notice
            val e = sbn.notification?.extras
            val title = e?.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
            val text = e?.getCharSequence(Notification.EXTRA_TEXT)?.toString().orEmpty()
            if (title.isBlank() && text.isBlank()) return
            NotificationStore.put(
                sbn.key,
                JSONObject()
                    .put("app", appLabel(sbn.packageName))
                    .put("package", sbn.packageName)
                    .put("title", title)
                    .put("text", text)
                    .put("time_ms", sbn.postTime)
                    .put("ongoing", sbn.isOngoing)
            )
        } catch (_: Exception) {
        }
    }

    private fun appLabel(pkg: String): String = try {
        packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
    } catch (_: Exception) {
        pkg
    }
}
