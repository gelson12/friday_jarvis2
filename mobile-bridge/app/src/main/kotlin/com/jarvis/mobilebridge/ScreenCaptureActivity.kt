package com.jarvis.mobilebridge

import android.app.Activity
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts

/**
 * Transparent, no-UI activity whose only job is to pop the system MediaProjection
 * consent dialog and hand the granted result to [ScreenShare]. Finishes itself
 * immediately either way, so nothing covers the screen the user wants to share.
 *
 * Extends ComponentActivity (NOT AppCompatActivity): it carries a translucent
 * non-AppCompat theme, and AppCompatActivity hard-crashes with "You need to use a
 * Theme.AppCompat theme" under such a theme. ComponentActivity still provides
 * registerForActivityResult and works with any theme.
 */
class ScreenCaptureActivity : ComponentActivity() {

    private val launcher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val data = result.data
        if (result.resultCode == Activity.RESULT_OK && data != null) {
            ScreenShare.deliver(result.resultCode, data)
        } else {
            ScreenShare.lastDenied = true
        }
        finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        try {
            val mpm = getSystemService(MediaProjectionManager::class.java)
            launcher.launch(mpm.createScreenCaptureIntent())
        } catch (e: Exception) {
            ScreenShare.lastDenied = true
            finish()
        }
    }
}
