package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.provider.ContactsContract
import org.json.JSONArray
import org.json.JSONObject

object ContactsHandler {

    /**
     * Search contacts by display name. Args:
     *   query  (substring, required)
     *   limit  (int, default 10)
     */
    fun search(ctx: Context, args: JSONObject): JSONObject {
        val query = args.optString("query").trim()
        val limit = args.optInt("limit", 10).coerceIn(1, 50)
        if (query.isEmpty()) {
            return JSONObject().put("error", "query is required")
        }
        val out = JSONArray()
        val proj = arrayOf(
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER,
            ContactsContract.CommonDataKinds.Phone.TYPE,
        )
        ctx.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            proj,
            "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ?",
            arrayOf("%$query%"),
            "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} ASC LIMIT $limit",
        )?.use { c ->
            val iName = c.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME)
            val iNum = c.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.NUMBER)
            val iType = c.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.TYPE)
            val seen = mutableSetOf<String>()
            while (c.moveToNext()) {
                val name = c.getString(iName) ?: continue
                val number = (c.getString(iNum) ?: "").replace(" ", "")
                val key = "$name|$number"
                if (key in seen) continue
                seen += key
                out.put(
                    JSONObject()
                        .put("name", name)
                        .put("number", number)
                        .put("type", c.getInt(iType))
                )
            }
        }
        return JSONObject().put("contacts", out)
    }
}
