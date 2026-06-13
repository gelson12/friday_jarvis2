package com.jarvis.mobilebridge

import android.content.Context
import com.jarvis.mobilebridge.handlers.AlarmHandler
import com.jarvis.mobilebridge.handlers.AppHandler
import com.jarvis.mobilebridge.handlers.BrowserHandler
import com.jarvis.mobilebridge.handlers.CalendarHandler
import com.jarvis.mobilebridge.handlers.ContactsHandler
import com.jarvis.mobilebridge.handlers.DeviceStatusHandler
import com.jarvis.mobilebridge.handlers.DialHandler
import com.jarvis.mobilebridge.handlers.HostInfoHandler
import com.jarvis.mobilebridge.handlers.LocationHandler
import com.jarvis.mobilebridge.handlers.RingerHandler
import com.jarvis.mobilebridge.handlers.SettingsPanelHandler
import com.jarvis.mobilebridge.handlers.SmsHandler
import com.jarvis.mobilebridge.handlers.TelegramHandler
import com.jarvis.mobilebridge.handlers.VolumeHandler
import com.jarvis.mobilebridge.handlers.WhatsAppCallHandler
import com.jarvis.mobilebridge.handlers.WhatsAppHandler
import org.json.JSONObject

/**
 * Maps a `cmd` name to its handler. Mirrors desktop-bridge/bridge.py's
 * _HANDLERS dict. Handlers return a JSONObject; the LiveKitClient
 * wraps it as `{"id", "machine", "result": <handler_output>}`.
 */
class CommandRouter(private val ctx: Context) {
    suspend fun execute(cmd: String, args: JSONObject): JSONObject = try {
        when (cmd) {
            "host_info" -> HostInfoHandler.execute(ctx, args)
            "sms_list" -> SmsHandler.list(ctx, args)
            "sms_send" -> SmsHandler.send(ctx, args)
            "contacts_search" -> ContactsHandler.search(ctx, args)
            "dial" -> DialHandler.execute(ctx, args)
            "open_app" -> AppHandler.open(ctx, args)
            "list_apps" -> AppHandler.list(ctx, args)
            "install_app" -> AppHandler.install(ctx, args)
            "uninstall_app" -> AppHandler.uninstall(ctx, args)
            "open_url" -> BrowserHandler.openUrl(ctx, args)
            "whatsapp_send" -> WhatsAppHandler.send(ctx, args)
            "whatsapp_call" -> WhatsAppCallHandler.call(ctx, args)
            "telegram_send" -> TelegramHandler.send(ctx, args)
            "location_get" -> LocationHandler.get(ctx, args)
            "location_panel" -> LocationHandler.panel(ctx)
            "device_status" -> DeviceStatusHandler.execute(ctx, args)
            "volume_set" -> VolumeHandler.set(ctx, args)
            "ringer_set" -> RingerHandler.set(ctx, args)
            "alarm_set" -> AlarmHandler.set(ctx, args)
            "alarm_dismiss" -> AlarmHandler.dismiss(ctx, args)
            "calendar_add" -> CalendarHandler.add(ctx, args)
            "calendar_remove" -> CalendarHandler.remove(ctx, args)
            "wifi_panel" -> SettingsPanelHandler.wifi(ctx, args)
            "hotspot_panel" -> SettingsPanelHandler.hotspot(ctx, args)
            else -> JSONObject().put("error", "unknown command '$cmd'")
        }
    } catch (e: Exception) {
        JSONObject().put("error", e.message ?: "unknown error")
    }
}
