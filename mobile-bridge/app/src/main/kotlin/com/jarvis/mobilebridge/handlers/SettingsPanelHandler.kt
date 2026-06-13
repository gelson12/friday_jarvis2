package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
import org.json.JSONObject

/**
 * Wi-Fi / hotspot. Android 10+ FORBIDS apps from toggling these programmatically (security),
 * so the honest best we can do is open the relevant panel/settings for a one-tap. We say so in
 * the reply note rather than pretending we flipped it.
 */
object SettingsPanelHandler {
    fun wifi(ctx: Context, @Suppress("UNUSED_PARAMETER") args: JSONObject): JSONObject {
        return try {
            val i = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
                Intent(Settings.Panel.ACTION_WIFI)
            else
                Intent(Settings.ACTION_WIFI_SETTINGS)
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
            JSONObject().put("wifi_panel_opened", true)
                .put("note", "Android blocks apps from toggling Wi-Fi directly — opened the panel for a tap.")
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "cannot open Wi-Fi panel")
        }
    }

    fun hotspot(ctx: Context, @Suppress("UNUSED_PARAMETER") args: JSONObject): JSONObject {
        // The tethering screen has no single standard intent — the component name
        // differs by OEM (OPPO/ColorOS, Samsung, Pixel…). Try the known ones in
        // order and launch the first that resolves.
        val candidates = listOf(
            Intent("android.settings.WIFI_AP_SETTINGS"),
            Intent().setClassName("com.android.settings", "com.android.settings.TetherSettings"),
            Intent().setClassName(
                "com.android.settings", "com.android.settings.Settings\$TetherSettingsActivity"),
            Intent().setClassName(
                "com.android.settings", "com.android.settings.wifi.tether.WifiTetherSettings"),
            Intent(Settings.ACTION_WIRELESS_SETTINGS),
            Intent(Settings.ACTION_SETTINGS),
        )
        for (i in candidates) {
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            try {
                ctx.startActivity(i)
                return JSONObject().put("hotspot_settings_opened", true)
                    .put("note", "Android blocks apps from toggling the hotspot — opened the settings for a tap.")
            } catch (_: Exception) { /* try the next candidate */ }
        }
        return JSONObject().put("error", "couldn't open hotspot settings on this device")
    }
}
