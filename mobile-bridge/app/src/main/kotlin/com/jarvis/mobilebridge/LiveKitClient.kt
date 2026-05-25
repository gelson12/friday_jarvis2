package com.jarvis.mobilebridge

import android.content.Context
import android.util.Log
import io.livekit.android.LiveKit
import io.livekit.android.events.RoomEvent
import io.livekit.android.events.collect
import io.livekit.android.room.Room
import io.livekit.android.room.track.DataPublishReliability
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * Holds the LiveKit Room connection. Reconnects on disconnect with
 * exponential backoff. Filters inbound packets to the `mobile-cmd`
 * topic, dispatches via CommandRouter, publishes `mobile-result`.
 */
class LiveKitClient(
    private val ctx: Context,
    private val onStatus: (String) -> Unit,
) {
    private val tag = "LiveKitClient"
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var room: Room? = null
    private var lifecycleJob: Job? = null
    private val router = CommandRouter(ctx)

    fun start() {
        if (lifecycleJob != null) return
        lifecycleJob = scope.launch { lifecycle() }
    }

    fun stop() {
        lifecycleJob?.cancel()
        lifecycleJob = null
        scope.launch { room?.disconnect() }
        room = null
        onStatus("Disconnected")
    }

    private suspend fun lifecycle() {
        var backoffMs = 1000L
        while (true) {
            try {
                onStatus("Fetching token…")
                val creds = TokenFetcher.fetch(ctx)
                if (creds == null || creds.serverUrl.isEmpty() || creds.token.isEmpty()) {
                    onStatus("Token endpoint failed — check settings")
                    delay(backoffMs.coerceAtMost(30_000))
                    backoffMs = (backoffMs * 2).coerceAtMost(30_000)
                    continue
                }
                onStatus("Connecting…")
                val r = LiveKit.create(appContext = ctx.applicationContext)
                r.connect(creds.serverUrl, creds.token)
                room = r
                onStatus("Connected to Jarvis")
                backoffMs = 1000L
                consumeEvents(r)
            } catch (e: Exception) {
                Log.w(tag, "connection lost", e)
                onStatus("Reconnecting in ${backoffMs / 1000}s")
                try { room?.disconnect() } catch (_: Exception) {}
                room = null
                delay(backoffMs)
                backoffMs = (backoffMs * 2).coerceAtMost(30_000)
            }
        }
    }

    private suspend fun consumeEvents(r: Room) {
        r.events.collect { event ->
            if (event is RoomEvent.Disconnected) {
                throw RuntimeException("room disconnected: ${event.reason}")
            }
            if (event !is RoomEvent.DataReceived) return@collect
            if (event.topic != TOPIC_CMD) return@collect
            val packet = event.data
            try {
                val msg = JSONObject(String(packet, Charsets.UTF_8))
                val target = msg.optString("target").lowercase()
                val me = Config.machineName(ctx).lowercase()
                if (target !in listOf(me, "phone", "mobile", "all", "any", "")) return@collect
                val cmdId = msg.optString("id")
                val cmd = msg.optString("cmd")
                val args = msg.optJSONObject("args") ?: JSONObject()
                Log.i(tag, "exec $cmd $args")
                val result = router.execute(cmd, args)
                val reply = JSONObject()
                    .put("id", cmdId)
                    .put("machine", me)
                    .put("result", result)
                r.localParticipant.publishData(
                    reply.toString().toByteArray(Charsets.UTF_8),
                    reliability = DataPublishReliability.RELIABLE,
                    topic = TOPIC_RESULT,
                )
            } catch (e: Exception) {
                Log.e(tag, "command handling failed", e)
            }
        }
    }

    companion object {
        const val TOPIC_CMD = "mobile-cmd"
        const val TOPIC_RESULT = "mobile-result"
    }
}
