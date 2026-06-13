package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.ContactsContract
import org.json.JSONObject

/**
 * Places a WhatsApp voice/video call by firing the contact-data call intent that
 * WhatsApp registers on a contact's row (the technique automation apps use).
 * Needs the contact SAVED and a WhatsApp user — the call data row only exists
 * then. READ_CONTACTS required (already granted for contacts_search).
 *
 *   voice -> vnd.android.cursor.item/vnd.com.whatsapp.voip.call
 *   video -> vnd.android.cursor.item/vnd.com.whatsapp.video.call
 */
object WhatsAppCallHandler {
    fun call(ctx: Context, args: JSONObject): JSONObject {
        val number = args.optString("number").trim()
        if (number.isEmpty()) return JSONObject().put("error", "number required")
        val video = args.optBoolean("video", false)
        val mime = if (video)
            "vnd.android.cursor.item/vnd.com.whatsapp.video.call"
        else
            "vnd.android.cursor.item/vnd.com.whatsapp.voip.call"
        val dataId = whatsAppDataId(ctx, number, mime) ?: return JSONObject().put(
            "error",
            "no WhatsApp ${if (video) "video " else ""}call for that contact — save them and " +
                "make sure they're on WhatsApp"
        )
        return try {
            val i = Intent(Intent.ACTION_VIEW)
                .setDataAndType(Uri.parse("content://com.android.contacts/data/$dataId"), mime)
                .setPackage("com.whatsapp")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
            JSONObject().put("whatsapp_calling", true).put("video", video)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "whatsapp call failed")
        }
    }

    /** number -> contactId (PhoneLookup) -> the WhatsApp call data-row _ID for [mime]. */
    private fun whatsAppDataId(ctx: Context, number: String, mime: String): Long? {
        var contactId = -1L
        val lookup = Uri.withAppendedPath(
            ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number)
        )
        ctx.contentResolver.query(
            lookup, arrayOf(ContactsContract.PhoneLookup.CONTACT_ID), null, null, null
        )?.use { if (it.moveToFirst()) contactId = it.getLong(0) }
        if (contactId < 0L) return null
        ctx.contentResolver.query(
            ContactsContract.Data.CONTENT_URI,
            arrayOf(ContactsContract.Data._ID),
            "${ContactsContract.Data.CONTACT_ID}=? AND ${ContactsContract.Data.MIMETYPE}=?",
            arrayOf(contactId.toString(), mime),
            null
        )?.use { if (it.moveToFirst()) return it.getLong(0) }
        return null
    }
}
