package com.jarvis.mobilebridge

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * OPTIONAL auto-send for WhatsApp.
 *
 * WhatsApp has no API to send a message on a personal account, so WhatsAppHandler
 * opens the chat pre-filled and the user taps send. When THIS service is enabled
 * (Settings → Accessibility → Jarvis Mobile Bridge), WhatsAppHandler.arm()s it just
 * before opening the chat; the service then taps WhatsApp's Send button so the
 * message goes out hands-free.
 *
 * It does NOTHING unless a send was just requested (guarded by [pendingUntil]), and
 * it is scoped to com.whatsapp only (see res/xml/whatsapp_send_service.xml) — so it
 * never reads any other app. Best-effort: a WhatsApp UI change may need re-tuning.
 */
class WhatsAppSendService : AccessibilityService() {

    companion object {
        @Volatile
        var pendingUntil = 0L

        /** Arm a one-shot auto-send for the next few seconds. */
        fun arm(windowMs: Long = 9000L) {
            pendingUntil = System.currentTimeMillis() + windowMs
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (System.currentTimeMillis() > pendingUntil) return
        val root = rootInActiveWindow ?: return
        if (root.packageName?.toString() != "com.whatsapp") return
        val btn = root.findAccessibilityNodeInfosByViewId("com.whatsapp:id/send")
            ?.firstOrNull { it.isClickable }
            ?: findClickableByDesc(root, "Send")
        if (btn != null) {
            btn.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            pendingUntil = 0L  // one-shot: don't re-tap
        }
    }

    private fun findClickableByDesc(node: AccessibilityNodeInfo?, desc: String): AccessibilityNodeInfo? {
        if (node == null) return null
        if (node.isClickable && node.contentDescription?.toString()?.equals(desc, true) == true) {
            return node
        }
        for (i in 0 until node.childCount) {
            val hit = findClickableByDesc(node.getChild(i), desc)
            if (hit != null) return hit
        }
        return null
    }

    override fun onInterrupt() {}
}
