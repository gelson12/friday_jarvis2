package com.jarvis.mobilebridge

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity

/**
 * Full-screen WebView that loads the Jarvis HUD — the SAME web UI served on Railway — so the
 * phone IS Jarvis: the conscious core, the panels, and voice. We grant the microphone permission
 * the LiveKit voice agent requests, and let media autoplay so Jarvis can speak.
 */
class JarvisActivity : AppCompatActivity() {

    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val url = intent.getStringExtra("url")?.takeIf { it.isNotBlank() } ?: Config.uiUrl(this)
        if (url.isBlank()) {
            Toast.makeText(this, "Set the token endpoint URL first, sir.", Toast.LENGTH_LONG).show()
            finish(); return
        }

        web = WebView(this).apply {
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT,
            )
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                mediaPlaybackRequiresUserGesture = false   // let Jarvis speak without a tap
                cacheMode = WebSettings.LOAD_DEFAULT
                useWideViewPort = true
                loadWithOverviewMode = true
                userAgentString = "$userAgentString JarvisMobileBridge"
            }
            webViewClient = WebViewClient()
            webChromeClient = object : WebChromeClient() {
                override fun onPermissionRequest(request: PermissionRequest) {
                    // Grant mic / media so the LiveKit voice agent works inside the WebView.
                    runOnUiThread { request.grant(request.resources) }
                }
            }
        }
        setContentView(web)
        web.loadUrl(url)

        // Back button navigates the web history before leaving Jarvis.
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })
    }

    override fun onDestroy() {
        if (this::web.isInitialized) web.destroy()
        super.onDestroy()
    }
}
