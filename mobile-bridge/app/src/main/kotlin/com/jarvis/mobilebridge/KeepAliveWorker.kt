package com.jarvis.mobilebridge

import android.content.Context
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/**
 * WorkManager periodic worker: the OS schedules and runs it in a FRESH process even
 * after ours was killed, so it can resurrect the bridge service. 15 minutes is the
 * platform minimum for periodic work; the exact alarm in RestartReceiver covers the
 * faster (~1 min) path, and WorkManager is the durable guarantee that survives
 * reboots, app-standby buckets and process death.
 */
class KeepAliveWorker(ctx: Context, params: WorkerParameters) : Worker(ctx, params) {
    override fun doWork(): Result {
        try {
            BridgeService.start(applicationContext)
            RestartReceiver.scheduleNext(applicationContext)
        } catch (_: Exception) {
            return Result.retry()
        }
        return Result.success()
    }

    companion object {
        private const val NAME = "jarvis_bridge_keepalive"

        /** Idempotent — KEEP policy means repeated calls don't pile up duplicate work. */
        fun enqueue(ctx: Context) {
            val req = PeriodicWorkRequestBuilder<KeepAliveWorker>(15, TimeUnit.MINUTES)
                .build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                NAME, ExistingPeriodicWorkPolicy.KEEP, req,
            )
        }
    }
}
