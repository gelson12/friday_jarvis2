package com.jarvis.mobilebridge.handlers

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CallLog
import androidx.core.content.ContextCompat
import org.json.JSONObject

/**
 * Redial — dials the most recent call-log number. Needs READ_CALL_LOG (+ CALL_PHONE
 * for the actual placing, via DialHandler). Optional args.type =
 * "missed" | "outgoing" | "incoming" picks the most recent of that kind.
 *
 * The placing reuses DialHandler, so it auto-calls when CALL_PHONE is granted and
 * otherwise opens the dialer pre-filled.
 */
object CallLogHandler {
    fun dialRecent(ctx: Context, args: JSONObject): JSONObject {
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.READ_CALL_LOG)
            != PackageManager.PERMISSION_GRANTED
        ) {
            return JSONObject().put("error", "call-log permission not granted")
                .put("note", "grant Call logs to the app in Settings, sir")
        }
        val type = when (args.optString("type").lowercase()) {
            "missed" -> CallLog.Calls.MISSED_TYPE
            "outgoing", "dialed", "dialled" -> CallLog.Calls.OUTGOING_TYPE
            "incoming", "received" -> CallLog.Calls.INCOMING_TYPE
            else -> -1
        }
        val sel = if (type >= 0) "${CallLog.Calls.TYPE}=?" else null
        val selArgs = if (type >= 0) arrayOf(type.toString()) else null
        var number = ""
        var name = ""
        try {
            ctx.contentResolver.query(
                CallLog.Calls.CONTENT_URI,
                arrayOf(CallLog.Calls.NUMBER, CallLog.Calls.CACHED_NAME),
                sel, selArgs,
                "${CallLog.Calls.DATE} DESC"
            )?.use { cur ->
                if (cur.moveToFirst()) {
                    number = cur.getString(0) ?: ""
                    name = cur.getString(1) ?: ""
                }
            }
        } catch (e: SecurityException) {
            return JSONObject().put("error", "call-log permission not granted")
        }
        if (number.isBlank()) return JSONObject().put("error", "no recent call found")
        val res = DialHandler.execute(ctx, JSONObject().put("number", number))
        if (name.isNotBlank()) res.put("name", name)
        return res
    }
}
