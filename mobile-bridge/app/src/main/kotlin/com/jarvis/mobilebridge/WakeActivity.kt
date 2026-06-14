package com.jarvis.mobilebridge

import android.app.KeyguardManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.appcompat.app.AppCompatActivity

/**
 * Best-effort remote wake / unlock. Turns the display on and shows over the lock
 * screen; if `dismiss` is set, asks the keyguard to dismiss.
 *
 * HARD LIMIT (Android security): `requestDismissKeyguard` clears only a NON-secure
 * (swipe) lock, or one already satisfied by Smart Lock / on-body. A PIN, pattern,
 * password or biometric CANNOT be entered or bypassed by any app — there is no
 * such API. For remote screen-share, keep the phone on swipe/Smart-Lock.
 */
class WakeActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
                    WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
            )
        }
        val dismiss = intent.getBooleanExtra("dismiss", false)
        if (dismiss) {
            try {
                getSystemService(KeyguardManager::class.java)?.requestDismissKeyguard(this, null)
            } catch (_: Exception) {
            }
        }
        // Auto-finish so we don't sit on top of the screen the user wants to see/share.
        // Short for a dismiss (reveal the screen), a few seconds for a plain wake.
        window.decorView.postDelayed({ finish() }, if (dismiss) 1500L else 6000L)
    }
}
