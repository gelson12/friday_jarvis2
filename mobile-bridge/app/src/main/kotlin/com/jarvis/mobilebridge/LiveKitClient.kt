package com.jarvis.mobilebridge

import android.content.Context
import android.content.Intent
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
import kotlinx.coroutines.withTimeoutOrNull
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
                // Cellular carrier NAT silently kills an idle socket and the SDK doesn't
                // always notice → the app shows "Connected" but the server dropped it (a
                // zombie/half-open connection, so commands never arrive). keepAlive keeps
                // the NAT mapping alive; the REFRESH ceiling proactively reconnects so a
                // half-open can never linger more than a few minutes.
                val ka = scope.launch { keepAlive(r) }
                try {
                    withTimeoutOrNull(REFRESH_MS) { consumeEvents(r) }
                } finally {
                    ka.cancel()
                }
                Log.i(tag, "periodic refresh — reconnecting")
                try { r.disconnect() } catch (_: Exception) {}
                room = null
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
                // Mic/camera publishing needs the Room's localParticipant, which the
                // handlers (ctx-only) can't reach — so handle them HERE. Enabling
                // publishes a track into jarvis-control; the web HUD subscribes to
                // see/hear it. Android always shows the mic/camera privacy dot.
                val result = when (cmd) {
                    "mic_on", "mic_off" -> try {
                        r.localParticipant.setMicrophoneEnabled(cmd == "mic_on")
                        JSONObject().put("mic", if (cmd == "mic_on") "on" else "off")
                    } catch (e: Exception) {
                        JSONObject().put("error", e.message ?: "mic toggle failed")
                    }
                    "camera_on", "camera_off" -> try {
                        r.localParticipant.setCameraEnabled(cmd == "camera_on")
                        JSONObject().put("camera", if (cmd == "camera_on") "on" else "off")
                    } catch (e: Exception) {
                        JSONObject().put("error", e.message ?: "camera toggle failed — grant the Camera permission, sir")
                    }
                    // Screen share via MediaProjection — needs a one-time on-screen
                    // consent tap (Android security), so the first screen_on returns
                    // "requested" and the track goes live once the user approves.
                    "screen_on" -> try {
                        val pend = ScreenShare.pending
                        if (pend != null) {
                            enableScreenShare(r, pend.second)
                            JSONObject().put("screen", "on")
                        } else {
                            ScreenShare.onGranted = { _, data ->
                                scope.launch {
                                    try { enableScreenShare(r, data) }
                                    catch (e: Exception) { Log.w(tag, "screen share enable failed", e) }
                                }
                            }
                            ScreenShare.request(ctx)
                            JSONObject().put("screen", "requested")
                                .put("note", "approve the screen-capture prompt on the phone, sir")
                        }
                    } catch (e: Exception) {
                        JSONObject().put("error", e.message ?: "screen share failed")
                    }
                    "screen_off" -> try {
                        r.localParticipant.setScreenShareEnabled(false)
                        ScreenShare.pending = null
                        ScreenShare.onGranted = null
                        BridgeService.setMediaProjection(ctx, false)
                        JSONObject().put("screen", "off")
                    } catch (e: Exception) {
                        JSONObject().put("error", e.message ?: "screen stop failed")
                    }
                    else -> router.execute(cmd, args)
                }
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

    /** Heartbeat that keeps the cellular NAT mapping alive so the signaling socket
     * can't go idle-dead. If a publish fails the socket is gone, so we disconnect
     * to make the lifecycle reconnect (instead of sitting as a zombie). */
    private suspend fun keepAlive(r: Room) {
        while (true) {
            delay(KEEPALIVE_MS)
            try {
                r.localParticipant.publishData(
                    KEEPALIVE_PAYLOAD,
                    reliability = DataPublishReliability.LOSSY,
                    topic = TOPIC_KEEPALIVE,
                )
            } catch (e: Exception) {
                Log.w(tag, "keepalive failed — dropping to reconnect", e)
                try { r.disconnect() } catch (_: Exception) {}
                return
            }
        }
    }

    /** Promote the foreground service to the mediaProjection type (Android 14
     * needs this active BEFORE capture starts), then publish the screen track. */
    private suspend fun enableScreenShare(r: Room, data: Intent) {
        BridgeService.setMediaProjection(ctx, true)
        r.localParticipant.setScreenShareEnabled(true, data)
    }

    companion object {
        const val TOPIC_CMD = "mobile-cmd"
        const val TOPIC_RESULT = "mobile-result"
        const val TOPIC_KEEPALIVE = "keepalive"
        const val KEEPALIVE_MS = 15_000L        // < typical carrier NAT idle timeout
        const val REFRESH_MS = 5 * 60_000L      // proactively reconnect every 5 min
        private val KEEPALIVE_PAYLOAD = "ka".toByteArray(Charsets.UTF_8)
    }
}
