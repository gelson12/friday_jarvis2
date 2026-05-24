package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.provider.Telephony
import android.telephony.SmsManager
import org.json.JSONArray
import org.json.JSONObject

object SmsHandler {

    /**
     * Read recent SMS. Args:
     *   limit          (int, default 10)
     *   number_filter  (optional substring on the address)
     *   since_minutes  (optional cutoff)
     */
    fun list(ctx: Context, args: JSONObject): JSONObject {
        val limit = args.optInt("limit", 10).coerceIn(1, 100)
        val numberFilter = args.optString("number_filter", "").trim()
        val sinceMinutes = args.optInt("since_minutes", 0)

        val projection = arrayOf(
            Telephony.Sms.ADDRESS,
            Telephony.Sms.BODY,
            Telephony.Sms.DATE,
            Telephony.Sms.READ,
            Telephony.Sms.TYPE,
        )
        val where = StringBuilder()
        val whereArgs = mutableListOf<String>()
        if (numberFilter.isNotEmpty()) {
            where.append("${Telephony.Sms.ADDRESS} LIKE ?")
            whereArgs.add("%$numberFilter%")
        }
        if (sinceMinutes > 0) {
            if (where.isNotEmpty()) where.append(" AND ")
            where.append("${Telephony.Sms.DATE} > ?")
            whereArgs.add((System.currentTimeMillis() - sinceMinutes * 60_000L).toString())
        }

        val messages = JSONArray()
        ctx.contentResolver.query(
            Telephony.Sms.CONTENT_URI,
            projection,
            if (where.isEmpty()) null else where.toString(),
            if (whereArgs.isEmpty()) null else whereArgs.toTypedArray(),
            "${Telephony.Sms.DATE} DESC LIMIT $limit",
        )?.use { c ->
            val iAddr = c.getColumnIndexOrThrow(Telephony.Sms.ADDRESS)
            val iBody = c.getColumnIndexOrThrow(Telephony.Sms.BODY)
            val iDate = c.getColumnIndexOrThrow(Telephony.Sms.DATE)
            val iRead = c.getColumnIndexOrThrow(Telephony.Sms.READ)
            val iType = c.getColumnIndexOrThrow(Telephony.Sms.TYPE)
            while (c.moveToNext()) {
                val type = c.getInt(iType)  // 1 = inbox, 2 = sent
                messages.put(
                    JSONObject()
                        .put("from", c.getString(iAddr) ?: "")
                        .put("body", c.getString(iBody) ?: "")
                        .put("date_ms", c.getLong(iDate))
                        .put("read", c.getInt(iRead) != 0)
                        .put("direction", if (type == 2) "outbound" else "inbound")
                )
            }
        }
        return JSONObject().put("messages", messages)
    }

    /**
     * Send an SMS. Args:
     *   number   (required)
     *   message  (required)
     */
    fun send(ctx: Context, args: JSONObject): JSONObject {
        val number = args.optString("number").trim()
        val message = args.optString("message")
        if (number.isEmpty() || message.isEmpty()) {
            return JSONObject().put("error", "number and message are required")
        }
        return try {
            val sms = ctx.getSystemService(SmsManager::class.java) ?: SmsManager.getDefault()
            val parts = sms.divideMessage(message)
            if (parts.size == 1) {
                sms.sendTextMessage(number, null, message, null, null)
            } else {
                sms.sendMultipartTextMessage(number, null, parts, null, null)
            }
            JSONObject().put("sent", true).put("parts", parts.size)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "send failed")
        }
    }
}
