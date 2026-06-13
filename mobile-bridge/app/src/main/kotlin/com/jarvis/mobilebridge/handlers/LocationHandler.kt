package com.jarvis.mobilebridge.handlers

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.location.Geocoder
import android.location.LocationManager
import android.provider.Settings
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.util.Locale

/**
 * Current device location + the Location settings panel.
 *
 * `get`  — last-known fix across LocationManager providers, reverse-geocoded to an
 *          address. Needs ACCESS_FINE/COARSE_LOCATION (requested by MainActivity).
 * `panel`— Android forbids apps from silently toggling the location radio, so this
 *          just opens Location settings for a tap (same as Wi-Fi/hotspot).
 */
object LocationHandler {
    fun get(ctx: Context, @Suppress("UNUSED_PARAMETER") args: JSONObject): JSONObject {
        val fine = ContextCompat.checkSelfPermission(
            ctx, Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        val coarse = ContextCompat.checkSelfPermission(
            ctx, Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        if (!fine && !coarse) {
            return JSONObject().put("error", "location permission not granted")
                .put("note", "grant Location to the app in Settings, sir")
        }
        return try {
            val lm = ctx.getSystemService(Context.LOCATION_SERVICE) as LocationManager
            val loc = lm.allProviders
                .mapNotNull { runCatching { lm.getLastKnownLocation(it) }.getOrNull() }
                .maxByOrNull { it.time }
                ?: return JSONObject().put(
                    "error", "no recent location fix — open Maps once to refresh it, sir"
                )
            val out = JSONObject()
                .put("lat", loc.latitude)
                .put("lng", loc.longitude)
                .put("accuracy_m", loc.accuracy.toInt())
            try {
                @Suppress("DEPRECATION")
                Geocoder(ctx, Locale.getDefault())
                    .getFromLocation(loc.latitude, loc.longitude, 1)
                    ?.firstOrNull()?.let { a ->
                        val parts = listOfNotNull(
                            a.thoroughfare, a.locality, a.adminArea, a.countryName
                        )
                        if (parts.isNotEmpty()) out.put("address", parts.joinToString(", "))
                    }
            } catch (_: Exception) { /* geocoder offline — coords still returned */ }
            out
        } catch (e: SecurityException) {
            JSONObject().put("error", "location permission not granted")
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "couldn't get location")
        }
    }

    fun panel(ctx: Context): JSONObject {
        return try {
            ctx.startActivity(
                Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            JSONObject().put("location_panel_opened", true)
                .put("note", "Android blocks apps from toggling location — opened the panel for a tap.")
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "cannot open location settings")
        }
    }
}
