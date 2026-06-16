package com.jarvis.mobilebridge

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.util.Log

/**
 * The keystone of self-revival. A reconnect *loop* only runs while the process is
 * alive; once an aggressive OEM (ColorOS/MIUI/EMUI) kills the process, nothing
 * in-process can bring it back. This receiver is owned by the SYSTEM, so Android
 * delivers to it even after our process was killed — and it simply restarts the
 * foreground service (which reconnects to LiveKit on its own).
 *
 * It fires from four independent sources so a kill can never be permanent:
 *   1) device boot / our APK being replaced  (system broadcasts above)
 *   2) a repeating exact alarm we re-arm on every fire  (fast revive, ~1 min)
 *   3) WorkManager's periodic KeepAliveWorker            (guaranteed, ~15 min)
 *   4) BridgeService.onTaskRemoved (user swiped the app away)
 */
class RestartReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        Log.i("RestartReceiver", "revive trigger: ${intent?.action ?: ACTION_RESTART}")
        try {
            BridgeService.start(context)
        } catch (e: Exception) {
            Log.w("RestartReceiver", "service start failed", e)
        }
        // Always re-arm the next alarm — an exact alarm is one-shot, so each fire must
        // schedule the next one to keep the watchdog ticking.
        scheduleNext(context)
    }

    companion object {
        const val ACTION_RESTART = "com.jarvis.mobilebridge.RESTART"
        private const val REQ = 0x4A52
        // ~1 minute. Doze may stretch it, but combined with WorkManager (15 min) and a
        // foreground service the gap after any kill stays small.
        private const val INTERVAL_MS = 60_000L

        private fun pending(ctx: Context): PendingIntent {
            val i = Intent(ctx, RestartReceiver::class.java).setAction(ACTION_RESTART)
            return PendingIntent.getBroadcast(
                ctx, REQ, i,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
        }

        /** Arm (or re-arm) the watchdog alarm. setExactAndAllowWhileIdle fires even in
         * Doze; we re-arm on each delivery so it repeats. Fail-soft — a missing
         * exact-alarm grant must never crash the service. */
        fun scheduleNext(ctx: Context) {
            val am = ctx.getSystemService(Context.ALARM_SERVICE) as? AlarmManager ?: return
            val at = SystemClock.elapsedRealtime() + INTERVAL_MS
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && !am.canScheduleExactAlarms()) {
                    // No exact-alarm grant — fall back to an inexact (but Doze-tolerant) alarm.
                    am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pending(ctx))
                    return
                }
                am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pending(ctx))
            } catch (_: SecurityException) {
                try { am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pending(ctx)) }
                catch (_: Exception) {}
            } catch (_: Exception) {}
        }
    }
}
