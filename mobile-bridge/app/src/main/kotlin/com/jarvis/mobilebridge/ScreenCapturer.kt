package com.jarvis.mobilebridge

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.HandlerThread
import java.io.ByteArrayOutputStream

/**
 * APP-ONLY screen capture (no laptop). Captures the screen via MediaProjection + an
 * ImageReader and emits downscaled JPEG frames — which LiveKitClient publishes over the
 * data channel for the dashboard to render as an <img>. We DON'T use the LiveKit SDK's
 * setScreenShareEnabled (its WebRTC path can't satisfy Android 14's foreground-service
 * ordering). Here we control the order exactly:
 *   getMediaProjection → startForeground(mediaProjection) → registerCallback → VirtualDisplay.
 */
class ScreenCapturer(
    private val ctx: Context,
    private val onFrame: (ByteArray) -> Unit,
) {
    private var projection: MediaProjection? = null
    private var vDisplay: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var lastEmit = 0L
    @Volatile var running = false
        private set

    fun start(resultCode: Int, data: Intent, maxW: Int = 480, fps: Int = 3) {
        val mpm = ctx.getSystemService(MediaProjectionManager::class.java)
        val proj = mpm.getMediaProjection(resultCode, data)
            ?: throw IllegalStateException("getMediaProjection returned null")
        // Android 14 ORDER: FGS must be mediaProjection-typed AFTER the projection is
        // obtained and BEFORE the VirtualDisplay is created.
        BridgeService.setMediaProjection(ctx, true)
        thread = HandlerThread("jarvis-screencap").apply { start() }
        handler = Handler(thread!!.looper)
        proj.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { stop() }
        }, handler)

        val dm = ctx.resources.displayMetrics
        val sw = dm.widthPixels
        val sh = dm.heightPixels
        val dpi = dm.densityDpi
        val scale = minOf(1f, maxW.toFloat() / sw)
        val w = ((sw * scale).toInt() / 2) * 2
        val h = ((sh * scale).toInt() / 2) * 2
        val minInterval = (1000L / fps).coerceAtLeast(80L)

        reader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        reader!!.setOnImageAvailableListener({ r ->
            val img = try { r.acquireLatestImage() } catch (_: Exception) { null } ?: return@setOnImageAvailableListener
            try {
                val now = System.currentTimeMillis()
                if (now - lastEmit >= minInterval) {
                    lastEmit = now
                    val plane = img.planes[0]
                    val pixStride = plane.pixelStride
                    val rowStride = plane.rowStride
                    val rowPadding = rowStride - pixStride * w
                    val bmpW = w + (if (pixStride > 0) rowPadding / pixStride else 0)
                    val bmp = Bitmap.createBitmap(bmpW, h, Bitmap.Config.ARGB_8888)
                    bmp.copyPixelsFromBuffer(plane.buffer)
                    val out = if (bmpW != w) Bitmap.createBitmap(bmp, 0, 0, w, h) else bmp
                    val baos = ByteArrayOutputStream()
                    out.compress(Bitmap.CompressFormat.JPEG, 45, baos)
                    onFrame(baos.toByteArray())
                    if (out !== bmp) out.recycle()
                    bmp.recycle()
                }
            } catch (_: Exception) {
            } finally {
                img.close()
            }
        }, handler)

        vDisplay = proj.createVirtualDisplay(
            "jarvis-screen", w, h, dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader!!.surface, null, handler,
        )
        projection = proj
        running = true
    }

    fun stop() {
        running = false
        try { vDisplay?.release() } catch (_: Exception) {}
        try { reader?.close() } catch (_: Exception) {}
        try { projection?.stop() } catch (_: Exception) {}
        try { thread?.quitSafely() } catch (_: Exception) {}
        vDisplay = null; reader = null; projection = null; thread = null; handler = null
        try { BridgeService.setMediaProjection(ctx, false) } catch (_: Exception) {}
    }
}
