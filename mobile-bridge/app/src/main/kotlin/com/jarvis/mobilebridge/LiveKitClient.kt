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
    // True once the user has turned screen-share on, until they turn it off. While true we
    // (a) skip the proactive 5-min refresh (it would tear the screen down) and (b) re-arm
    // the screen track automatically after any reconnect — so the consent popup appears at
    // most once (and zero times if the PROJECT_MEDIA app-op is granted via ADB).
    @Volatile private var wantScreenShare = false
    private var screenCapturer: ScreenCapturer? = null
    @Volatile private var screenRoom: Room? = null  // current publish target for screen frames

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
                // If a screen-share was running before this (re)connect, bring it straight
                // back without bothering the user for another consent tap.
                if (wantScreenShare) restoreScreenShare(r)
                // Cellular carrier NAT silently kills an idle socket and the SDK doesn't
                // always notice → the app shows "Connected" but the server dropped it (a
                // zombie/half-open connection, so commands never arrive). keepAlive keeps
                // the NAT mapping alive; the REFRESH ceiling proactively reconnects so a
                // half-open can never linger more than a few minutes — EXCEPT while
                // screen-sharing, when a proactive reconnect would kill the screen track,
                // so we then lean on keepAlive + event-driven reconnect instead.
                val ka = scope.launch { keepAlive(r) }
                try {
                    if (wantScreenShare) {
                        consumeEvents(r)
                    } else {
                        withTimeoutOrNull(REFRESH_MS) { consumeEvents(r) }
                    }
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
                        // Promote the FGS to the microphone type BEFORE publishing (A14+),
                        // demote after stopping — the service starts as plain dataSync now.
                        if (cmd == "mic_on") BridgeService.setMicType(true)
                        r.localParticipant.setMicrophoneEnabled(cmd == "mic_on")
                        if (cmd == "mic_off") BridgeService.setMicType(false)
                        JSONObject().put("mic", if (cmd == "mic_on") "on" else "off")
                    } catch (e: Exception) {
                        JSONObject().put("error", e.message ?: "mic toggle failed")
                    }
                    "camera_on", "camera_off" -> try {
                        if (cmd == "camera_on") BridgeService.setCameraType(true)
                        r.localParticipant.setCameraEnabled(cmd == "camera_on")
                        if (cmd == "camera_off") BridgeService.setCameraType(false)
                        JSONObject().put("camera", if (cmd == "camera_on") "on" else "off")
                    } catch (e: Exception) {
                        JSONObject().put("error", e.message ?: "camera toggle failed — grant the Camera permission, sir")
                    }
                    // Screen share via MediaProjection — needs a one-time on-screen
                    // consent tap (Android security), so the first screen_on returns
                    // "requested" and the track goes live once the user approves.
                    "screen_on" -> try {
                        wantScreenShare = true
                        val pend = ScreenShare.pending
                        if (pend != null) {
                            enableScreenShare(r, pend.first, pend.second)
                            JSONObject().put("screen", "on")
                        } else {
                            ScreenShare.onGranted = { rc, data ->
                                scope.launch {
                                    val sres = try {
                                        enableScreenShare(r, rc, data)
                                        JSONObject().put("screen", "on")
                                    } catch (e: Exception) {
                                        Log.w(tag, "screen share enable failed", e)
                                        ScreenShare.log("enableScreenShare FAILED: ${e.message ?: e.toString()}")
                                        JSONObject().put("screen", "error")
                                            .put("detail", (e.message ?: e.toString()).take(400))
                                    }
                                    // Report the post-consent result back over the room (the app's
                                    // own logs are filtered on ColorOS), so failures are visible.
                                    try {
                                        r.localParticipant.publishData(
                                            JSONObject().put("id", "screen_grant")
                                                .put("machine", Config.machineName(ctx).lowercase())
                                                .put("result", sres).toString().toByteArray(Charsets.UTF_8),
                                            reliability = DataPublishReliability.RELIABLE,
                                            topic = TOPIC_RESULT,
                                        )
                                    } catch (_: Exception) {}
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
                        wantScreenShare = false
                        try { screenCapturer?.stop() } catch (_: Exception) {}
                        screenCapturer = null
                        ScreenShare.pending = null
                        ScreenShare.onGranted = null
                        JSONObject().put("screen", "off")
                    } catch (e: Exception) {
                        JSONObject().put("error", e.message ?: "screen stop failed")
                    }
                    // APP-ONLY remote control via the accessibility service (no laptop/ADB).
                    // Coords are normalised (0..1) from the dashboard.
                    "phone_tap" -> {
                        val svc = ControlAccessibilityService.instance
                        if (svc == null) JSONObject().put("error", "enable Jarvis under Settings → Accessibility, sir")
                        else { svc.tapNorm(args.optDouble("nx", 0.5), args.optDouble("ny", 0.5)); JSONObject().put("tapped", true) }
                    }
                    "phone_swipe" -> {
                        val svc = ControlAccessibilityService.instance
                        if (svc == null) JSONObject().put("error", "enable Jarvis under Settings → Accessibility, sir")
                        else {
                            svc.swipeNorm(args.optDouble("nx1", 0.5), args.optDouble("ny1", 0.5),
                                args.optDouble("nx2", 0.5), args.optDouble("ny2", 0.5),
                                args.optLong("ms", 180L))
                            JSONObject().put("swiped", true)
                        }
                    }
                    "phone_key" -> {
                        val svc = ControlAccessibilityService.instance
                        if (svc == null) JSONObject().put("error", "enable Jarvis under Settings → Accessibility, sir")
                        else JSONObject().put("key", svc.globalKey(args.optString("key")))
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

    /** Custom MediaProjection capturer (NOT the SDK's setScreenShareEnabled, which can't
     * satisfy Android 14's FGS ordering). Captures the screen + publishes JPEG frames on
     * the data channel for the dashboard to render. Frames go to `screenRoom` so a reconnect
     * can re-point them without re-prompting for consent. */
    private fun enableScreenShare(r: Room, resultCode: Int, data: Intent) {
        ScreenShare.log("custom capturer: starting (rc=$resultCode)")
        screenRoom = r
        try { screenCapturer?.stop() } catch (_: Exception) {}
        var frameN = 0
        val cap = ScreenCapturer(ctx) { jpeg ->
            frameN++
            if (frameN <= 3 || frameN % 15 == 0) ScreenShare.log("frame #$frameN size=${jpeg.size}")
            val rr = screenRoom ?: return@ScreenCapturer
            scope.launch {
                try {
                    rr.localParticipant.publishData(
                        jpeg, reliability = DataPublishReliability.RELIABLE, topic = TOPIC_SCREEN_FRAME)
                } catch (e: Exception) {
                    if (frameN <= 3) ScreenShare.log("publish frame#$frameN failed: ${e.javaClass.simpleName}: ${e.message}")
                }
            }
        }
        cap.start(resultCode, data, maxW = 400, fps = 3)
        screenCapturer = cap
        ScreenShare.log("custom capturer: virtual display up, streaming frames")
    }

    /** After a reconnect: if the custom capturer is still running (MediaProjection survives
     * a LiveKit reconnect), just RE-POINT the frames to the new room — no re-consent. Only
     * if it's not running do we re-request. */
    private fun restoreScreenShare(r: Room) {
        if (screenCapturer?.running == true) {
            screenRoom = r
            ScreenShare.log("reconnect: re-pointed screen frames to the new room")
            return
        }
        ScreenShare.onGranted = { rc, data ->
            scope.launch { try { enableScreenShare(r, rc, data) } catch (e: Exception) { Log.w(tag, "screen re-enable failed", e) } }
        }
        ScreenShare.request(ctx)
    }

    companion object {
        const val TOPIC_CMD = "mobile-cmd"
        const val TOPIC_RESULT = "mobile-result"
        const val TOPIC_KEEPALIVE = "keepalive"
        const val TOPIC_SCREEN_FRAME = "phone-frame"  // JPEG frames from the app-only capturer
        const val KEEPALIVE_MS = 15_000L        // < typical carrier NAT idle timeout
        const val REFRESH_MS = 5 * 60_000L      // proactively reconnect every 5 min
        private val KEEPALIVE_PAYLOAD = "ka".toByteArray(Charsets.UTF_8)
    }
}
