package com.jarvis.mobilebridge

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import java.io.File

/**
 * Transparent, no-UI activity whose only job is to pop the system MediaProjection consent
 * and hand the granted result to [ScreenShare]. Finishes immediately either way.
 *
 * Uses a PLAIN Activity + the classic startActivityForResult/onActivityResult — which is
 * the most reliable way to get the consent result back for a background-launched activity
 * (registerForActivityResult was silently not firing on ColorOS). Plain Activity also
 * avoids the AppCompat-theme crash under the translucent theme. Writes a small file log
 * (filesDir/screen.log) so the flow is debuggable via `adb pull` (ColorOS filters logcat).
 */
class ScreenCaptureActivity : Activity() {

    private fun log(m: String) {
        try { File(filesDir, "screen.log").appendText("${System.currentTimeMillis()} $m\n") } catch (_: Exception) {}
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        log("onCreate")
        try {
            val mpm = getSystemService(MediaProjectionManager::class.java)
            @Suppress("DEPRECATION")
            startActivityForResult(mpm.createScreenCaptureIntent(), REQ)
            log("consent launched")
        } catch (e: Exception) {
            log("launch failed: ${e.message}")
            ScreenShare.lastDenied = true
            finish()
        }
    }

    @Deprecated("classic result API is intentional here")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        log("onActivityResult rc=$resultCode hasData=${data != null}")
        if (requestCode == REQ && resultCode == Activity.RESULT_OK && data != null) {
            ScreenShare.deliver(resultCode, data)
        } else {
            ScreenShare.lastDenied = true
        }
        finish()
    }

    companion object {
        private const val REQ = 0x5C  // arbitrary
    }
}
