package com.jarvis.mobilebridge

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.view.accessibility.AccessibilityEvent

/**
 * APP-ONLY remote control (no laptop/ADB needed). Once the owner enables this under
 * Settings → Accessibility, the bridge can inject taps/swipes onto ANY app via
 * dispatchGesture, and Back/Home/Recents via performGlobalAction — which is how the
 * dashboard drives the phone when there's no laptop to do it over ADB.
 *
 * Coordinates arrive NORMALISED (0..1) from the dashboard (it doesn't know the phone's
 * resolution); we scale to the real display here.
 */
class ControlAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        instance = this
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) { /* not used */ }
    override fun onInterrupt() { /* not used */ }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    private fun wh(): Pair<Int, Int> {
        val dm = resources.displayMetrics
        return dm.widthPixels to dm.heightPixels
    }

    fun tapNorm(nx: Double, ny: Double) {
        val (w, h) = wh()
        val x = (nx.coerceIn(0.0, 1.0) * w).toFloat()
        val y = (ny.coerceIn(0.0, 1.0) * h).toFloat()
        val p = Path().apply { moveTo(x, y) }
        try {
            dispatchGesture(
                GestureDescription.Builder()
                    .addStroke(GestureDescription.StrokeDescription(p, 0, 60)).build(),
                null, null,
            )
        } catch (_: Exception) {}
    }

    fun swipeNorm(nx1: Double, ny1: Double, nx2: Double, ny2: Double, ms: Long) {
        val (w, h) = wh()
        val p = Path().apply {
            moveTo((nx1 * w).toFloat(), (ny1 * h).toFloat())
            lineTo((nx2 * w).toFloat(), (ny2 * h).toFloat())
        }
        try {
            dispatchGesture(
                GestureDescription.Builder()
                    .addStroke(GestureDescription.StrokeDescription(p, 0, ms.coerceIn(40, 1200)))
                    .build(),
                null, null,
            )
        } catch (_: Exception) {}
    }

    fun globalKey(key: String): Boolean {
        val action = when (key.lowercase()) {
            "back" -> GLOBAL_ACTION_BACK
            "home" -> GLOBAL_ACTION_HOME
            "recents" -> GLOBAL_ACTION_RECENTS
            "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
            else -> return false
        }
        return try { performGlobalAction(action) } catch (_: Exception) { false }
    }

    companion object {
        @Volatile var instance: ControlAccessibilityService? = null
        fun enabled(): Boolean = instance != null
    }
}
