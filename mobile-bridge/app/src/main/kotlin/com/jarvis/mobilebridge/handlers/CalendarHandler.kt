package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.content.Intent
import android.provider.CalendarContract
import org.json.JSONObject

/**
 * Calendar events via ACTION_INSERT — opens the calendar's "new event" screen pre-filled
 * (no WRITE_CALENDAR permission needed; the user confirms with one tap). Direct deletion of a
 * specific event isn't possible without the event id + WRITE_CALENDAR, so `remove` opens the
 * calendar app for the user to delete it.
 */
object CalendarHandler {
    /** Args: title (required), begin (epoch ms, optional), end (epoch ms, optional),
     *  all_day (bool), location (optional). */
    fun add(ctx: Context, args: JSONObject): JSONObject {
        val title = args.optString("title").ifEmpty { args.optString("name") }
        if (title.isEmpty()) return JSONObject().put("error", "title required")
        val begin = args.optLong("begin", 0L)
        val end = args.optLong("end", if (begin > 0) begin + 3_600_000L else 0L)
        val i = Intent(Intent.ACTION_INSERT)
            .setData(CalendarContract.Events.CONTENT_URI)
            .putExtra(CalendarContract.Events.TITLE, title)
            .putExtra(CalendarContract.EXTRA_EVENT_ALL_DAY, args.optBoolean("all_day", false))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (begin > 0) i.putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, begin)
        if (end > 0) i.putExtra(CalendarContract.EXTRA_EVENT_END_TIME, end)
        args.optString("location").let { if (it.isNotEmpty()) i.putExtra(CalendarContract.Events.EVENT_LOCATION, it) }
        return try {
            ctx.startActivity(i)
            JSONObject().put("calendar_event_opened", true).put("title", title)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "no calendar app available")
        }
    }

    /** Open the calendar app (the user deletes the event manually — no silent-delete API). */
    fun remove(ctx: Context, @Suppress("UNUSED_PARAMETER") args: JSONObject): JSONObject {
        val i = Intent(Intent.ACTION_VIEW)
            .setData(CalendarContract.CONTENT_URI.buildUpon().appendPath("time").build())
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            ctx.startActivity(i)
            JSONObject().put("calendar_opened", true)
                .put("note", "Android has no silent-delete; opened the calendar to remove it.")
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "no calendar app available")
        }
    }
}
