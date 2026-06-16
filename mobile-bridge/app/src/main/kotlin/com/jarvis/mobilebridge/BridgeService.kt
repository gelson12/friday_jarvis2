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
        // Go foreground as plain dataSync ONLY. Starting with camera/microphone (or
        // mediaProjection) FGS types on Android 14/15 throws — especially on a background
        // revival — so startForeground() never completes and the OS kills us with
        // "ForegroundServiceDidNotStartInTimeException" (which the revival watchdog then
        // turned into a crash-loop). We add those capture types ONLY while a capture is
        // actually live (addType/dropType).
        startFg()
        client = LiveKitClient(this) { newStatus ->
            status = newStatus
            getSystemService(NotificationManager::class.java)
                ?.notify(NOTIF_ID, buildNotification(newStatus))
        }
        client.start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Re-assert foreground on EVERY (re)start so a repeated startForegroundService()
        // call (the alarm/WorkManager revival fires one each cycle) can never trip the
        // "did not start in time" kill.
        startFg()
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

    /** Capture types beyond the always-on dataSync (camera/microphone/mediaProjection),
     * OR'd in only while a capture is live. */
    @Volatile private var extraTypes = 0

    /** Go (or stay) foreground. Always at least dataSync — which is allowed even on a
     * background revival of a battery-exempt app — plus whatever capture types are
     * currently live. If the system rejects the capture types (e.g. a background
     * camera/mic start on Android 14+), we degrade to dataSync rather than crash:
     * startForeground MUST succeed or the OS kills us with "did not start in time". */
    private fun startFg() {
        val notif = buildNotification(status)
        if (Build.VERSION.SDK_INT < 34) {
            try { startForeground(NOTIF_ID, notif) } catch (_: Exception) {}
            return
        }
        val want = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC or extraTypes
        for (t in intArrayOf(want, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)) {
            try {
                ServiceCompat.startForeground(this, NOTIF_ID, notif, t)
                if (extraTypes != 0) ScreenShare.log("FGS startForeground OK types=0x${t.toString(16)}")
                return
            } catch (e: Exception) {
                if (extraTypes != 0) ScreenShare.log("FGS startForeground(0x${t.toString(16)}) rejected: ${e.javaClass.simpleName}: ${e.message}")
                android.util.Log.w("BridgeService", "startForeground($t) rejected", e)
            }
        }
        try { startForeground(NOTIF_ID, notif) } catch (_: Exception) {}
    }

    private fun addType(t: Int) { extraTypes = extraTypes or t; startFg() }
    private fun dropType(t: Int) { extraTypes = extraTypes and t.inv(); startFg() }

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

        /** Add/remove the mediaProjection FGS type around screen capture (must be active
         * BEFORE setScreenShareEnabled on Android 14+). No-op if the service isn't running. */
        fun setMediaProjection(ctx: Context, active: Boolean) {
            instance?.let {
                if (active) it.addType(ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
                else it.dropType(ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
            }
        }

        /** Add/remove the microphone FGS type around mic publishing (Android 14+ needs it). */
        fun setMicType(active: Boolean) {
            instance?.let {
                if (active) it.addType(ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
                else it.dropType(ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            }
        }

        /** Add/remove the camera FGS type around camera publishing (Android 14+ needs it). */
        fun setCameraType(active: Boolean) {
            instance?.let {
                if (active) it.addType(ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA)
                else it.dropType(ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA)
            }
        }

        fun start(ctx: Context) {
            val i = Intent(ctx, BridgeService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ctx.startForegroundService(i) else ctx.startService(i)
        }
        fun stop(ctx: Context) { ctx.stopService(Intent(ctx, BridgeService::class.java)) }
    }
}
