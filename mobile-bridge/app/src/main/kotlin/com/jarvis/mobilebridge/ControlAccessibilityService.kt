package com.jarvis.mobilebridge

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

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

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (!ScreenShare.awaitingScreenConsent) return
        if (System.currentTimeMillis() > ScreenShare.consentDeadlineMs) {
            ScreenShare.awaitingScreenConsent = false
            return
        }
        val t = event?.eventType ?: return
        if (t != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED &&
            t != AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED) return
        try { autoAcceptConsent() } catch (_: Exception) {}
    }

    /** Walk the MediaProjection "screen recording" consent dialog and accept it:
     *  open the "Single app" spinner, choose "Entire screen", then tap "Start now".
     *  Driven across successive window-content events by ScreenShare.consentStage so each
     *  step fires once the previous one has rendered. Best-effort + fail-soft: if the texts
     *  don't match (locale/OEM), nothing happens and the owner just taps Start themselves. */
    private fun autoAcceptConsent() {
        when (ScreenShare.consentStage) {
            0 -> {
                val spinner = findByTexts(SINGLE_APP_TEXTS)
                if (spinner != null) { clickNode(spinner); ScreenShare.consentStage = 1; return }
                // No spinner (already entire-screen / no chooser): try entire-screen, else Start.
                val entire = findByTexts(ENTIRE_SCREEN_TEXTS)
                if (entire != null) { clickNode(entire); ScreenShare.consentStage = 2; return }
                val start = findByTexts(START_TEXTS)
                if (start != null) { clickNode(start); ScreenShare.awaitingScreenConsent = false }
            }
            1 -> {
                val entire = findByTexts(ENTIRE_SCREEN_TEXTS)
                if (entire != null) { clickNode(entire); ScreenShare.consentStage = 2 }
            }
            2 -> {
                val start = findByTexts(START_TEXTS)
                if (start != null) { clickNode(start); ScreenShare.awaitingScreenConsent = false }
            }
        }
    }

    private fun roots(): List<AccessibilityNodeInfo> {
        val out = ArrayList<AccessibilityNodeInfo>()
        rootInActiveWindow?.let { out.add(it) }
        try { windows?.forEach { w -> w.root?.let { out.add(it) } } } catch (_: Exception) {}
        return out
    }

    private fun findByTexts(texts: Array<String>): AccessibilityNodeInfo? {
        for (root in roots()) {
            for (q in texts) {
                val hits = try { root.findAccessibilityNodeInfosByText(q) } catch (_: Exception) { null }
                val n = hits?.firstOrNull { it != null && it.isVisibleToUser }
                    ?: hits?.firstOrNull()
                if (n != null) return n
            }
        }
        return null
    }

    /** Click a node: prefer ACTION_CLICK on it or a clickable ancestor; else tap its centre. */
    private fun clickNode(node: AccessibilityNodeInfo) {
        var n: AccessibilityNodeInfo? = node
        var hops = 0
        while (n != null && hops < 6) {
            if (n.isClickable) {
                if (n.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return
            }
            n = n.parent; hops++
        }
        val r = Rect(); node.getBoundsInScreen(r)
        if (r.width() > 0 && r.height() > 0) {
            val p = Path().apply { moveTo(r.exactCenterX(), r.exactCenterY()) }
            try {
                dispatchGesture(
                    GestureDescription.Builder()
                        .addStroke(GestureDescription.StrokeDescription(p, 0, 60)).build(),
                    null, null,
                )
            } catch (_: Exception) {}
        }
    }

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

    /** Draw an unlock PATTERN on the secure keyguard bouncer by tracing one continuous
     *  gesture through the 3x3 dot grid in the given digit order (e.g. "321478965").
     *  This ENTERS the pattern exactly as the owner would — it NEVER changes or clears the
     *  stored credential. The grid is located from the LockPatternView node when possible
     *  (auto-adapts to any device), else from a normalised override, else a heuristic.
     *  Digit grid:  1 2 3 / 4 5 6 / 7 8 9 . Returns whether the gesture was dispatched. */
    fun drawPattern(digits: String, gridNorm: DoubleArray? = null): Boolean {
        val seq = digits.filter { it in '1'..'9' }
        if (seq.length < 2) return false
        val (w, h) = wh()
        val rect: Rect = when {
            gridNorm != null && gridNorm.size == 4 && gridNorm[2] > gridNorm[0] && gridNorm[3] > gridNorm[1] ->
                Rect((gridNorm[0] * w).toInt(), (gridNorm[1] * h).toInt(),
                     (gridNorm[2] * w).toInt(), (gridNorm[3] * h).toInt())
            else -> findPatternRect() ?: run {
                // Heuristic fallback: a centred square in the lower-middle (ColorOS-typical).
                val gw = (w * 0.80f).toInt()
                val left = (w - gw) / 2
                val top = (h * 0.42f).toInt()
                val bottom = (h * 0.82f).toInt()
                Rect(left, top, left + gw, bottom)
            }
        }
        val cw = rect.width() / 3f
        val ch = rect.height() / 3f
        val path = Path()
        seq.forEachIndexed { i, c ->
            val d = c - '0'
            val col = (d - 1) % 3
            val row = (d - 1) / 3
            val x = rect.left + cw * (col + 0.5f)
            val y = rect.top + ch * (row + 0.5f)
            if (i == 0) path.moveTo(x, y) else path.lineTo(x, y)
        }
        return try {
            // Slow stroke (1.4s) so the pattern view registers every dot it crosses.
            dispatchGesture(
                GestureDescription.Builder()
                    .addStroke(GestureDescription.StrokeDescription(path, 0, 1400)).build(),
                null, null,
            )
        } catch (_: Exception) { false }
    }

    private fun findPatternRect(): Rect? {
        for (root in roots()) {
            val n = findByClassName(root, "LockPatternView") ?: continue
            val r = Rect(); n.getBoundsInScreen(r)
            if (r.width() > 0 && r.height() > 0) return r
        }
        return null
    }

    private fun findByClassName(root: AccessibilityNodeInfo, cls: String): AccessibilityNodeInfo? {
        val q = ArrayDeque<AccessibilityNodeInfo>()
        q.add(root)
        var hops = 0
        while (q.isNotEmpty() && hops < 5000) {
            val n = q.removeFirst(); hops++
            if ((n.className?.toString() ?: "").contains(cls, ignoreCase = true)) return n
            for (i in 0 until n.childCount) n.getChild(i)?.let { q.add(it) }
        }
        return null
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

        // Consent-dialog button labels (English + a few PT fallbacks). findAccessibilityNodeInfosByText
        // is a case-insensitive substring match, so "Start" also catches "Start now".
        private val SINGLE_APP_TEXTS = arrayOf("Single app", "single app", "Um único app", "app único")
        private val ENTIRE_SCREEN_TEXTS = arrayOf("Entire screen", "entire screen", "Whole screen",
            "Tela inteira", "Ecrã inteiro", "Todo o ecrã")
        private val START_TEXTS = arrayOf("Start now", "Start recording", "Start", "Iniciar agora",
            "Começar agora", "Iniciar", "Começar")
    }
}
