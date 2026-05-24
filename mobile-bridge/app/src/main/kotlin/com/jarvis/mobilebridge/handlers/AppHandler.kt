package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject

object AppHandler {

    /**
     * Open an installed app. Args:
     *   package  (preferred — exact bundle id)
     *   name     (fallback — fuzzy label match across installed apps)
     */
    fun open(ctx: Context, args: JSONObject): JSONObject {
        val pkg = args.optString("package").trim()
        val name = args.optString("name").trim()
        val pm = ctx.packageManager

        val target = pkg.ifEmpty {
            if (name.isEmpty()) return JSONObject().put("error", "package or name required")
            findPackageByLabel(pm, name)
                ?: return JSONObject().put("error", "no app matched '$name'")
        }
        val launch = pm.getLaunchIntentForPackage(target)
            ?: return JSONObject().put("error", "no launcher activity for '$target'")
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(launch)
        return JSONObject().put("opened", target)
    }

    /**
     * List installed user-visible apps. Returns {apps: [{label, package}]}.
     * Caps at 200 entries so the worker's data-channel reply stays small.
     */
    fun list(ctx: Context, args: JSONObject): JSONObject {
        val pm = ctx.packageManager
        val out = JSONArray()
        val pkgs = pm.getInstalledApplications(PackageManager.GET_META_DATA)
        for (info in pkgs) {
            if (out.length() >= 200) break
            // Skip pure system apps the user never sees.
            if ((info.flags and ApplicationInfo.FLAG_SYSTEM) != 0
                && (info.flags and ApplicationInfo.FLAG_UPDATED_SYSTEM_APP) == 0
                && pm.getLaunchIntentForPackage(info.packageName) == null
            ) continue
            val label = pm.getApplicationLabel(info).toString()
            out.put(JSONObject().put("label", label).put("package", info.packageName))
        }
        return JSONObject().put("apps", out)
    }

    /**
     * Open Play Store at a package listing so the user can tap Install.
     * (Silent install requires REQUEST_INSTALL_PACKAGES + sideload — out of scope.)
     */
    fun install(ctx: Context, args: JSONObject): JSONObject {
        val pkg = args.optString("package").trim()
        if (pkg.isEmpty()) return JSONObject().put("error", "package is required")
        return try {
            val i = Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=$pkg"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
            JSONObject().put("playstore_opened", true).put("package", pkg)
        } catch (e: Exception) {
            // Fallback to the web URL if Play Store isn't installed.
            try {
                val web = Intent(Intent.ACTION_VIEW,
                    Uri.parse("https://play.google.com/store/apps/details?id=$pkg"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                ctx.startActivity(web)
                JSONObject().put("playstore_opened", true).put("package", pkg).put("via", "web")
            } catch (e2: Exception) {
                JSONObject().put("error", "could not open Play Store: ${e2.message}")
            }
        }
    }

    /**
     * Fire Android's "Uninstall this app?" dialog for the given package.
     */
    @Suppress("DEPRECATION")  // ACTION_DELETE works fine; ACTION_UNINSTALL_PACKAGE needs API 14
    fun uninstall(ctx: Context, args: JSONObject): JSONObject {
        val pkg = args.optString("package").trim()
        if (pkg.isEmpty()) return JSONObject().put("error", "package is required")
        return try {
            val i = Intent(Intent.ACTION_DELETE, Uri.parse("package:$pkg"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
            JSONObject().put("uninstall_prompted", true).put("package", pkg)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "uninstall failed")
        }
    }

    private fun findPackageByLabel(pm: PackageManager, query: String): String? {
        val q = query.lowercase().trim()
        // Common social-app shorthand → package shortcuts (avoids a full
        // pm.getInstalledApplications() scan on every open command).
        val aliases = mapOf(
            "instagram" to "com.instagram.android",
            "insta" to "com.instagram.android",
            "tiktok" to "com.zhiliaoapp.musically",
            "tik tok" to "com.zhiliaoapp.musically",
            "facebook" to "com.facebook.katana",
            "messenger" to "com.facebook.orca",
            "whatsapp" to "com.whatsapp",
            "youtube" to "com.google.android.youtube",
            "chrome" to "com.android.chrome",
            "browser" to "com.android.chrome",
            "spotify" to "com.spotify.music",
            "gmail" to "com.google.android.gm",
            "maps" to "com.google.android.apps.maps",
            "calendar" to "com.google.android.calendar",
        )
        aliases[q]?.let {
            // Confirm the alias is actually installed.
            return try { pm.getApplicationInfo(it, 0); it } catch (_: Exception) { null }
        }
        // Fallback: scan installed apps by label.
        var bestPkg: String? = null
        var bestScore = -1
        for (info in pm.getInstalledApplications(0)) {
            val label = pm.getApplicationLabel(info).toString().lowercase()
            val score = when {
                label == q -> 100
                label.startsWith(q) -> 80
                label.contains(q) -> 60
                else -> -1
            }
            if (score > bestScore) {
                bestScore = score
                bestPkg = info.packageName
            }
        }
        return bestPkg
    }
}
