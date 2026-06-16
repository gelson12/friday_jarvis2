package com.jarvis.mobilebridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat

/**
 * Foreground service keeping the LiveKit connection alive when the
 * screen is off. Standard Android pattern: persistent notification
 * (required >= API 26), START_STICKY restart, single LiveKitClient
 * instance for the service lifetime.
 */
class BridgeService : Service() {
    private lateinit var client: LiveKitClient
    @Volatile private var status: String = "Starting…"

    override fun onCreate() {
        super.onCreate()
        instance = this
        ensureChannel()
        // Start WITHOUT mediaProjection — Android 14 kills a mediaProjection-typed
        // FGS that has no active projection token. We promote to it only once the
        // user grants screen capture (applyForegroundTypes(true)).
        applyForegroundTypes(mediaProjection = false)
        client = LiveKitClient(this) { newStatus ->
            status = newStatus
            getSystemService(NotificationManager::class.java)
                ?.notify(NOTIF_ID, buildNotification(newStatus))
        }
        client.start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Arm both watchdogs on every start (idempotent): the ~1-min exact alarm and the
        // 15-min WorkManager job. Either one re-launches us if the OS kills the process.
        RestartReceiver.scheduleNext(this)
        KeepAliveWorker.enqueue(this)
        return START_STICKY
    }

    /** ColorOS (and most aggressive OEMs) kill the service when the user swipes the app
     * off the recents screen. Schedule an immediate restart so it comes straight back. */
    override fun onTaskRemoved(rootIntent: Intent?) {
        RestartReceiver.scheduleNext(this)
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        instance = null
        client.stop()
        // If we're being torn down (low-memory kill, OEM cleanup), make sure a wake-up is
        // pending so the service is resurrected rather than gone for good.
        RestartReceiver.scheduleNext(this)
        super.onDestroy()
    }

    /** Re-assert the foreground state with the right service types. On Android 14+
     * the type must be explicit AND mediaProjection only while a projection is
     * active; older versions take the types from the manifest, so the plain
     * 2-arg call is enough. Fail-soft — never let this crash the service. */
    fun applyForegroundTypes(mediaProjection: Boolean) {
        val notif = buildNotification(status)
        try {
            if (Build.VERSION.SDK_INT >= 34) {
                var types = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC or
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA or
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
                if (mediaProjection) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
                ServiceCompat.startForeground(this, NOTIF_ID, notif, types)
            } else {
                startForeground(NOTIF_ID, notif)
            }
        } catch (e: Exception) {
            try { startForeground(NOTIF_ID, notif) } catch (_: Exception) {}
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = getSystemService(NotificationManager::class.java) ?: return
        if (mgr.getNotificationChannel(CHANNEL_ID) != null) return
        mgr.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID, "Jarvis Mobile Bridge",
                NotificationManager.IMPORTANCE_LOW,
            ).apply { setShowBadge(false) }
        )
    }

    private fun buildNotification(text: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentTitle("Jarvis Mobile Bridge")
            .setContentText(text)
            .setContentIntent(openIntent)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .build()
    }

    companion object {
        const val CHANNEL_ID = "jarvis_bridge_channel"
        const val NOTIF_ID = 0x4A52  // "JR" — anything stable + non-zero

        /** The running service, so LiveKitClient can promote/demote the FGS type
         * around screen capture without binding. */
        @Volatile var instance: BridgeService? = null

        /** Promote (active=true) or demote the foreground service to/from the
         * mediaProjection type. No-op if the service isn't running. */
        fun setMediaProjection(ctx: Context, active: Boolean) {
            instance?.applyForegroundTypes(active)
        }

        fun start(ctx: Context) {
            val i = Intent(ctx, BridgeService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ctx.startForegroundService(i) else ctx.startService(i)
        }
        fun stop(ctx: Context) { ctx.stopService(Intent(ctx, BridgeService::class.java)) }
    }
}
