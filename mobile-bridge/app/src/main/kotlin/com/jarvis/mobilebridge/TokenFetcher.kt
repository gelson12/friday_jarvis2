package com.jarvis.mobilebridge

import android.content.Context
import android.util.Log
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * POSTs to the worker's LIVEKIT_TOKEN_ENDPOINT with the bridge token
 * and the machine identity. Mirrors the desktop-bridge token-fetch
 * flow (see desktop-bridge/bridge.py _fetch_token_via_http) so the
 * worker only needs ONE token-issuing endpoint for all bridges.
 *
 * Response shape: {"serverUrl": "wss://...", "token": "<JWT>"}
 */
object TokenFetcher {
    private const val TAG = "TokenFetcher"
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    data class TokenResult(val serverUrl: String, val token: String)

    fun fetch(ctx: Context): TokenResult? {
        val endpoint = Config.tokenEndpoint(ctx)
        val bridgeToken = Config.bridgeToken(ctx)
        val machine = "mobile-bridge-" + Config.machineName(ctx)
        if (endpoint.isEmpty() || bridgeToken.isEmpty()) {
            Log.w(TAG, "token endpoint or bridge token not configured")
            return null
        }
        val body = JSONObject()
            .put("machine", machine)
            .put("control_room", Config.controlRoom(ctx))
            .toString()
            .toRequestBody("application/json".toMediaTypeOrNull())
        val req = Request.Builder()
            .url(endpoint)
            .post(body)
            .header("Authorization", "Bearer $bridgeToken")
            .build()
        return try {
            http.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) {
                    Log.w(TAG, "token endpoint returned ${resp.code}")
                    return@use null
                }
                val txt = resp.body?.string().orEmpty()
                val json = JSONObject(txt)
                TokenResult(
                    serverUrl = json.optString("serverUrl"),
                    token = json.optString("token"),
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "token fetch failed", e)
            null
        }
    }
}
