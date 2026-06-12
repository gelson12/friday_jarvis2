package com.jarvis.mobilebridge

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import android.os.Build

/**
 * Persistent config for the bridge — token-endpoint, BRIDGE_TOKEN,
 * machine name. Stored in EncryptedSharedPreferences so the bridge
 * token never sits in plain XML.
 */
object Config {
    private const val PREFS = "mobile_bridge_prefs"

    private fun prefs(ctx: Context) = EncryptedSharedPreferences.create(
        ctx,
        PREFS,
        MasterKey.Builder(ctx).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    fun get(ctx: Context, key: String, default: String = "") =
        prefs(ctx).getString(key, default) ?: default

    fun set(ctx: Context, key: String, value: String) {
        prefs(ctx).edit().putString(key, value).apply()
    }

    fun tokenEndpoint(ctx: Context) = get(ctx, "token_endpoint")
    fun bridgeToken(ctx: Context) = get(ctx, "bridge_token")
    fun machineName(ctx: Context) = get(
        ctx, "machine_name",
        default = Build.MODEL.lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-'),
    )
    fun controlRoom(ctx: Context) = get(ctx, "control_room", default = "jarvis-control")

    /** The Jarvis HUD URL — the SAME web app served on Railway. Derived from the token
     *  endpoint ("<base>/api/bridge" -> "<base>") unless an explicit ui_url override is saved. */
    fun uiUrl(ctx: Context): String {
        val override = get(ctx, "ui_url").trim()
        if (override.isNotBlank()) return override
        val ep = tokenEndpoint(ctx).trim()
        return if (ep.contains("/api/")) ep.substringBefore("/api/") else ep.trimEnd('/')
    }
}
